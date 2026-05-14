"""
Binance Spot aggTrade stream consumer.

WS endpoint: <symbol>@aggTrade (per-trade aggregated by aggressive taker order).
Message fields (subset):
    p : price (string)
    q : quantity (string)
    T : trade time, ms
    m : True iff the buyer is the maker
        → True  ⇒ seller is taker ⇒ aggressor is SELL
        → False ⇒ buyer is taker ⇒ aggressor is BUY

This module has no snapshot/sequence concerns — trades are append-only.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import structlog
import websockets
import websockets.exceptions

from biz.domain.order import OrderSide
from biz.domain.trade import TradeTick
from biz.repo.trade import TradeStreamRepo
from pkg import exapi
from pkg.symbol import binance_spot_into_external

_HEARTBEAT_TIMEOUT = 30.0   # s — quiet symbols can go 10+ seconds with no trades
_MAX_BACKOFF = 5.0


class BinanceSpotAggTradeTracker(TradeStreamRepo):

    def __init__(
        self,
        symbol: str,
        lg: structlog.stdlib.BoundLogger,
        on_trade: Callable[[TradeTick], None],
        testnet: bool = False,
        proxy: str | None = None,
    ) -> None:
        self._symbol = symbol.upper()
        self._ext_symbol = binance_spot_into_external(symbol)
        self.lg = lg.bind(venue="binance", symbol=self._symbol, component="agg_trade")
        self._on_trade = on_trade
        self._proxy = proxy
        api = exapi.BINANCE_SPOT
        self._ws_url = api.ws_testnet if testnet else api.ws
        self._stop = asyncio.Event()

    async def run(self) -> None:
        backoff = 0.1
        while not self._stop.is_set():
            try:
                await self._run_session()
                backoff = 0.1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.lg.warning("agg_trade_session_error", error=str(exc))
                await asyncio.sleep(min(backoff, _MAX_BACKOFF))
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def stop(self) -> None:
        self._stop.set()

    async def _run_session(self) -> None:
        ws_url = f"{self._ws_url}?streams={self._ext_symbol.lower()}@aggTrade"
        async with websockets.connect(
            ws_url, ping_interval=20, ping_timeout=10, proxy=self._proxy,
        ) as ws:
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_TIMEOUT)
                except TimeoutError as exc:
                    raise RuntimeError("agg_trade heartbeat timeout") from exc

                tick = self._parse(raw)
                if tick is not None:
                    self._on_trade(tick)

    def _parse(self, raw: str | bytes) -> TradeTick | None:
        envelope = json.loads(raw)
        data = envelope.get("data", envelope)
        if data.get("e") != "aggTrade":
            return None
        try:
            price = float(data["p"])
            qty = float(data["q"])
            event_ts = int(data["T"])
            is_buyer_maker = bool(data["m"])
        except (KeyError, ValueError, TypeError):
            return None
        side = OrderSide.SELL if is_buyer_maker else OrderSide.BUY
        return TradeTick(
            symbol=self._symbol,
            venue="binance_spot",
            price=price,
            qty=qty,
            side=side,
            event_ts=event_ts,
            recv_ts=time.monotonic_ns(),
        )
