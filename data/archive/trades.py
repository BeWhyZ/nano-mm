"""Serialize public TradeTick to Parquet trade_tape."""
from __future__ import annotations

import time
from typing import Literal

from biz.domain.trade import TradeTick
from pkg.storage.parquet_writer import AsyncParquetWriter

_TABLE = "trade_tape"


def write_trade_tick(
    writer: AsyncParquetWriter,
    tick: TradeTick,
    role: Literal["target", "reference"],
) -> None:
    writer.put_nowait(_TABLE, {
        "ts_ms": tick.event_ts,
        "recv_ts_ns": time.time_ns(),
        "symbol": tick.symbol,
        "venue": tick.venue,
        "role": role,
        "price": tick.price,
        "qty": tick.qty,
        "side": tick.side.value,
        "schema_version": 1,
    })
