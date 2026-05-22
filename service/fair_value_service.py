"""
FairValueService: manages orderbook subscriptions across multiple exchanges for a symbol
and exposes a clean fair-price interface for downstream consumers (e.g. GLT).

Usage:
    svc = FairValueService(symbol, [Exchange.BINANCE_SPOT], session, cfg.pricing_engine, lg)
    svc.register_book_listener(Exchange.BINANCE_SPOT, glt_server.on_book)
    await svc.run()   # runs until cancelled
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable

import aiohttp
import structlog

from biz.domain.book import OrderBookSnapshot
from biz.repo.orderbook import OrderBookRepo, make_orderbook_tracker
from biz.usecase.fair_value import FairPriceState, FairValueEngine
from config import PricingConfig
from pkg.constant import Exchange


class FairValueService:
    """
    Owns L2 orderbook subscriptions for one symbol across one or more exchanges.

    - The first exchange in *exchanges* is the primary (reference) venue.
    - Consumers can call get_fair_price() to read the latest FairPriceState.
    - Consumers can register book-update listeners to share the subscription
      without opening a duplicate WebSocket connection.
    """

    def __init__(
        self,
        symbol: str,
        exchanges: list[Exchange],
        session: aiohttp.ClientSession,
        cfg: PricingConfig,
        lg: structlog.stdlib.BoundLogger,
        proxy: str | None = None,
    ) -> None:
        if not exchanges:
            raise ValueError("exchanges must be non-empty")

        self._symbol = symbol.upper()
        self._primary = exchanges[0]
        self.lg = lg.bind(component="fair_value_service", symbol=self._symbol)

        self._engines: dict[Exchange, FairValueEngine] = {}
        self._trackers: dict[Exchange, OrderBookRepo] = {}
        self._book_listeners: defaultdict[Exchange, list[Callable[[OrderBookSnapshot], None]]] = (
            defaultdict(list)
        )

        for ex in exchanges:
            engine = FairValueEngine(symbol, lg=self.lg, micro_k=int(cfg.micro_k))
            self._engines[ex] = engine
            tracker = make_orderbook_tracker(
                exchange=ex,
                symbol=symbol,
                session=session,
                on_update=self._make_on_update(ex),
                lg=self.lg,
                proxy=proxy,
            )
            self._trackers[ex] = tracker

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_fair_price(self, exchange: Exchange | None = None) -> FairPriceState | None:
        """Return the latest FairPriceState from the primary (or specified) exchange."""
        ex = exchange if exchange is not None else self._primary
        engine = self._engines.get(ex)
        return engine.state if engine else None

    def register_book_listener(
        self,
        exchange: Exchange,
        cb: Callable[[OrderBookSnapshot], None],
    ) -> None:
        """
        Register *cb* to receive raw OrderBookSnapshot events from *exchange*.

        Avoids opening a second WebSocket for the same symbol/exchange pair.
        """
        self._book_listeners[exchange].append(cb)

    async def run(self) -> None:
        """Start all trackers concurrently. Runs until cancelled."""
        self.lg.info("fair_value_service_start", exchanges=[str(e) for e in self._trackers])
        await asyncio.gather(*[t.run() for t in self._trackers.values()])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_on_update(self, exchange: Exchange) -> Callable[[OrderBookSnapshot], None]:
        def _on_update(snap: OrderBookSnapshot) -> None:
            self._engines[exchange].on_tick(snap)
            for cb in self._book_listeners[exchange]:
                cb(snap)

        return _on_update
