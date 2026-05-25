"""Fixed-bin latency histogram for in-process telemetry.

Accumulates per-metric microsecond samples; exports p50/p95/p99/max/count
as row dicts every minute for Parquet archival.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Bin edges in microseconds: 0–10ms at 10µs resolution, then exponential up to 10s.
_EDGES_US: np.ndarray = np.unique(
    np.concatenate([
        np.arange(0, 10_000, 10),        # 0–10ms, 10µs steps
        np.arange(10_000, 100_000, 100),  # 10–100ms, 100µs steps
        np.arange(100_000, 10_000_001, 10_000),  # 100ms–10s, 10ms steps
    ])
).astype(np.float64)


@dataclass
class _MetricBins:
    counts: np.ndarray = field(default_factory=lambda: np.zeros(len(_EDGES_US), dtype=np.int64))
    max_us: float = 0.0

    def observe(self, us: float) -> None:
        idx = int(np.searchsorted(_EDGES_US, us, side="right")) - 1
        idx = max(0, min(idx, len(self.counts) - 1))
        self.counts[idx] += 1
        if us > self.max_us:
            self.max_us = us

    def percentile(self, pct: float) -> float:
        total = self.counts.sum()
        if total == 0:
            return 0.0
        target = total * pct / 100.0
        cumsum = np.cumsum(self.counts)
        idx = int(np.searchsorted(cumsum, target))
        idx = min(idx, len(_EDGES_US) - 1)
        return float(_EDGES_US[idx])

    def reset(self) -> "_MetricBins":
        snapshot = _MetricBins(counts=self.counts.copy(), max_us=self.max_us)
        self.counts[:] = 0
        self.max_us = 0.0
        return snapshot


class LatencyHistogram:
    """Thread-safe (GIL-level) in-process histogram store.

    observe() is hot-path safe. dump_and_reset() is called once per minute by
    the archive background task and returns a list of row dicts ready for Parquet.
    """

    def __init__(self) -> None:
        self._bins: defaultdict[str, _MetricBins] = defaultdict(_MetricBins)

    def observe(self, metric: str, us: float) -> None:
        self._bins[metric].observe(us)

    def dump_and_reset(self, session_id: str, minute_ts_ns: int | None = None) -> list[dict[str, Any]]:
        ts = minute_ts_ns if minute_ts_ns is not None else time.time_ns()
        rows: list[dict[str, Any]] = []
        for metric, bins in list(self._bins.items()):
            snapshot = bins.reset()
            count = int(snapshot.counts.sum())
            if count == 0:
                continue
            rows.append({
                "session_id": session_id,
                "minute_ts_ns": ts,
                "metric_name": metric,
                "count": count,
                "p50_us": snapshot.percentile(50),
                "p95_us": snapshot.percentile(95),
                "p99_us": snapshot.percentile(99),
                "max_us": snapshot.max_us,
                "schema_version": 1,
            })
        return rows
