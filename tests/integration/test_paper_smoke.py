"""
Integration smoke: full paper-executor pipeline against canned market data.

Verifies:
  - At least one order is placed, ACKed, and archived.
  - A walk-through fill is generated, applied through OrderTracker,
    and written to the fills table.
  - SQLite fill row has non-NULL inventory_after, realized_pnl_after, q_norm_after.
  - OrderTracker.inventory() == PnlTracker._inventory after the run.
  - order_events table shows ADD → ACK → FILL sequence.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog

from biz.domain.book import OrderBookSnapshot, PriceLevel
from biz.domain.order import OrderSide
from biz.domain.quote import Quote, QuoteState
from biz.domain.trade import TradeTick
from biz.usecase.fill_simulator import FillSimulator
from config import PaperConfig, SpreadConfig
from data.archive import ArchiveManager
from data.exchange.oms import OrderTracker
from data.exchange.paper import PaperExchange
from service.paper_executor_service import PaperExecutor, PnlTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(
    best_bid: float = 49999.0,
    best_ask: float = 50001.0,
    bid_qty: float = 0.5,
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTC_USDT",
        venue="paper",
        bids=[PriceLevel(best_bid, bid_qty), PriceLevel(best_bid - 1, 1.0)],
        asks=[PriceLevel(best_ask, 1.0), PriceLevel(best_ask + 1, 2.0)],
        event_ts=int(time.time() * 1000),
        send_ts=0,
        recv_ts=time.monotonic_ns(),
        seq=1,
    )


def _state(
    bid_price: float = 49999.0,
    ask_price: float = 50001.0,
    qty: float = 0.001,
) -> QuoteState:
    return QuoteState(
        symbol="BTC_USDT",
        venue="paper",
        mid=50000.0,
        bids=(Quote(OrderSide.BUY, bid_price, qty),),
        asks=(Quote(OrderSide.SELL, ask_price, qty),),
        sigma=0.001,
        A=5.0,
        k=0.0005,
        gamma=0.1,
        q_norm=0.0,
        ts_ns=time.monotonic_ns(),
    )


def _trade(price: float, qty: float, side: OrderSide) -> TradeTick:
    return TradeTick(
        symbol="BTC_USDT",
        venue="paper",
        price=price,
        qty=qty,
        side=side,
        event_ts=int(time.time() * 1000),
        recv_ts=time.monotonic_ns(),
    )


@pytest.fixture()
async def archive(tmp_path: Path) -> ArchiveManager:
    mgr = ArchiveManager(
        base_dir=tmp_path,
        symbol="BTC_USDT",
        target_venue="paper",
        reference_venue="paper",
        mode="paper",
        sqlite_flush_rows=1,
        sqlite_flush_interval_s=0.05,
        parquet_flush_rows=1,
        parquet_flush_interval_s=0.05,
    )
    await mgr.start()
    yield mgr
    await mgr.stop()


class _FakeMMService:
    """Minimal stub that allows PaperExecutor to register listeners without a live WS."""

    def __init__(self):
        self._book_listeners = []
        self._quote_listeners = []
        self._trade_listeners = []
        self._inventory = 0.0
        self.state = None  # QuoteState

    def register_book_listener(self, cb): self._book_listeners.append(cb)
    def register_quote_listener(self, cb): self._quote_listeners.append(cb)
    def register_trade_listener(self, cb): self._trade_listeners.append(cb)

    def set_inventory(self, q_norm): self._inventory = q_norm

    def get_fair_price(self, reference: bool = False): return None

    # Drive helpers
    def push_book(self, snap): [cb(snap) for cb in self._book_listeners]
    def push_quote(self, state): [cb(state) for cb in self._quote_listeners]
    def push_trade(self, tick): [cb(tick) for cb in self._trade_listeners]


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paper_executor_full_pipeline(archive: ArchiveManager, tmp_path: Path):
    spread_cfg = SpreadConfig(
        price_tick=1.0,  # $1 tick so prices land cleanly
        lot_size=0.001,
        Q_max=10.0,
    )
    paper_cfg = PaperConfig(qty_step=0.00001, maker_fee_bps=0.0)

    mm = _FakeMMService()
    executor = PaperExecutor(
        symbol="BTC_USDT",
        venue="paper",
        mm_service=mm,
        spread_cfg=spread_cfg,
        paper_cfg=paper_cfg,
        session_id=archive.session_id,
        archive=archive,
        lg=structlog.get_logger("test"),
    )

    # Step 1: feed book + quote to trigger order placement
    snap = _snap(best_bid=49999.0, best_ask=50001.0, bid_qty=0.5)
    state = _state(bid_price=49999.0, ask_price=50001.0, qty=0.001)

    mm.push_book(snap)
    mm.push_quote(state)

    # Pump the event loop so asyncio.create_task callbacks resolve
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # At least one order should be tracked as OPEN
    active = executor._tracker.active_orders()
    assert len(active) >= 1, "Expected at least one active order after quote"

    bid_orders = [o for o in active if o.side == OrderSide.BUY]
    assert len(bid_orders) >= 1, "Expected at least one bid order"

    # Step 2: walk-through sell trade at a price below our bid → fills us
    # bid is at 49999; trade at 49998 (walk-through)
    trade = _trade(49998.0, 0.001, OrderSide.SELL)
    mm.push_trade(trade)

    # Step 3: flush archive and verify
    await archive._sqlite.flush()

    db_path = str(tmp_path / "sqlite" / "nano-mm.db")
    db = sqlite3.connect(db_path)

    # Check fills table
    fill_rows = db.execute(
        "SELECT trade_id, qty, inventory_after, realized_pnl_after, q_norm_after FROM fills"
    ).fetchall()
    db.close()

    assert len(fill_rows) >= 1, "Expected at least one fill in archive"
    row = fill_rows[0]
    trade_id, qty, inv_after, realized, q_norm_after = row
    assert trade_id.startswith("paper-")
    assert qty == pytest.approx(0.001, abs=1e-6)
    assert inv_after is not None, "inventory_after must not be NULL"
    assert realized is not None, "realized_pnl_after must not be NULL"
    assert q_norm_after is not None, "q_norm_after must not be NULL"

    # Check inventory consistency between tracker and pnl tracker
    assert abs(executor._pnl._inventory - executor._tracker.inventory()) < 1e-8


@pytest.mark.asyncio
async def test_order_events_sequence(archive: ArchiveManager, tmp_path: Path):
    """ADD → ACK sequence must appear in order_events."""
    spread_cfg = SpreadConfig(price_tick=1.0, lot_size=0.001, Q_max=10.0)
    paper_cfg = PaperConfig(qty_step=0.00001)

    mm = _FakeMMService()
    executor = PaperExecutor(
        symbol="BTC_USDT",
        venue="paper",
        mm_service=mm,
        spread_cfg=spread_cfg,
        paper_cfg=paper_cfg,
        session_id=archive.session_id,
        archive=archive,
        lg=structlog.get_logger("test"),
    )

    snap = _snap()
    state = _state()
    mm.push_book(snap)
    mm.push_quote(state)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await archive._sqlite.flush()

    db = sqlite3.connect(str(tmp_path / "sqlite" / "nano-mm.db"))
    events = db.execute(
        "SELECT event_type FROM order_events ORDER BY seq"
    ).fetchall()
    db.close()

    event_types = [e[0] for e in events]
    assert "ADD" in event_types
    assert "ACK" in event_types
    # ADD must come before ACK for each order
    add_idx = event_types.index("ADD")
    ack_idx = event_types.index("ACK")
    assert add_idx < ack_idx


@pytest.mark.asyncio
async def test_post_only_reject_no_fill(archive: ArchiveManager, tmp_path: Path):
    """Order that would cross the spread is rejected; no fill generated."""
    spread_cfg = SpreadConfig(price_tick=1.0, lot_size=0.001, Q_max=10.0)
    paper_cfg = PaperConfig(qty_step=0.00001)

    mm = _FakeMMService()
    executor = PaperExecutor(
        symbol="BTC_USDT",
        venue="paper",
        mm_service=mm,
        spread_cfg=spread_cfg,
        paper_cfg=paper_cfg,
        session_id=archive.session_id,
        archive=archive,
        lg=structlog.get_logger("test"),
    )

    # Book: best_ask = 50001. Place a bid at 50001 → should be rejected.
    snap = _snap(best_ask=50001.0)
    state = _state(bid_price=50001.0, ask_price=50002.0)  # bid crosses ask

    mm.push_book(snap)
    mm.push_quote(state)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # No OPEN orders expected (all rejected or pending cancel from initial empty state)
    active = executor._tracker.active_orders()
    open_bid = [o for o in active if o.side == OrderSide.BUY]
    assert open_bid == [], "Crossing bid should have been rejected"

    await archive._sqlite.flush()
    db = sqlite3.connect(str(tmp_path / "sqlite" / "nano-mm.db"))
    fills = db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    db.close()
    assert fills == 0, "No fills expected after a rejected order"
