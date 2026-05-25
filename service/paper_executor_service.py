"""
PaperExecutor: closes the MM loop in paper-trading mode.

Wiring:
  MMService  ─book_listener──►  PaperExecutor._on_book
             ─quote_listener──► PaperExecutor._on_quote
             ─trade_listener──► PaperExecutor._on_trade

On each QuoteState:
  QuoteDiffer.diff()  →  cancel/place actions  →  PaperExchange
  PaperExchange callbacks  →  OrderTracker + FillSimulator

On each public trade:
  FillSimulator.on_trade()  →  fills  →  OrderTracker / PnlTracker / archive
  q_norm recomputed  →  MMService.set_inventory()  (GLT skew feedback)
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import defaultdict
from dataclasses import dataclass

import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import Fill, Order, OrderSide, OrderStatus, OrderType
from biz.domain.quote import QuoteState
from biz.repo.archive import ArchiveRepo, FillArchiveCtx, OrderArchiveCtx
from biz.usecase import fill_simulator as _fill_sim_mod
from biz.usecase import quote_differ
from biz.usecase.fill_simulator import FillSimulator
from biz.usecase.quote_differ import CancelAction, PlaceAction
from config import PaperConfig, SpreadConfig
from data.exchange.oms import OrderTracker
from data.exchange.paper import PaperExchange
from service.mm_service import MMService


# ---------------------------------------------------------------------------
# PnlTracker — average-cost realized + unrealized PnL
# ---------------------------------------------------------------------------

class PnlTracker:
    """
    Average-cost PnL accounting.

    BUY fills increase long inventory and raise cost basis.
    SELL fills against a long position realize PnL = qty × (fill_price − avg_cost).
    Position flips are handled by realizing the closing portion then re-basing.

    Inventory here is a cross-check mirror; authoritative inventory lives in OrderTracker.
    """

    def __init__(self) -> None:
        self._inventory: float = 0.0   # signed base qty (mirror of OrderTracker)
        self._avg_cost: float = 0.0    # quote-currency per unit
        self._realized: float = 0.0

    def on_fill(self, fill: Fill) -> tuple[float, float]:
        """Apply fill; return (realized_pnl_after, unrealized_at_fill_mark).

        unrealized is computed at fill price as a proxy mark — caller can pass
        a better mark if available.
        """
        qty = fill.qty if fill.side == OrderSide.BUY else -fill.qty
        price = fill.price

        if self._inventory == 0.0:
            self._inventory = qty
            self._avg_cost = price
        elif (self._inventory > 0.0 and qty > 0.0) or (self._inventory < 0.0 and qty < 0.0):
            # Adding to existing side — update avg cost.
            total = self._inventory + qty
            self._avg_cost = (self._inventory * self._avg_cost + qty * price) / total
            self._inventory = total
        else:
            # Reducing or flipping.
            closing = min(abs(qty), abs(self._inventory))
            if self._inventory > 0.0:
                self._realized += closing * (price - self._avg_cost)
            else:
                self._realized += closing * (self._avg_cost - price)

            residual = abs(qty) - closing
            new_inv = self._inventory + qty
            if abs(new_inv) < 1e-12:
                self._inventory = 0.0
                self._avg_cost = 0.0
            elif residual > 1e-12:
                # Position flipped — re-base avg cost on residual.
                self._inventory = new_inv
                self._avg_cost = price
            else:
                self._inventory = new_inv

        unrealized = self.unrealized(price)
        return self._realized, unrealized

    def unrealized(self, mark: float) -> float:
        if self._inventory == 0.0:
            return 0.0
        return self._inventory * (mark - self._avg_cost)

    @property
    def realized(self) -> float:
        return self._realized


# ---------------------------------------------------------------------------
# QuoteCtx — stash of strategy state at order-submit time (for fill archive)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _QuoteCtx:
    state: QuoteState
    snap: OrderBookSnapshot
    ladder_level: int


# ---------------------------------------------------------------------------
# PaperExecutor
# ---------------------------------------------------------------------------

class PaperExecutor:

    def __init__(
        self,
        symbol: str,
        venue: str,
        mm_service: MMService,
        spread_cfg: SpreadConfig,
        paper_cfg: PaperConfig,
        session_id: str,
        archive: ArchiveRepo | None,
        lg: structlog.stdlib.BoundLogger,
    ) -> None:
        self._symbol = symbol.upper()
        self._venue = venue
        self._mm = mm_service
        self._spread_cfg = spread_cfg
        self._paper_cfg = paper_cfg
        self._session_id = session_id
        self._archive = archive
        self.lg = lg.bind(component="paper_executor", symbol=self._symbol)

        self._tracker = OrderTracker(lg)
        self._pnl = PnlTracker()
        self._sim = FillSimulator(symbol, venue, lg)

        self._exch = PaperExchange(
            symbol=symbol,
            venue=venue,
            on_ack=self._on_ack,
            on_reject=self._on_reject,
            on_cancel_ack=self._on_cancel_ack,
            lg=lg,
        )

        self._latest_snap: OrderBookSnapshot | None = None
        self._latest_state: QuoteState | None = None

        # coid → _QuoteCtx at the moment of submission
        self._quote_ctx: dict[str, _QuoteCtx] = {}

        # coid → monotonic_ns when order was ACKed; used for max_quote_age eviction.
        self._order_ack_ts: dict[str, int] = {}

        self._coid_seq = itertools.count(1)
        self._event_seq: dict[str, int] = defaultdict(int)

        # Set initial inventory from config
        if paper_cfg.initial_q_norm != 0.0:
            mm_service.set_inventory(paper_cfg.initial_q_norm)

        # Register listeners
        mm_service.register_book_listener(self._on_book)
        mm_service.register_quote_listener(self._on_quote)
        mm_service.register_trade_listener(self._on_trade)
        mm_service.register_ref_trade_listener(self._on_ref_trade)

    # ------------------------------------------------------------------
    # Listener callbacks (called from MMService event loop)
    # ------------------------------------------------------------------

    def _on_book(self, snap: OrderBookSnapshot) -> None:
        self._latest_snap = snap
        self._exch.set_book(snap)
        self._sim.set_book(snap)
        self._expire_stale_quotes()

    def _on_quote(self, state: QuoteState) -> None:
        self._latest_state = state
        if self._latest_snap is None:
            return

        snap = self._latest_snap
        active = self._all_live_orders()
        actions = quote_differ.diff(
            state,
            snap,
            active,
            price_tick=self._spread_cfg.price_tick,
            qty_step=self._paper_cfg.qty_step,
        )

        for action in actions:
            if isinstance(action, CancelAction):
                self._tracker.mark_pending_cancel(action.client_order_id)
                asyncio.create_task(
                    self._exch.cancel_order(action.client_order_id, self._symbol)
                )
            else:  # PlaceAction
                self._dispatch_place(action, state)

    def _on_trade(self, tick) -> None:
        fills = self._sim.on_trade(tick)
        for fill in fills:
            self._apply_fill(fill, tick)

    # ------------------------------------------------------------------
    # PaperExchange callbacks
    # ------------------------------------------------------------------

    def _on_ack(self, coid: str, exid: str) -> None:
        order = self._tracker.on_ack(coid, exid)
        self._sim.add_order(coid, order.side, order.price, order.original_qty)
        self._order_ack_ts[coid] = time.monotonic_ns()
        if self._archive is not None:
            self._archive.write_order_event(
                order, "ACK", seq=self._next_event_seq(coid), is_maker=True
            )

    def _on_reject(self, coid: str, reason: str) -> None:
        order = self._tracker.on_reject(coid, reason)
        self._quote_ctx.pop(coid, None)
        self._order_ack_ts.pop(coid, None)
        if self._archive is not None:
            self._archive.write_order_event(
                order, "REJECT", seq=self._next_event_seq(coid),
                reason=reason, reject_code="post_only_cross",
            )

    def _on_cancel_ack(self, coid: str, canceled_qty: float) -> None:
        order = self._tracker.on_cancel_ack(coid, canceled_qty)
        self._sim.remove_order(coid)
        self._quote_ctx.pop(coid, None)
        self._order_ack_ts.pop(coid, None)
        if self._archive is not None:
            self._archive.write_order_event(
                order, "CANCEL_ACK", seq=self._next_event_seq(coid)
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch_place(self, action: PlaceAction, state: QuoteState) -> None:
        sid = self._session_id[:6] if len(self._session_id) >= 6 else self._session_id
        coid = f"{sid}-{next(self._coid_seq):07d}"

        order = Order(
            client_order_id=coid,
            symbol=self._symbol,
            venue=self._venue,
            side=action.side,
            order_type=OrderType.LIMIT_MAKER,
            price=action.price,
            original_qty=action.qty,
        )
        self._tracker.add(order)

        # Stash context for fill-time archive enrichment
        self._quote_ctx[coid] = _QuoteCtx(
            state=state,
            snap=action.snap,
            ladder_level=action.ladder_level,
        )

        # Archive the new order row
        if self._archive is not None:
            queue_ahead = FillSimulator._depth_at(action.snap, action.side, action.price)
            ref_fair = self._mm.get_fair_price(reference=True)
            mid_ref_now = ref_fair.mid if ref_fair else state.mid
            ctx = OrderArchiveCtx(
                session_id=self._session_id,
                ladder_level=action.ladder_level,
                quote_emit_ts_ns=state.ts_ns,
                mid_target=state.mid,
                mid_ref=mid_ref_now,
                sigma=state.sigma,
                A=state.A,
                k=state.k,
                gamma=state.gamma,
                q_norm=state.q_norm,
                queue_ahead=queue_ahead,
                book_seq=action.snap.seq,
            )
            self._archive.write_order(order, ctx)
            self._archive.write_order_event(
                order, "ADD", seq=self._next_event_seq(coid)
            )

        asyncio.create_task(
            self._exch.submit_limit(
                client_order_id=coid,
                symbol=self._symbol,
                side=action.side,
                price=action.price,
                qty=action.qty,
                post_only=True,
                snap=action.snap,
            )
        )

    def _apply_fill(self, fill: Fill, tick) -> None:
        # Capture is_ghost_fill BEFORE on_fill mutates order status.
        order = self._tracker.get(fill.order_id)
        is_ghost = (
            order is not None and order.status == OrderStatus.PENDING_CANCEL
        )

        order = self._tracker.on_fill(fill)
        if order is None:
            return

        inv_before = self._pnl._inventory
        realized, unrealized = self._pnl.on_fill(fill)

        # Sanity cross-check (debug only — don't crash production on float drift)
        tracker_inv = self._tracker.inventory()
        if abs(self._pnl._inventory - tracker_inv) > 1e-8:
            self.lg.warning(
                "pnl_inventory_diverged",
                pnl_inv=self._pnl._inventory,
                tracker_inv=tracker_inv,
            )

        q_norm = max(-1.0, min(1.0, self._tracker.inventory() / self._spread_cfg.Q_max))
        self._mm.set_inventory(q_norm)
        self._exch.notify_fill(fill.order_id, fill.qty)

        # Archive fill
        if self._archive is not None:
            ctx_snap = self._quote_ctx.get(fill.order_id)
            state = ctx_snap.state if ctx_snap else self._latest_state
            snap = ctx_snap.snap if ctx_snap else self._latest_snap
            ladder_level = ctx_snap.ladder_level if ctx_snap else 0

            fair = self._mm.get_fair_price()
            ref_fair = self._mm.get_fair_price(reference=True)
            mid_target = (fair.mid if fair else (state.mid if state else 0.0))
            mid_ref = (ref_fair.mid if ref_fair else mid_target)
            spread_bps = (
                (snap.spread() / snap.mid_price * 1e4)
                if snap and snap.mid_price and snap.spread()
                else None
            )
            obi = fair.obi if fair and hasattr(fair, "obi") else None
            micro = fair.micro if fair else None
            micro_minus_mid_bps = (
                (micro - mid_target) / mid_target * 1e4
                if micro and mid_target else None
            )
            sigma_norm = (
                state.sigma / mid_target if state and mid_target else None
            )

            fill_ctx = FillArchiveCtx(
                session_id=self._session_id,
                mid_target=mid_target,
                mid_ref=mid_ref,
                spread_bps=spread_bps,
                obi=obi,
                micro_minus_mid_bps=micro_minus_mid_bps,
                book_seq=snap.seq if snap else None,
                sigma=state.sigma if state else None,
                sigma_norm=sigma_norm,
                q_norm=state.q_norm if state else 0.0,
                ladder_level=ladder_level,
                aggressor_imbalance_30s=None,
                quote_emit_ts_ns=state.ts_ns if state else 0,
                inventory_before=inv_before,
                inventory_after=self._pnl._inventory,
                q_norm_after=q_norm,
                realized_pnl_after=realized,
                unrealized_pnl_at_fill=unrealized,
                is_ghost_fill=is_ghost,
                is_maker=True,
            )
            self._archive.write_fill(fill, fill_ctx)
            self._archive.write_order_event(
                order, "FILL",
                seq=self._next_event_seq(fill.order_id),
                fill=fill,
                is_maker=True,
            )

        # Clean up ctx if order reached terminal state
        if order.status.is_terminal:
            self._quote_ctx.pop(fill.order_id, None)
            self._order_ack_ts.pop(fill.order_id, None)

        self.lg.info(
            "paper_fill",
            coid=fill.order_id,
            side=fill.side.value,
            price=fill.price,
            qty=fill.qty,
            inventory=self._tracker.inventory(),
            q_norm=q_norm,
            realized_pnl=realized,
        )

    def _cancel_orders(self, coids: list[str]) -> None:
        """Mark and schedule async cancel for a list of coids."""
        for coid in coids:
            self._tracker.mark_pending_cancel(coid)
            asyncio.create_task(self._exch.cancel_order(coid, self._symbol))

    def _expire_stale_quotes(self) -> None:
        """Force-cancel quotes that have been resting longer than max_quote_age_ms."""
        if self._paper_cfg.max_quote_age_ms <= 0.0:
            return
        max_age_ns = int(self._paper_cfg.max_quote_age_ms * 1_000_000)
        now = time.monotonic_ns()
        stale = [
            o.client_order_id
            for o in self._all_live_orders()
            if o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
            and now - self._order_ack_ts.get(o.client_order_id, now) > max_age_ns
        ]
        if stale:
            self.lg.debug("expire_stale_quotes", count=len(stale))
            self._cancel_orders(stale)

    def _on_ref_trade(self, tick) -> None:
        """Cancel the adverse side immediately on a reference-venue aggTrade.

        A BUY aggressor on the reference (binance) means price is rising —
        our resting asks on the target (bybit) are at stale low prices and
        vulnerable to informed cross-venue arbitrage.  Cancel them now and
        let the next quote cycle re-place at the updated fair price.

        Symmetric logic for SELL aggressor → cancel bids.

        Controlled by paper_cfg.ref_trade_cancel_min_notional (0 = all trades).
        """
        min_notional = self._paper_cfg.ref_trade_cancel_min_notional
        if min_notional > 0.0 and tick.price * tick.qty < min_notional:
            return

        if tick.side == OrderSide.BUY:
            adverse_side = OrderSide.SELL   # our asks are stale
        else:
            adverse_side = OrderSide.BUY    # our bids are stale

        targets = [
            o.client_order_id
            for o in self._all_live_orders()
            if o.side == adverse_side
            and o.status in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
        ]
        if targets:
            self.lg.debug(
                "ref_trade_cancel",
                ref_side=tick.side.value,
                ref_price=tick.price,
                ref_qty=tick.qty,
                cancelling=len(targets),
            )
            self._cancel_orders(targets)

    def _all_live_orders(self) -> list[Order]:
        return [
            o for o in self._tracker._orders.values()
            if o.status in {
                OrderStatus.PENDING_NEW,
                OrderStatus.OPEN,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.PENDING_CANCEL,
            }
        ]

    def _next_event_seq(self, coid: str) -> int:
        seq = self._event_seq[coid]
        self._event_seq[coid] += 1
        return seq
