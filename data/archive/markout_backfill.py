"""Background task: backfill markout columns on fills table.

Runs every backfill_interval_s. Finds fills WHERE markout_60s IS NULL AND
recv_ts_ns < now - 65s (all sessions). For each fill and each horizon τ,
queries mid_tape Parquet for the median reference mid in a ±50ms window
around fill_ts + τ. Updates fills in-place.

Robust across process restarts: no session_id filter, so orphaned fills
from prior sessions are also backfilled on startup.

Threading notes
---------------
_backfill() runs in a ThreadPoolExecutor via run_in_executor.

⚠️  Never use the module-level duckdb.sql() shortcut here — it shares a
    process-level (or thread-local, version-dependent) default connection
    that is NOT safe to call from a thread pool.  Always open an explicit
    duckdb.connect() inside the worker and close it before returning.

Query design
------------
Instead of N×4 individual Parquet scans (one per fill × horizon), we build
an in-memory DuckDB temp table with all required windows and do a single
Parquet scan via JOIN.  This cuts disk I/O from O(N·4) full table scans
down to O(1) per sweep.
"""
from __future__ import annotations

import asyncio
import glob as _glob
import sqlite3
import time
from pathlib import Path

import duckdb


_HORIZONS: list[tuple[str, int]] = [
    ("1s", 1),
    ("5s", 5),
    ("30s", 30),
    ("60s", 60),
]
# Wait 65s after recv_ts before backfilling (to ensure mid_tape has data for t+60s).
_MIN_AGE_NS = 65 * 10**9
_WINDOW_NS = 50_000_000  # ±50 ms


