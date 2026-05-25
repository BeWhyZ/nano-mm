"""
MMService: lifecycle owner for one (symbol, exchange) market-making pair.

Owns:
- Target OrderBook + AggTrade WebSocket subscriptions → FairValueEngine + GltSpreadEngine
- Optional reference OrderBook subscription → separate FairValueEngine for ref mid

Reference venue mid (used for uncontaminated markout baseline in archive) may
differ from the target venue when doing cross-venue quoting.  When target ==
reference the two engines share the same snapshot.

Usage:
    svc = MMService(symbol, Exchange.BINANCE_SPOT, session, cfg, lg)
    svc.register_quote_listener(on_quote)
    await svc.run()
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import aiohttp
import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.quote import QuoteState
from biz.domain.trade import TradeTick
from biz.usecase.fair_value import FairPriceState, FairValueEngine
from biz.usecase.glt_spread import GltSpreadEngine
from config import PricingConfig, SpreadConfig
from biz.repo.orderbook import make_orderbook_tracker
from biz.repo.trade import make_trade_tracker
from biz.repo.archive import ArchiveRepo
from pkg.constant import Exchange


class MMService:
    """
    Lifecycle owner for one market-making pair.

    Subscribes to the orderbook and trade streams for target *exchange*/*symbol*,
    and optionally subscribes a separate *reference_exchange* orderbook for an
    uncontaminated fair-price feed.  Dispatches QuoteState updates to registered
    listeners on every L2 tick.
    """

    def __init__(
        self,
        symbol: str,
        exchange: Exchange,
        session: aiohttp.ClientSession,
        pricing_cfg: PricingConfig,
        spread_cfg: SpreadConfig,
        lg: structlog.stdlib.BoundLogger,
        reference_exchange: Exchange | None = None,
        archive: ArchiveRepo | None = None,
        proxy: str | None = None,
    ) -> None:
        self._symbol = symbol.upper()
        self._exchange = exchange
        self._ref_exchange = reference_exchange or exchange
        self._archive = archive
        self.lg = lg.bind(component="mm_service", symbol=self._symbol, exchange=str(exchange))

        # biz layer — target venue
        self._fair_engine = FairValueEngine(symbol, lg=self.lg, micro_k=int(pricing_cfg.micro_k))
        self._glt_engine = GltSpreadEngine(symbol, spread_cfg, lg=self.lg)

        # biz layer — reference venue (may alias target engine if same exchange)
        if self._ref_exchange != exchange:
            self._ref_fair_engine: FairValueEngine = FairValueEngine(
                symbol, lg=self.lg, micro_k=int(pricing_cfg.micro_k)
            )
        else:
            self._ref_fair_engine = self._fair_engine

        self._book_tracker = make_orderbook_tracker(
            exchange=exchange,
            symbol=symbol,
            session=session,
            on_update=self._on_book,
            lg=self.lg,
            proxy=proxy,
        )
        self._trade_tracker = make_trade_tracker(
            exchange=exchange,
            symbol=symbol,
            on_trade=self._on_trade,
            lg=self.lg,
            proxy=proxy,
        )

        # Reference venue tracker (only spawned when ref != target)
        self._ref_book_tracker = (
            make_orderbook_tracker(
                exchange=self._ref_exchange,
                symbol=symbol,
                session=session,
                on_update=self._on_ref_book,
                lg=self.lg,
                proxy=proxy,
            )
            if self._ref_exchange != exchange
            else None
        )

        # Reference venue trade tracker (only when ref != target).
        # Forwards binance aggTrades to ref_trade_listeners for cross-venue
        # cancel-on-signal logic in the executor.
        self._ref_trade_tracker = (
            make_trade_tracker(
                exchange=self._ref_exchange,
                symbol=symbol,
                on_trade=self._on_ref_trade,
                lg=self.lg,
                proxy=proxy,
            )
            if self._ref_exchange != exchange
            else None
        )

        self._book_listeners: list[Callable[[OrderBookSnapshot], None]] = []
        self._quote_listeners: list[Callable[[QuoteState], None]] = []
        self._trade_listeners: list[Callable[[TradeTick], None]] = []
        self._ref_trade_listeners: list[Callable[[TradeTick], None]] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_fair_price(self, reference: bool = False) -> FairPriceState | None:
        engine = self._ref_fair_engine if reference else self._fair_engine
        return engine.state

    @property
    def quote_state(self) -> QuoteState | None:
        return self._glt_engine.state

    def set_inventory(self, q_norm: float) -> None:
        self._glt_engine.on_inventory(q_norm)

    def register_book_listener(self, cb: Callable[[OrderBookSnapshot], None]) -> None:
        self._book_listeners.append(cb)

    def register_quote_listener(self, cb: Callable[[QuoteState], None]) -> None:
        self._quote_listeners.append(cb)

    def register_trade_listener(self, cb: Callable[[TradeTick], None]) -> None:
        self._trade_listeners.append(cb)

    def register_ref_trade_listener(self, cb: Callable[[TradeTick], None]) -> None:
        """Register a listener for reference-venue aggTrades (cross-venue only).

        No-op when target == reference (ref_trade_tracker is None in that case).
        """
        self._ref_trade_listeners.append(cb)

    async def run(self) -> None:
        self.lg.info("mm_service_start", reference_venue=str(self._ref_exchange))
        tasks = [self._book_tracker.run(), self._trade_tracker.run()]
        if self._ref_book_tracker is not None:
            tasks.append(self._ref_book_tracker.run())
        if self._ref_trade_tracker is not None:
            tasks.append(self._ref_trade_tracker.run())
        await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    def _on_book(self, snap: OrderBookSnapshot) -> None:
        self._fair_engine.on_tick(snap)
        self._glt_engine.on_book(snap)

        state = self._glt_engine.state
        target_fp = self._fair_engine.state
        ref_fp = self._ref_fair_engine.state

        target_mid = target_fp.mid if target_fp else (snap.mid_price or 0.0)
        ref_mid = ref_fp.mid if ref_fp else target_mid

        if self._archive is not None and snap.mid_price is not None:
            micro = target_fp.micro if target_fp else None
            self._archive.write_mid_sample(snap, mid=target_mid, micro=micro, role="target")

        if state is not None:
            if self._archive is not None:
                self._archive.write_quote_snapshot(
                    state, target_mid=target_mid, ref_mid=ref_mid, event_type="requote"
                )
            for cb in self._quote_listeners:
                cb(state)
        for cb in self._book_listeners:
            cb(snap)

    def _on_ref_book(self, snap: OrderBookSnapshot) -> None:
        self._ref_fair_engine.on_tick(snap)
        if self._archive is not None and snap.mid_price is not None:
            ref_fp = self._ref_fair_engine.state
            micro = ref_fp.micro if ref_fp else None
            self._archive.write_mid_sample(snap, mid=snap.mid_price, micro=micro, role="reference")

    def _on_trade(self, tick: TradeTick) -> None:
        self._glt_engine.on_trade(tick)
        if self._archive is not None:
            self._archive.write_trade_tick(tick, role="target")
        for cb in self._trade_listeners:
            cb(tick)

    def _on_ref_trade(self, tick: TradeTick) -> None:
        if self._archive is not None:
            self._archive.write_trade_tick(tick, role="reference")
        for cb in self._ref_trade_listeners:
            cb(tick)
