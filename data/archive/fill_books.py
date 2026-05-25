"""Persist top-20 OB snapshot triggered by a fill to Parquet fill_books."""
from __future__ import annotations

import time

from biz.domain.book import OrderBookSnapshot
from pkg.storage.parquet_writer import AsyncParquetWriter

_TABLE = "fill_books"


def write_fill_book(
    writer: AsyncParquetWriter,
    trade_id: str,
    snap: OrderBookSnapshot,
    session_id: str,
) -> None:
    writer.put_nowait(_TABLE, {
        "ts_ns": time.time_ns(),
        "session_id": session_id,
        "trade_id": trade_id,
        "venue": snap.venue,
        "book_seq": snap.seq,
        "bids": [{"price": lv.price, "size": lv.qty} for lv in snap.bids[:20]],
        "asks": [{"price": lv.price, "size": lv.qty} for lv in snap.asks[:20]],
        "schema_version": 1,
    })
