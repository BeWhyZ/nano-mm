"""
FairValueServer: wires BinanceSpotOrderBookTracker → FairValueEngine.

Usage:
    server = FairValueServer(symbol, session, on_state=my_callback)
    await server.run()
"""
from __future__ import annotations

from collections.abc import Callable

import aiohttp
import structlog

from biz.domain.book import OrderBookSnapshot
from biz.usecase.fair_value import FairPriceState, FairValueEngine
from data.orderbook.binance_spot import BinanceSpotOrderBookTracker


class FairValueServer:
    """
    Composes the orderbook tracker and fair-value usecase into a runnable unit.

    on_state fires on every tick that produces a valid FairPriceState.
    """

    def __init__(
        self,
        symbol: str,
        session: aiohttp.ClientSession,
        on_state: Callable[[FairPriceState], None],
        lg: structlog.stdlib.BoundLogger,
        micro_k: int = 5,
        proxy: str | None = None,
    ) -> None:
        self.lg = lg.bind(component="fair_value_server", symbol=symbol.upper())
        self._engine = FairValueEngine(symbol, lg=self.lg, micro_k=micro_k)
        self._on_state = on_state
        self._tracker = BinanceSpotOrderBookTracker(
            symbol=symbol,
            session=session,
            lg=self.lg,
            on_update=self._on_update,
            proxy=proxy,
        )

    def _on_update(self, snap: OrderBookSnapshot) -> None:
        self._engine.on_tick(snap)
        state = self._engine.state
        if state is not None:
            self._on_state(state)

    async def run(self) -> None:
        self.lg.info("fair_value_server_start")
        await self._tracker.run()
