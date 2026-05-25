"""
FillSimulator: queue-aware maker fill simulation against public trade stream.

Fill model: strict FIFO / price-time priority.
  - At submit time we snapshot queue_ahead = visible depth at our price level in the
    book (depth already in the book BEFORE our order; conservative because book-diff
    cancels are invisible to us).
  - When a public aggressor trade lands at our price (same-tick) we consume queue_ahead
    first; residual fills us.
  - When a trade walks through our level (taker price strictly past our limit price),
    our order fills up to min(remaining_qty, tick.qty) — the trade is a bounded event.

Known limitations (Phase 1 v1 — do not fix here):
  - queue_ahead is never decreased from book diffs (cancels ahead of us are invisible)
    → pessimistic fill rate (better to under-estimate than over-estimate in paper trading).
  - Paper orders are "ghosts" — they are NOT inserted into the book, so they do not
    affect price formation or the queue_ahead of other paper orders.
  - Multi-paper-orders at the same exact price level: each is evaluated independently
    with the same queue_ahead snapshot taken at submit time. Phase 1 ladder uses
    distinct ticks so this does not arise in practice.
  - Walk-through fill is capped at tick.qty; any residual that would walk to the next
    level is silently dropped (accurate when there is only one paper order per level).
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import Fill, OrderSide


@dataclass(slots=True)
class _RestingOrder:
    coid: str
    side: OrderSide
    price: float
    remaining_qty: float
    queue_ahead: float
    submit_recv_ts: int  # monotonic_ns at add time — used as recv_ts on synthetic fills


_FILL_SEQ = itertools.count(1)
_EPS = 1e-12


class FillSimulator:

    def __init__(
        self,
        symbol: str,
        venue: str,
        lg: structlog.stdlib.BoundLogger,
    ) -> None:
        self._symbol = symbol
        self._venue = venue
        self.lg = lg.bind(component="fill_simulator", symbol=symbol)
        self._orders: dict[str, _RestingOrder] = {}
        self._latest_snap: OrderBookSnapshot | None = None

    # ------------------------------------------------------------------
    # Book feed
    # ------------------------------------------------------------------

    def set_book(self, snap: OrderBookSnapshot) -> None:
        self._latest_snap = snap

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------

    def add_order(self, coid: str, side: OrderSide, price: float, qty: float) -> None:
        """Register a newly ACKed paper order for fill simulation.

        Must be called only after PaperExchange.on_ack fires — never on the submit
        path directly (post-only rejects must never reach the simulator).
        """
        queue_ahead = self._depth_at(self._latest_snap, side, price)
        self._orders[coid] = _RestingOrder(
            coid=coid,
            side=side,
            price=price,
            remaining_qty=qty,
            queue_ahead=queue_ahead,
            submit_recv_ts=time.monotonic_ns(),
        )
        self.lg.debug(
            "sim_order_added",
            coid=coid,
            side=side.value,
            price=price,
            qty=qty,
            queue_ahead=queue_ahead,
        )

    def remove_order(self, coid: str) -> None:
        """Deregister a paper order (cancelled or terminal)."""
        self._orders.pop(coid, None)

    # ------------------------------------------------------------------
    # Trade feed
    # ------------------------------------------------------------------

    def on_trade(self, tick) -> list[Fill]:  # tick: TradeTick (duck-typed to avoid circular)
        """Process one public aggressor trade; return any synthetic fills generated."""
        fills: list[Fill] = []
        completed: list[str] = []

        for coid, ord_ in self._orders.items():
            fill = self._try_fill(ord_, tick)
            if fill is not None:
                fills.append(fill)
                if ord_.remaining_qty <= _EPS:
                    completed.append(coid)

        for coid in completed:
            self._orders.pop(coid, None)

        return fills

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_fill(self, ord_: _RestingOrder, tick) -> Fill | None:
        """Attempt to fill *ord_* against *tick*. Returns Fill or None."""
        # Determine if this trade can hit our resting order.
        # BUY resting maker: filled by a SELL aggressor at price <= our bid.
        # SELL resting maker: filled by a BUY aggressor at price >= our ask.
        if ord_.side == OrderSide.BUY:
            if tick.side != OrderSide.SELL:
                return None
            if tick.price > ord_.price + _EPS:
                return None  # trade is at a worse ask; doesn't reach us
        else:  # SELL resting
            if tick.side != OrderSide.BUY:
                return None
            if tick.price < ord_.price - _EPS:
                return None  # trade is at a worse bid; doesn't reach us

        # Trade can reach our level.  Determine fill qty.
        if (
            (ord_.side == OrderSide.BUY and tick.price < ord_.price - _EPS) or
            (ord_.side == OrderSide.SELL and tick.price > ord_.price + _EPS)
        ):
            # Walk-through: taker swept past our price level entirely.
            # Our entire level was consumed; we fill up to min(remaining, tick.qty).
            ord_.queue_ahead = 0.0
            fill_qty = min(ord_.remaining_qty, tick.qty)
        else:
            # Trade is at exactly our price.  Consume queue_ahead first.
            if tick.qty <= ord_.queue_ahead + _EPS:
                ord_.queue_ahead = max(0.0, ord_.queue_ahead - tick.qty)
                return None  # queue not exhausted yet
            residual = tick.qty - ord_.queue_ahead
            ord_.queue_ahead = 0.0
            fill_qty = min(ord_.remaining_qty, residual)

        if fill_qty <= _EPS:
            return None

        ord_.remaining_qty -= fill_qty
        if ord_.remaining_qty < _EPS:
            ord_.remaining_qty = 0.0

        trade_id = f"paper-{tick.event_ts}-{ord_.coid}-{next(_FILL_SEQ)}"
        self.lg.debug(
            "sim_fill",
            coid=ord_.coid,
            trade_id=trade_id,
            fill_qty=fill_qty,
            fill_price=ord_.price,
            remaining=ord_.remaining_qty,
        )
        return Fill(
            trade_id=trade_id,
            order_id=ord_.coid,
            price=ord_.price,
            qty=fill_qty,
            side=ord_.side,
            event_ts=tick.event_ts,
            recv_ts=time.monotonic_ns(),
            fee=0.0,
            fee_asset="",
        )

    @staticmethod
    def _depth_at(
        snap: OrderBookSnapshot | None,
        side: OrderSide,
        price: float,
    ) -> float:
        """Return visible depth at *price* on the maker side of the book.

        For a BUY resting maker we look at the bid side; for SELL we look at asks.
        Returns 0.0 if the level is absent or snap is None.
        """
        if snap is None:
            return 0.0
        levels = snap.bids if side == OrderSide.BUY else snap.asks
        for level in levels:
            if abs(level.price - price) < 1e-9:
                return level.qty
        return 0.0
