"""Serialize QuoteState snapshots to Parquet."""
from __future__ import annotations

import time
from typing import Any

from biz.domain.quote import QuoteState
from pkg.storage.parquet_writer import AsyncParquetWriter

_TABLE = "quote_snapshots"


def write_quote_snapshot(
    writer: AsyncParquetWriter,
    state: QuoteState,
    target_mid: float,
    ref_mid: float,
    event_type: str,
    session_id: str,
) -> None:
    ts_ns = time.time_ns()
    inner_bid = state.bids[0] if state.bids else None
    inner_ask = state.asks[0] if state.asks else None

    row: dict[str, Any] = {
        "ts_ns": ts_ns,
        "session_id": session_id,
        "symbol": state.symbol,
        "target_venue": state.venue,
        "event_type": event_type,
        "mid_target": target_mid,
        "mid_ref": ref_mid,
        "sigma": state.sigma,
        "A": state.A,
        "k": state.k,
        "gamma": state.gamma,
        "q_norm": state.q_norm,
        "n_bids": len(state.bids),
        "n_asks": len(state.asks),
        "inner_bid_px": inner_bid.price if inner_bid else None,
        "inner_bid_size": inner_bid.size if inner_bid else None,
        "inner_ask_px": inner_ask.price if inner_ask else None,
        "inner_ask_size": inner_ask.size if inner_ask else None,
        "inner_spread_bps": (
            (inner_ask.price - inner_bid.price) / target_mid * 1e4
            if inner_bid and inner_ask and target_mid > 0
            else None
        ),
        # Full ladder only on actual re-quote events to save storage.
        "bids": (
            [{"price": q.price, "size": q.size} for q in state.bids]
            if event_type == "requote"
            else None
        ),
        "asks": (
            [{"price": q.price, "size": q.size} for q in state.asks]
            if event_type == "requote"
            else None
        ),
        "schema_version": 1,
    }
    writer.put_nowait(_TABLE, row)
