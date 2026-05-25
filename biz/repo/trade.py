from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

import structlog

from biz.domain.trade import TradeTick
from pkg.constant import Exchange


class TradeStreamRepo(ABC):

    @abstractmethod
    async def run(self) -> None:
        """Connect, stream trades to the registered callback, reconnect on error."""

    @abstractmethod
    def stop(self) -> None:
        """Signal the run loop to exit gracefully."""


def make_trade_tracker(
    exchange: Exchange,
    symbol: str,
    on_trade: Callable[[TradeTick], None],
    lg: structlog.stdlib.BoundLogger,
    proxy: str | None = None,
) -> TradeStreamRepo:
    match exchange:
        case Exchange.BINANCE_SPOT:
            from data.trade.binance_spot import BinanceSpotAggTradeTracker

            return BinanceSpotAggTradeTracker(
                symbol=symbol, lg=lg, on_trade=on_trade, proxy=proxy,
            )
        case Exchange.BYBIT_SPOT:
            from data.trade.bybit_spot import BybitSpotTradeTracker

            return BybitSpotTradeTracker(
                symbol=symbol, lg=lg, on_trade=on_trade, proxy=proxy,
            )
        case _:
            raise ValueError(f"No trade tracker for exchange: {exchange!r}")
