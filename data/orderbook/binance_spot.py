"""
Binance Spot L2 OB tracker.

Sync protocol:
  1. Open WS, buffer all diffs.
  2. After WS is stable, REST-fetch snapshot (concurrent with buffering).
  3. Discard buffered events where u <= snapshot.lastUpdateId.
  4. First applied event: U <= lastUpdateId+1 AND u >= lastUpdateId+1.
  5. Subsequent events: U == prev.u + 1.
  6. Any sequence gap → SequenceError → outer loop re-syncs.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import aiohttp
import structlog
import websockets
import websockets.exceptions

from biz.domain.book import OrderBookSnapshot
from biz.repo.orderbook import OrderBookRepo
from data.orderbook.base import LocalOrderBook
from pkg import exapi, metrics
from pkg.symbol import binance_spot_into_external

_HEARTBEAT_TIMEOUT = 3.0   # s: no message → assume dead
_SNAPSHOT_LIMIT = 1000     # REST snapshot depth
_MAX_BACKOFF = 5.0         # s


class SequenceError(Exception):
    pass


class BinanceSpotOrderBookTracker(OrderBookRepo):

    def __init__(
        self,
        symbol: str,
        session: aiohttp.ClientSession,
        lg: structlog.stdlib.BoundLogger,
        on_update: Callable[[OrderBookSnapshot], None] | None = None,
        top_k: int = 20,
        testnet: bool = False,
        proxy: str | None = None,
    ) -> None:
        self._symbol = symbol.upper()                          # internal: BTC_USDT
        self._ext_symbol = binance_spot_into_external(symbol)  # wire: BTCUSDT
        self.lg = lg.bind(venue="binance", symbol=self._symbol)
        self._session = session
        self._on_update = on_update
        self._top_k = top_k
        self._proxy = proxy
        api = exapi.BINANCE_SPOT
        self._rest_url = api.rest_testnet if testnet else api.rest
        self._ws_url = api.ws_testnet if testnet else api.ws
        self._book = LocalOrderBook(self._symbol, "binance_spot")
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
        fail_count = 0

        while not self._stop.is_set():
            try:
                await self._run_session()
                fail_count = 0
                backoff = 0.1
            except asyncio.CancelledError:
                raise
            except SequenceError as exc:
                metrics.inc("ob_seq_gap_total", venue="binance", symbol=self._symbol)
                self.lg.warning("ob_seq_gap", error=str(exc))
                self._book.mark_dirty()
                fail_count += 1
                await asyncio.sleep(min(backoff, _MAX_BACKOFF))
                backoff = min(backoff * 2, _MAX_BACKOFF)
            except Exception as exc:
                fail_count += 1
                self.lg.warning("ob_session_error", error=str(exc), attempt=fail_count)
                self._book.mark_dirty()
                await asyncio.sleep(min(backoff, _MAX_BACKOFF))
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Session: one WS connection lifetime
    # ------------------------------------------------------------------

    async def _run_session(self) -> None:
        ws_url = f"{self._ws_url}?streams={self._ext_symbol.lower()}@depth@100ms"
        buffer: list[dict[str, Any]] = []

        async with websockets.connect(
            ws_url, ping_interval=20, ping_timeout=10, proxy=self._proxy,
        ) as ws:
            # Phase 1: buffer events AND fetch snapshot concurrently
            snapshot_task = asyncio.create_task(self._fetch_snapshot())
            try:
                while not snapshot_task.done():
                    raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_TIMEOUT)
                    msg = self._parse_stream(raw)
                    if msg:
                        buffer.append(msg)
            except asyncio.TimeoutError:
                snapshot_task.cancel()
                raise RuntimeError("heartbeat timeout during snapshot fetch")

            snapshot = await snapshot_task
            last_update_id: int = snapshot["lastUpdateId"]
            recv_ts = time.monotonic_ns()

            # Apply snapshot to local book
            bids = [(float(p), float(q)) for p, q in snapshot["bids"]]
            asks = [(float(p), float(q)) for p, q in snapshot["asks"]]
            self._book.apply_snapshot(
                bids, asks,
                seq=last_update_id,
                event_ts=0,
                recv_ts=recv_ts,
            )

            # Phase 2: drain buffer with sequence validation
            prev_u = last_update_id
            first_found = False

            for msg in buffer:
                U: int = msg["U"]
                u: int = msg["u"]

                if not first_found:
                    if u < last_update_id + 1:
                        continue  # entirely covered by snapshot
                    if U > last_update_id + 1:
                        raise SequenceError(
                            f"buffer gap: snapshot.lastUpdateId={last_update_id}, "
                            f"first applicable event U={U}"
                        )
                    first_found = True
                else:
                    if U != prev_u + 1:
                        raise SequenceError(
                            f"buffer seq gap: expected U={prev_u + 1}, got U={U}"
                        )

                self._apply_msg(msg)
                prev_u = u

            # Phase 3: live stream
            async for raw in ws:
                msg = self._parse_stream(raw)
                if not msg:
                    continue

                U = msg["U"]
                u = msg["u"]

                if not first_found:
                    # Rare: buffer was empty, first event arrives here
                    if u < last_update_id + 1:
                        continue
                    if U > last_update_id + 1:
                        raise SequenceError(
                            f"live gap after snapshot: expected U<={last_update_id + 1}, got U={U}"
                        )
                    first_found = True
                elif U != prev_u + 1:
                    raise SequenceError(
                        f"live seq gap: expected U={prev_u + 1}, got U={U}"
                    )

                self._apply_msg(msg)
                prev_u = u

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_snapshot(self) -> dict[str, Any]:
        params = {"symbol": self._ext_symbol, "limit": _SNAPSHOT_LIMIT}
        return await exapi.get(
            self._session, self._rest_url, "/api/v3/depth", params, proxy=self._proxy,
        )

    def _parse_stream(self, raw: str | bytes) -> dict[str, Any] | None:
        import json
        envelope = json.loads(raw)
        # Combined stream format: {"stream": "...", "data": {...}}
        data = envelope.get("data", envelope)
        if data.get("e") != "depthUpdate":
            return None
        return data

    def _apply_msg(self, msg: dict[str, Any]) -> None:
        recv_ts = time.monotonic_ns()
        event_ts: int = msg.get("E", 0)
        u: int = msg["u"]

        bids = [(float(p), float(q)) for p, q in msg.get("b", [])]
        asks = [(float(p), float(q)) for p, q in msg.get("a", [])]

        self._book.apply_diff(
            bids, asks,
            seq=u,
            event_ts=event_ts,
            recv_ts=recv_ts,
        )

        age_ms = (recv_ts - event_ts * 1_000_000) / 1e6 if event_ts else 0
        metrics.gauge("ws_recv_lag_ms", age_ms, venue="binance", symbol=self._symbol)

        if self._on_update:
            self._on_update(self._book.snapshot(self._top_k))
