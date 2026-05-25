"""
MMServer: thin orchestrator around MMService.

Usage:
    server = MMServer(symbol, session, cfg, on_quote=my_callback, lg=lg)
    await server.run()
"""
from __future__ import annotations

from collections.abc import Callable

import aiohttp
import structlog

from biz.domain.quote import QuoteState
from biz.repo.archive import ArchiveRepo
from config import Config
from pkg.constant import Exchange
from service.mm_service import MMService


class MMServer:
    """
    Composes MMService into a single runnable unit.

    on_quote fires on every L2 tick that produces a QuoteState.
    """

    def __init__(
        self,
        symbol: str,
        session: aiohttp.ClientSession,
        cfg: Config,
        on_quote: Callable[[QuoteState], None],
        lg: structlog.stdlib.BoundLogger,
        exchange: Exchange | None = None,
        reference_exchange: Exchange | None = None,
        archive: ArchiveRepo | None = None,
        proxy: str | None = None,
    ) -> None:
        self.lg = lg.bind(component="mm_server", symbol=symbol.upper())
        _exchange = exchange or Exchange(cfg.venues.target)
        _ref_exchange = reference_exchange or Exchange(cfg.venues.reference)

        self._svc = MMService(
            symbol=symbol,
            exchange=_exchange,
            session=session,
            pricing_cfg=cfg.pricing_engine,
            spread_cfg=cfg.spread_engine,
            lg=self.lg,
            reference_exchange=_ref_exchange if _ref_exchange != _exchange else None,
            archive=archive,
            proxy=proxy,
        )
        self._svc.register_quote_listener(on_quote)

    def set_inventory(self, q_norm: float) -> None:
        self._svc.set_inventory(q_norm)

    async def run(self) -> None:
        self.lg.info("mm_server_start")
        await self._svc.run()
