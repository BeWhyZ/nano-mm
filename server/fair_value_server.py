"""
FairValueServer: thin orchestrator around FairValueService.

Usage:
    server = FairValueServer(symbol, session, on_state=my_callback, cfg=cfg, lg=lg)
    await server.run()
"""
from __future__ import annotations

from collections.abc import Callable

import aiohttp
import structlog

from biz.usecase.fair_value import FairPriceState
from config import Config
from pkg.constant import Exchange
from service.fair_value_service import FairValueService


class FairValueServer:
    """
    Wires FairValueService with an on_state callback for each book tick.

    on_state fires on every tick that produces a valid FairPriceState.
    """

    def __init__(
        self,
        symbol: str,
        session: aiohttp.ClientSession,
        on_state: Callable[[FairPriceState], None],
        cfg: Config,
        lg: structlog.stdlib.BoundLogger,
        exchanges: list[Exchange] | None = None,
        proxy: str | None = None,
    ) -> None:
        self.lg = lg.bind(component="fair_value_server", symbol=symbol.upper())
        _exchanges = exchanges or [Exchange.BINANCE_SPOT]
        self._svc = FairValueService(
            symbol=symbol,
            exchanges=_exchanges,
            session=session,
            cfg=cfg.pricing_engine,
            lg=self.lg,
            proxy=proxy,
        )
        # Register the on_state callback as a book listener on every exchange
        for ex in _exchanges:
            self._svc.register_book_listener(ex, self._make_listener(ex, on_state))

    def _make_listener(
        self, exchange: Exchange, on_state: Callable[[FairPriceState], None]
    ) -> Callable[[object], None]:
        def _listener(_snap: object) -> None:
            state = self._svc.get_fair_price(exchange)
            if state is not None:
                on_state(state)

        return _listener

    async def run(self) -> None:
        self.lg.info("fair_value_server_start")
        await self._svc.run()
