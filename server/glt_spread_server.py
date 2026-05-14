"""
GltSpreadServer: wires book tracker + aggTrade tracker + GLT engine.

Usage:
    server = GltSpreadServer(symbol, session, cfg, on_state=cb, lg=lg)
    await server.run()

`on_state` fires on every L2 tick that yields a QuoteState (i.e. always once
the book is synced; bid/ask may be None during calibration warmup).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import aiohttp
import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.quote import QuoteState
from biz.domain.trade import TradeTick
from biz.usecase.glt_spread import GltSpreadEngine
from config import SpreadConfig
from data.orderbook.binance_spot import BinanceSpotOrderBookTracker
from data.trade.binance_spot import BinanceSpotAggTradeTracker


class GltSpreadServer:

    def __init__(
        self,
        symbol: str,
        session: aiohttp.ClientSession,
        cfg: SpreadConfig,
        on_state: Callable[[QuoteState], None],
        lg: structlog.stdlib.BoundLogger,
        proxy: str | None = None,
    ) -> None:
        self.lg = lg.bind(component="glt_spread_server", symbol=symbol.upper())
        self._engine = GltSpreadEngine(symbol, cfg, lg=self.lg)
        self._on_state = on_state
        self._book = BinanceSpotOrderBookTracker(
            symbol=symbol, session=session, lg=self.lg,
            on_update=self._on_book, proxy=proxy,
        )
        self._trade = BinanceSpotAggTradeTracker(
            symbol=symbol, lg=self.lg, on_trade=self._on_trade, proxy=proxy,
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
        self.lg.info("glt_spread_server_start")
        await asyncio.gather(self._book.run(), self._trade.run())