class MarkoutBackfillTask:

    def __init__(
        self,
        db_path: Path,
        parquet_base: Path,
        reference_venue: str,
        backfill_interval_s: float = 30.0,
    ) -> None:
        self._db_path = db_path
        self._mid_tape_glob = str(parquet_base / "mid_tape" / "**" / "*.parquet")
        self._ref_venue = reference_venue
        self._interval = backfill_interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # Run one sweep immediately to catch orphans from prior sessions.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._backfill)
        self._task = asyncio.create_task(self._loop(), name="markout_backfill")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._interval)
            await loop.run_in_executor(None, self._backfill)

    # ------------------------------------------------------------------
    # Synchronous worker — runs in ThreadPoolExecutor, NOT on event loop.
    # ------------------------------------------------------------------

    def _backfill(self) -> None:
        cutoff_ns = time.time_ns() - _MIN_AGE_NS
        sq = sqlite3.connect(str(self._db_path))
        try:
            rows = sq.execute(
                "SELECT trade_id, recv_ts_ns, price, side, mid_ref_at_fill"
                " FROM fills"
                " WHERE markout_60s IS NULL AND recv_ts_ns < ?",
                (cutoff_ns,),
            ).fetchall()

            if not rows:
                return

            # Guard: if no Parquet files exist yet, skip silently.
            parquet_files = _glob.glob(self._mid_tape_glob, recursive=True)
            if not parquet_files:
                return

            # ----------------------------------------------------------
            # Build window list: (trade_id, horizon_label, lo_ns, hi_ns)
            # ----------------------------------------------------------
            windows: list[tuple[str, str, int, int]] = []
            # meta: trade_id → (price, side_sign, mid_ref_at_emit)
            meta: dict[str, tuple[float, float, float | None]] = {}

            for trade_id, recv_ts_ns, price, side, mid_ref_at_fill in rows:
                side_sign = 1.0 if side == "buy" else -1.0
                meta[trade_id] = (float(price), side_sign, mid_ref_at_fill)
                for label, horizon_s in _HORIZONS:
                    target_ns = recv_ts_ns + horizon_s * 10**9
                    windows.append((
                        trade_id,
                        label,
                        target_ns - _WINDOW_NS,
                        target_ns + _WINDOW_NS,
                    ))

            # ----------------------------------------------------------
            # One DuckDB connection per _backfill() call.
            #
            # duckdb.connect() creates a fresh in-memory DB; it is fully
            # thread-safe when each thread uses its own connection object.
            # SET threads=1 prevents DuckDB from spawning its own thread
            # pool and competing with the asyncio event loop for CPU.
            # ----------------------------------------------------------
            duck = duckdb.connect()
            try:
                duck.execute("SET threads=1")

                # Load windows into a DuckDB temp table.
                duck.execute(
                    "CREATE TEMP TABLE _windows"
                    "(trade_id TEXT, horizon TEXT, lo_ns BIGINT, hi_ns BIGINT)"
                )
                duck.executemany("INSERT INTO _windows VALUES (?,?,?,?)", windows)

                # Single Parquet scan: pass file list as a parameter (safe,
                # no SQL injection risk, no string size limit issues).
                result_rows = duck.execute(
                    """
                    SELECT w.trade_id,
                           w.horizon,
                           median(m.mid)  AS mid_med,
                           count(*)       AS cnt
                    FROM   _windows AS w
                    JOIN   read_parquet($files, union_by_name=true) AS m
                      ON   m.role  = 'reference'
                     AND   m.ts_ns BETWEEN w.lo_ns AND w.hi_ns
                    GROUP  BY w.trade_id, w.horizon
                    """,
                    {"files": parquet_files},
                ).fetchall()
            finally:
                duck.close()

            # ----------------------------------------------------------
            # Re-assemble results into per-(trade_id, horizon) maps.
            # ----------------------------------------------------------
            mid_map: dict[str, dict[str, float | None]] = {
                t: {lbl: None for lbl, _ in _HORIZONS} for t, *_ in rows
            }
            cnt_map: dict[str, dict[str, int | None]] = {
                t: {lbl: None for lbl, _ in _HORIZONS} for t, *_ in rows
            }
            for trade_id, horizon, mid_med, cnt in result_rows:
                mid_map[trade_id][horizon] = float(mid_med) if mid_med is not None else None
                cnt_map[trade_id][horizon] = int(cnt) if cnt else None

            # Build UPDATE batch.
            updates: list[tuple] = []
            for trade_id, _recv_ts_ns, _price, _side, _mid_ref in rows:
                price_f, side_sign, mid_ref_at_emit = meta[trade_id]
                mv = mid_map[trade_id]
                cv = cnt_map[trade_id]

                def _markout(mid_τ: float | None, p: float = price_f, s: float = side_sign) -> float | None:
                    return (mid_τ - p) * s if mid_τ is not None else None

                def _markout_emit(mid_τ: float | None, ref: float | None = mid_ref_at_emit, s: float = side_sign) -> float | None:
                    if mid_τ is None or ref is None:
                        return None
                    return (mid_τ - ref) * s

                updates.append((
                    mv["1s"], mv["5s"], mv["30s"], mv["60s"],
                    cv["1s"], cv["5s"], cv["30s"], cv["60s"],
                    _markout(mv["1s"]),
                    _markout(mv["5s"]),
                    _markout(mv["30s"]),
                    _markout(mv["60s"]),
                    _markout_emit(mv["5s"]),
                    _markout_emit(mv["60s"]),
                    trade_id,
                ))

            if updates:
                sq.executemany(
                    """UPDATE fills SET
                       mid_ref_1s=?, mid_ref_5s=?, mid_ref_30s=?, mid_ref_60s=?,
                       mid_samples_count_1s=?, mid_samples_count_5s=?,
                       mid_samples_count_30s=?, mid_samples_count_60s=?,
                       markout_1s=?, markout_5s=?, markout_30s=?, markout_60s=?,
                       markout_from_emit_5s=?, markout_from_emit_60s=?
                       WHERE trade_id=?""",
                    updates,
                )
                sq.commit()
        finally:
            sq.close()
