"""
Bybit V5 Spot public trade stream consumer.

WS endpoint: wss://stream.bybit.com/v5/public/spot
Topic: publicTrade.<SYMBOL>  (e.g. publicTrade.BTCUSDT)

Message fields (per-item in data[]):
    T : trade time, ms
    p : price (string)
    v : qty (string)
    S : "Buy" | "Sell"  — taker side (aggressor)
        "Buy"  ⇒ taker bought  ⇒ aggressor is BUY
        "Sell" ⇒ taker sold    ⇒ aggressor is SELL
    i : trade id (string)

No sequence concerns — trades are append-only; reconnect on error is sufficient.
Bybit closes the connection after ~30s silence; keep-alive via manual ping every 20s.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import structlog
import websockets
import websockets.exceptions

from biz.domain.order import OrderSide
from biz.domain.trade import TradeTick
from biz.repo.trade import TradeStreamRepo
from pkg import exapi
from pkg.symbol import bybit_spot_into_external

_HEARTBEAT_TIMEOUT = 25.0   # s — must survive 20s ping interval with no trades
_PING_INTERVAL = 20.0
_MAX_BACKOFF = 5.0


class BybitSpotTradeTracker(TradeStreamRepo):

    def __init__(
        self,
        symbol: str,
        lg: structlog.stdlib.BoundLogger,
        on_trade: Callable[[TradeTick], None],
        testnet: bool = False,
        proxy: str | None = None,
    ) -> None:
        self._symbol = symbol.upper()
        self._ext_symbol = bybit_spot_into_external(symbol)
        self.lg = lg.bind(venue="bybit", symbol=self._symbol, component="trade")
        self._on_trade = on_trade
        self._proxy = proxy
        api = exapi.BYBIT_SPOT
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
                self.lg.warning("trade_session_error", error=str(exc))
                await asyncio.sleep(min(backoff, _MAX_BACKOFF))
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def stop(self) -> None:
        self._stop.set()

    async def _run_session(self) -> None:
        async with websockets.connect(
            self._ws_url, ping_interval=None, proxy=self._proxy,
        ) as ws:
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [f"publicTrade.{self._ext_symbol}"],
            }))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                await self._recv_loop(ws)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

    async def _recv_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_TIMEOUT)
            except TimeoutError as exc:
                raise RuntimeError("bybit trade heartbeat timeout") from exc

            msg = json.loads(raw)

            if "op" in msg or msg.get("type") == "pong":
                continue

            if not msg.get("topic", "").startswith("publicTrade"):
                continue

            recv_ts = time.monotonic_ns()
            for item in msg.get("data", []):
                tick = self._parse_item(item, recv_ts)
                if tick is not None:
                    self._on_trade(tick)

    def _parse_item(self, item: dict[str, Any], recv_ts: int) -> TradeTick | None:
        try:
            price = float(item["p"])
            qty = float(item["v"])
            event_ts = int(item["T"])
            side = OrderSide.BUY if item["S"] == "Buy" else OrderSide.SELL
        except (KeyError, ValueError, TypeError):
            return None
        return TradeTick(
            symbol=self._symbol,
            venue="bybit_spot",
            price=price,
            qty=qty,
            side=side,
            event_ts=event_ts,
            recv_ts=recv_ts,
        )

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            try:
                await ws.send(json.dumps({"op": "ping"}))
            except Exception:
                return
