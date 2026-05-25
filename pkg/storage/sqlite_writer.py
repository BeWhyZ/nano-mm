"""Async-safe SQLite writer.

Hot path: put_nowait(sql, params) — enqueues immediately, never blocks.
Background asyncio task drains, batches, and writes via thread executor.

Usage:
    writer = AsyncSqliteWriter(db_path, init_sqls=[...])
    await writer.start()
    writer.put_nowait("INSERT INTO ...", (val1, val2))
    await writer.flush()  # force flush
    await writer.stop()   # flush + stop background task
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any


class AsyncSqliteWriter:

    def __init__(
        self,
        db_path: Path | str,
        batch_size: int = 100,
        flush_interval_s: float = 1.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: asyncio.Queue[tuple[str, tuple[Any, ...]]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self, init_sqls: list[str] | None = None) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._init_db, init_sqls or [])
        self._running = True
        self._task = asyncio.create_task(self._drain_loop(), name="sqlite_drain")

    def put_nowait(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._queue.put_nowait((sql, params))

    async def flush(self) -> None:
        batch: list[tuple[str, tuple[Any, ...]]] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._write_batch, batch)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()

    def _init_db(self, init_sqls: list[str]) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=OFF")
            for sql in init_sqls:
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def _write_batch(self, batch: list[tuple[str, tuple[Any, ...]]]) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            for sql, params in batch:
                try:
                    conn.execute(sql, params)
                except sqlite3.IntegrityError:
                    # duplicate PK — idempotent, skip silently
                    pass
            conn.commit()
        finally:
            conn.close()

    async def _drain_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval_s)
            await self.flush()
