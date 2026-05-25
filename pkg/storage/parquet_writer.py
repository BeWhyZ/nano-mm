"""Async-safe Parquet writer with hourly partitioned output.

Hot path: put_nowait(table, row) — enqueues immediately, never blocks.
Background task batches and writes snappy-compressed Parquet files under:
    base_dir/{table}/dt=YYYY-MM-DD/hour=HH/part-NNNN.parquet

Rows without a ts_ns or recv_ts_ns field use the current wall clock for
partition key calculation (they still land in the right hourly partition).

Usage:
    writer = AsyncParquetWriter(base_dir)
    await writer.start()
    writer.put_nowait("quote_snapshots", {"ts_ns": ..., "mid": ...})
    await writer.flush()
    await writer.stop()
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


class AsyncParquetWriter:

    def __init__(
        self,
        base_dir: Path | str,
        batch_size: int = 1000,
        flush_interval_s: float = 5.0,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        # table → list of pending rows (drained from queue before flush)
        self._buffers: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        # (table, dt_str, hour_str) → file sequence counter
        self._seq: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._task = asyncio.create_task(self._drain_loop(), name="parquet_drain")

    def put_nowait(self, table: str, row: dict[str, Any]) -> None:
        self._queue.put_nowait((table, row))

    async def flush(self) -> None:
        # Move everything from the queue into per-table buffers.
        while not self._queue.empty():
            try:
                table, row = self._queue.get_nowait()
                self._buffers[table].append(row)
            except asyncio.QueueEmpty:
                break

        non_empty = {k: v for k, v in self._buffers.items() if v}
        if not non_empty:
            return

        # Snapshot and clear under asyncio (single-threaded — no lock needed).
        buffers_snapshot = {k: list(v) for k, v in non_empty.items()}
        for k in non_empty:
            self._buffers[k].clear()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_buffers, buffers_snapshot)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()

    def _write_buffers(self, buffers: dict[str, list[dict[str, Any]]]) -> None:
        for table, rows in buffers.items():
            if not rows:
                continue
            # Group rows by (dt, hour) partition.
            partitions: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                ts_ns = row.get("ts_ns") or row.get("recv_ts_ns") or int(time.time_ns())
                dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
                key = (dt.strftime("%Y-%m-%d"), f"{dt.hour:02d}")
                partitions[key].append(row)

            for (date_str, hour_str), part_rows in partitions.items():
                part_dir = (
                    self._base_dir / table
                    / f"dt={date_str}"
                    / f"hour={hour_str}"
                )
                part_dir.mkdir(parents=True, exist_ok=True)

                seq_key = f"{table}/{date_str}/{hour_str}"
                seq = self._seq.get(seq_key, 0)
                self._seq[seq_key] = seq + 1

                path = part_dir / f"part-{seq:04d}.parquet"
                tmp_path = path.with_suffix(".tmp")

                tbl = pa.Table.from_pylist(part_rows)
                pq.write_table(tbl, tmp_path, compression="snappy")
                tmp_path.rename(path)

    async def _drain_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval_s)
            await self.flush()
