"""
Bybit V5 Spot L2 OB tracker.

Sync protocol:
  1. Subscribe to orderbook.50.<SYMBOL> on V5 public/spot WS.
  2. First message type=="snapshot": apply as full book.
  3. Subsequent type=="delta": apply diff; data.u must increment by exactly 1.
  4. Gap or dirty state: unsubscribe/reconnect, await next snapshot.

Bybit requires a ping every 20s (server closes after ~30s silence).
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

from biz.domain.book import OrderBookSnapshot
from biz.repo.orderbook import OrderBookRepo
from data.orderbook.base import LocalOrderBook
from pkg import metrics
from pkg.symbol import bybit_spot_into_external

_WS_URL = "wss://stream.bybit.com/v5/public/spot"
_HEARTBEAT_TIMEOUT = 3.0
_PING_INTERVAL = 20.0
_MAX_BACKOFF = 5.0


class SequenceError(Exception):
    pass


class BybitSpotOrderBookTracker(OrderBookRepo):

    def __init__(
        self,
        symbol: str,
        lg: structlog.stdlib.BoundLogger,
        depth: int = 50,
        on_update: Callable[[OrderBookSnapshot], None] | None = None,
        top_k: int = 20,
    ) -> None:
        self._symbol = symbol.upper()                         # internal: BTC_USDT
        self._ext_symbol = bybit_spot_into_external(symbol)  # wire: BTCUSDT
        self.lg = lg.bind(venue="bybit", symbol=self._symbol)
        self._depth = depth
        self._on_update = on_update
        self._top_k = top_k
        self._book = LocalOrderBook(self._symbol, "bybit_spot")
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # OrderBookRepo interface
    # ------------------------------------------------------------------

    def snapshot(self, k: int = 20) -> OrderBookSnapshot:
        return self._book.snapshot(k)

    def is_fresh(self, max_age_ms: float = 500.0) -> bool:
        return self._book.is_fresh(max_age_ms)

    def seq(self) -> int:
        return self._book.seq

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        backoff = 0.1
        while not self._stop.is_set():
            try:
                await self._run_session()
                backoff = 0.1
            except asyncio.CancelledError:
                raise
            except SequenceError as exc:
                metrics.inc("ob_seq_gap_total", venue="bybit", symbol=self._symbol)
                self.lg.warning("ob_seq_gap", error=str(exc))
                self._book.mark_dirty()
                await asyncio.sleep(min(backoff, _MAX_BACKOFF))
                backoff = min(backoff * 2, _MAX_BACKOFF)
            except Exception as exc:
                self.lg.warning("ob_session_error", error=str(exc))
                self._book.mark_dirty()
                await asyncio.sleep(min(backoff, _MAX_BACKOFF))
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Session: one WS connection lifetime
    # ------------------------------------------------------------------

    async def _run_session(self) -> None:
        async with websockets.connect(_WS_URL, ping_interval=None) as ws:
            # Subscribe
            sub_msg = json.dumps({
                "op": "subscribe",
                "args": [f"orderbook.{self._depth}.{self._ext_symbol}"],
            })
            await ws.send(sub_msg)

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
        prev_u: int = -1
        snapshot_received = False

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_TIMEOUT)
            recv_ts = time.monotonic_ns()
            msg = json.loads(raw)

            # Bybit pong / op responses
            if "op" in msg or msg.get("type") == "pong":
                continue

            topic: str = msg.get("topic", "")
            if not topic.startswith("orderbook"):
                continue

            msg_type: str = msg.get("type", "")
            data: dict[str, Any] = msg.get("data", {})
            ts: int = msg.get("ts", 0)

            bids = [(float(p), float(q)) for p, q in data.get("b", [])]
            asks = [(float(p), float(q)) for p, q in data.get("a", [])]
            u: int = data.get("u", -1)

            if msg_type == "snapshot":
                self._book.apply_snapshot(
                    bids, asks,
                    seq=u,
                    event_ts=ts,
                    recv_ts=recv_ts,
                    send_ts=ts,
                )
                prev_u = u
                snapshot_received = True

            elif msg_type == "delta":
                if not snapshot_received:
                    # Delta before snapshot — discard; wait for next snapshot
                    continue

                if u == -1:
                    # Malformed delta — force resync
                    raise SequenceError("delta missing u field")

                if u != prev_u + 1:
                    raise SequenceError(
                        f"seq gap: expected u={prev_u + 1}, got u={u}"
                    )

                self._book.apply_diff(
                    bids, asks,
                    seq=u,
                    event_ts=ts,
                    recv_ts=recv_ts,
                    send_ts=ts,
                )
                prev_u = u

            else:
                continue

            age_ms = (recv_ts - ts * 1_000_000) / 1e6 if ts else 0
            metrics.gauge("ws_recv_lag_ms", age_ms, venue="bybit", symbol=self._symbol)

            if self._on_update and snapshot_received:
                self._on_update(self._book.snapshot(self._top_k))

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            try:
                await ws.send(json.dumps({"op": "ping"}))
            except Exception:
                return
