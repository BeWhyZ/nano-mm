from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

import aiohttp
import structlog

from biz.domain.book import OrderBookSnapshot
from pkg.constant import Exchange


class OrderBookRepo(ABC):

    @abstractmethod
    def snapshot(self, k: int = 20) -> OrderBookSnapshot:
        """Return top-k levels on each side. Raises if OB is not yet synced."""

    @abstractmethod
    def is_fresh(self, max_age_ms: float = 500.0) -> bool:
        """True if OB is synced and last update is within max_age_ms."""

    @abstractmethod
    def seq(self) -> int:
        """Last applied update id. -1 if not yet synced."""

    @abstractmethod
    async def run(self) -> None:
        """Connect, stream, and block until stopped or cancelled."""

    @abstractmethod
    def stop(self) -> None:
        """Signal the run loop to exit cleanly."""


def make_orderbook_tracker(
    exchange: Exchange,
    symbol: str,
    session: aiohttp.ClientSession,
    on_update: Callable[[OrderBookSnapshot], None],
    lg: structlog.stdlib.BoundLogger,
    proxy: str | None = None,
) -> OrderBookRepo:
    match exchange:
        case Exchange.BINANCE_SPOT:
            from data.orderbook.binance_spot import BinanceSpotOrderBookTracker

            return BinanceSpotOrderBookTracker(
                symbol=symbol,
                session=session,
                lg=lg,
                on_update=on_update,
                proxy=proxy,
            )
        case Exchange.BYBIT_SPOT:
            from data.orderbook.bybit_spot import BybitSpotOrderBookTracker

            return BybitSpotOrderBookTracker(
                symbol=symbol,
                lg=lg,
                on_update=on_update,
                proxy=proxy,
            )
        case _:
            raise ValueError(f"No orderbook tracker for exchange: {exchange!r}")
