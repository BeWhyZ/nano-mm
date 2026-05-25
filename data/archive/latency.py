"""Latency histogram integration: observe samples, dump to Parquet every minute."""
from __future__ import annotations

import asyncio
import time

from pkg.storage.histogram import LatencyHistogram
from pkg.storage.parquet_writer import AsyncParquetWriter

_TABLE = "latency_histograms"


class LatencyArchive:
    """Wraps LatencyHistogram and periodically dumps to Parquet."""

    def __init__(
        self,
        writer: AsyncParquetWriter,
        session_id: str,
        dump_interval_s: float = 60.0,
    ) -> None:
        self._writer = writer
        self._session_id = session_id
        self._interval = dump_interval_s
        self._hist = LatencyHistogram()
        self._task: asyncio.Task[None] | None = None

    def observe(self, metric: str, us: float) -> None:
        self._hist.observe(metric, us)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._dump_loop(), name="latency_dump")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._flush()

    def _flush(self) -> None:
        rows = self._hist.dump_and_reset(self._session_id, time.time_ns())
        for row in rows:
            self._writer.put_nowait(_TABLE, row)

    async def _dump_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self._flush()
