"""Serialize mid-price samples to Parquet mid_tape (markout backfill source)."""
from __future__ import annotations

import time
from typing import Literal

from biz.domain.book import OrderBookSnapshot
from pkg.storage.parquet_writer import AsyncParquetWriter

_TABLE = "mid_tape"


def write_mid_sample(
    writer: AsyncParquetWriter,
    snap: OrderBookSnapshot,
    mid: float,
    micro: float | None,
    role: Literal["target", "reference"],
) -> None:
    best_bid = snap.best_bid
    best_ask = snap.best_ask
    writer.put_nowait(_TABLE, {
        "ts_ns": time.time_ns(),
        "symbol": snap.symbol,
        "venue": snap.venue,
        "role": role,
        "mid": mid,
        "micro": micro,
        "best_bid": best_bid.price if best_bid else None,
        "best_ask": best_ask.price if best_ask else None,
        "schema_version": 1,
    })
