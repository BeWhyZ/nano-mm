"""Integration smoke: write → flush → read back via DuckDB and Polars."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import duckdb
import polars as pl
import pytest

from biz.domain.book import OrderBookSnapshot, PriceLevel
from biz.domain.order import Fill, Order, OrderSide, OrderStatus, OrderType
from biz.domain.quote import Quote, QuoteState
from biz.domain.trade import TradeTick
from biz.repo.archive import FillArchiveCtx, OrderArchiveCtx
from data.archive import ArchiveManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snap(symbol: str = "BTC_USDT", venue: str = "binance_spot") -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol=symbol,
        venue=venue,
        bids=[PriceLevel(50000.0 - i, 0.1 * (i + 1)) for i in range(10)],
        asks=[PriceLevel(50001.0 + i, 0.1 * (i + 1)) for i in range(10)],
        event_ts=int(time.time() * 1000),
        send_ts=0,
        recv_ts=time.monotonic_ns(),
        seq=100,
    )


def _make_trade(symbol: str = "BTC_USDT", venue: str = "binance_spot") -> TradeTick:
    return TradeTick(
        symbol=symbol,
        venue=venue,
        price=50000.5,
        qty=0.01,
        side=OrderSide.BUY,
        event_ts=int(time.time() * 1000),
        recv_ts=time.monotonic_ns(),
    )


def _make_quote_state(symbol: str = "BTC_USDT", venue: str = "binance_spot") -> QuoteState:
    return QuoteState(
        symbol=symbol,
        venue=venue,
        mid=50000.5,
        bids=(Quote(OrderSide.BUY, 49999.5, 0.001),),
        asks=(Quote(OrderSide.SELL, 50001.5, 0.001),),
        sigma=0.001,
        A=5.0,
        k=0.0005,
        gamma=0.1,
        q_norm=0.0,
        ts_ns=time.monotonic_ns(),
    )


def _make_order(coid: str = "c001") -> Order:
    return Order(
        client_order_id=coid,
        symbol="BTC_USDT",
        venue="binance_spot",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT_MAKER,
        price=49999.5,
        original_qty=0.001,
    )


def _make_fill(trade_id: str = "t001", coid: str = "c001") -> Fill:
    return Fill(
        trade_id=trade_id,
        order_id=coid,
        price=49999.5,
        qty=0.001,
        side=OrderSide.BUY,
        event_ts=int(time.time() * 1000),
        recv_ts=time.monotonic_ns(),
        fee=0.000001,
        fee_asset="BNB",
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
async def archive(tmp_path: Path) -> ArchiveManager:
    mgr = ArchiveManager(
        base_dir=tmp_path,
        symbol="BTC_USDT",
        target_venue="binance_spot",
        reference_venue="binance_spot",
        mode="paper",
        sqlite_flush_rows=1,        # flush every row for test determinism
        sqlite_flush_interval_s=0.1,
        parquet_flush_rows=1,
        parquet_flush_interval_s=0.1,
    )
    await mgr.start()
    yield mgr
    await mgr.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_row(archive: ArchiveManager, tmp_path: Path) -> None:
    await archive._sqlite.flush()
    db = sqlite3.connect(str(tmp_path / "sqlite" / "nano-mm.db"))
    rows = db.execute("SELECT session_id, mode, symbol FROM sessions").fetchall()
    db.close()
    assert len(rows) == 1
    assert rows[0][1] == "paper"
    assert rows[0][2] == "BTC_USDT"


@pytest.mark.asyncio
async def test_order_event_roundtrip(archive: ArchiveManager, tmp_path: Path) -> None:
    order = _make_order()
    ctx = OrderArchiveCtx(
        session_id=archive.session_id,
        ladder_level=0,
        quote_emit_ts_ns=time.monotonic_ns(),
        mid_target=50000.5,
        mid_ref=50000.5,
        sigma=0.001,
        A=5.0,
        k=0.0005,
        gamma=0.1,
        q_norm=0.0,
        queue_ahead=0.5,
        book_seq=100,
    )
    archive.write_order(order, ctx)
    archive.write_order_event(order, "ADD", seq=0)

    order.status = OrderStatus.OPEN
    order.exchange_order_id = "EX001"
    archive.write_order_event(order, "ACK", seq=1, is_maker=True)

    await archive._sqlite.flush()
    db = sqlite3.connect(str(tmp_path / "sqlite" / "nano-mm.db"))
    order_rows = db.execute("SELECT client_order_id, price FROM orders").fetchall()
    event_rows = db.execute(
        "SELECT event_type, status_after FROM order_events ORDER BY seq"
    ).fetchall()
    db.close()

    assert len(order_rows) == 1
    assert order_rows[0][0] == "c001"
    assert len(event_rows) == 2
    assert event_rows[0][0] == "ADD"
    assert event_rows[1][0] == "ACK"


@pytest.mark.asyncio
async def test_fill_roundtrip(archive: ArchiveManager, tmp_path: Path) -> None:
    fill = _make_fill()
    ctx = FillArchiveCtx(
        session_id=archive.session_id,
        mid_target=50000.5,
        mid_ref=50000.5,
        spread_bps=4.0,
        obi=0.1,
        micro_minus_mid_bps=0.5,
        book_seq=100,
        sigma=0.001,
        sigma_norm=0.002,
        q_norm=0.0,
        ladder_level=0,
        aggressor_imbalance_30s=0.05,
        quote_emit_ts_ns=time.time_ns() - 1_000_000,
        inventory_before=0.0,
        inventory_after=0.001,
        q_norm_after=0.01,
        realized_pnl_after=None,
        unrealized_pnl_at_fill=None,
        is_ghost_fill=False,
        is_maker=True,
    )
    archive.write_fill(fill, ctx)

    await archive._sqlite.flush()
    db = sqlite3.connect(str(tmp_path / "sqlite" / "nano-mm.db"))
    rows = db.execute(
        "SELECT trade_id, price, is_maker, is_ghost_fill FROM fills"
    ).fetchall()
    db.close()

    assert len(rows) == 1
    assert rows[0][0] == "t001"
    assert rows[0][2] == 1   # is_maker
    assert rows[0][3] == 0   # not ghost


@pytest.mark.asyncio
async def test_ghost_fill_flag(archive: ArchiveManager, tmp_path: Path) -> None:
    fill = _make_fill(trade_id="t_ghost")
    ctx = FillArchiveCtx(
        session_id=archive.session_id,
        mid_target=50000.5, mid_ref=50000.5,
        spread_bps=None, obi=None, micro_minus_mid_bps=None, book_seq=None,
        sigma=None, sigma_norm=None, q_norm=0.5, ladder_level=0,
        aggressor_imbalance_30s=None, quote_emit_ts_ns=time.time_ns(),
        inventory_before=0.001, inventory_after=0.002, q_norm_after=0.02,
        realized_pnl_after=None, unrealized_pnl_at_fill=None,
        is_ghost_fill=True, is_maker=True,
    )
    archive.write_fill(fill, ctx)
    await archive._sqlite.flush()

    db = sqlite3.connect(str(tmp_path / "sqlite" / "nano-mm.db"))
    row = db.execute("SELECT is_ghost_fill FROM fills WHERE trade_id='t_ghost'").fetchone()
    db.close()
    assert row is not None and row[0] == 1


@pytest.mark.asyncio
async def test_quote_snapshots_parquet(archive: ArchiveManager, tmp_path: Path) -> None:
    snap = _make_snap()
    state = _make_quote_state()
    for _ in range(10):
        archive.write_quote_snapshot(state, target_mid=state.mid, ref_mid=state.mid, event_type="requote")
        archive.write_mid_sample(snap, mid=50000.5, micro=50000.4, role="target")

    await archive._parquet.flush()

    parquet_dir = tmp_path / "parquet" / "quote_snapshots"
    assert parquet_dir.exists(), "quote_snapshots directory not created"
    parquet_files = list(parquet_dir.rglob("*.parquet"))
    assert parquet_files, "no parquet files written"

    count = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{tmp_path}/parquet/quote_snapshots/**/*.parquet')"
    ).fetchone()[0]
    assert count == 10


@pytest.mark.asyncio
async def test_trade_tape_parquet(archive: ArchiveManager, tmp_path: Path) -> None:
    tick = _make_trade()
    for _ in range(5):
        archive.write_trade_tick(tick, role="target")
    await archive._parquet.flush()

    count = duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{tmp_path}/parquet/trade_tape/**/*.parquet')"
    ).fetchone()[0]
    assert count == 5


@pytest.mark.asyncio
async def test_polars_query(archive: ArchiveManager, tmp_path: Path) -> None:
    """Verify the Polars read path works end-to-end."""
    snap = _make_snap()
    state = _make_quote_state()
    for i in range(20):
        archive.write_quote_snapshot(state, target_mid=state.mid, ref_mid=state.mid, event_type="requote")
        archive.write_mid_sample(snap, mid=50000.0 + i * 0.1, micro=None, role="target")
    await archive._parquet.flush()

    df = pl.read_parquet(str(tmp_path / "parquet" / "mid_tape" / "**" / "*.parquet"))
    assert len(df) == 20
    assert "mid" in df.columns
    assert df["mid"].mean() is not None


@pytest.mark.asyncio
async def test_latency_observe(archive: ArchiveManager) -> None:
    archive.observe_latency("tick_to_quote_us", 1500.0)
    archive.observe_latency("tick_to_quote_us", 2000.0)
    archive.observe_latency("decision_to_send_us", 500.0)
    # Trigger a dump and confirm rows are generated (don't wait full minute)
    rows = archive._lat_archive._hist.dump_and_reset(archive.session_id)
    assert len(rows) == 2
    metrics = {r["metric_name"] for r in rows}
    assert "tick_to_quote_us" in metrics
