"""
GltSpreadServer: wires FairValueService (shared book feed) + aggTrade tracker + GLT engine.

The book subscription is owned by FairValueService — this server registers a listener
rather than opening a duplicate WebSocket. Only the trade tracker is owned here.

Usage:
    fair_svc = FairValueService(symbol, [Exchange.BINANCE_SPOT], session, cfg.pricing_engine, lg)
    server = GltSpreadServer(symbol, fair_svc, cfg.spread_engine, on_state=cb, lg=lg)
    await asyncio.gather(fair_svc.run(), server.run())

`on_state` fires on every L2 tick that yields a QuoteState (bid/ask may be None during
calibration warmup of ~30–60 s).
"""
from __future__ import annotations

from collections.abc import Callable

import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.quote import QuoteState
from biz.domain.trade import TradeTick
from biz.repo.trade import make_trade_tracker
from biz.usecase.glt_spread import GltSpreadEngine
from config import SpreadConfig
from pkg.constant import Exchange
from service.fair_value_service import FairValueService


class GltSpreadServer:

    def __init__(
        self,
        symbol: str,
        fair_value_svc: FairValueService,
        cfg: SpreadConfig,
        on_state: Callable[[QuoteState], None],
        lg: structlog.stdlib.BoundLogger,
        exchange: Exchange = Exchange.BINANCE_SPOT,
        proxy: str | None = None,
    ) -> None:
        self.lg = lg.bind(component="glt_spread_server", symbol=symbol.upper())
        self._engine = GltSpreadEngine(symbol, cfg, lg=self.lg)
        self._on_state = on_state

        # Register book listener on primary exchange — avoids a duplicate subscription
        fair_value_svc.register_book_listener(exchange, self._on_book)

        self._trade = make_trade_tracker(
            exchange=exchange, symbol=symbol, on_trade=self._on_trade, lg=self.lg, proxy=proxy,
        )

    def set_inventory(self, q_norm: float) -> None:
        """Push normalized inventory ∈ [-1, 1] into the engine."""
        self._engine.on_inventory(q_norm)

    def _on_book(self, snap: OrderBookSnapshot) -> None:
        self._engine.on_book(snap)
        state = self._engine.state
        if state is not None:
            self._on_state(state)

    def _on_trade(self, tick: TradeTick) -> None:
        self._engine.on_trade(tick)

    async def run(self) -> None:
        """Run the trade tracker. Book lifecycle is owned by FairValueService."""
        self.lg.info("glt_spread_server_start")
        await self._trade.run()
