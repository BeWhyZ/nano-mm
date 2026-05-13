"""
Unit tests for LocalOrderBook: snapshot/diff apply, sequence semantics,
top-k view, dirty state after mark_dirty.
"""
from __future__ import annotations

import time

import pytest

from data.orderbook.base import LocalOrderBook


def make_book(symbol: str = "BTC_USDT", venue: str = "test") -> LocalOrderBook:
    return LocalOrderBook(symbol, venue)


# ------------------------------------------------------------------
# Snapshot apply
# ------------------------------------------------------------------

def test_snapshot_populates_book():
    book = make_book()
    book.apply_snapshot(
        bids=[(50000.0, 1.0), (49999.0, 2.0)],
        asks=[(50001.0, 1.5), (50002.0, 3.0)],
        seq=100,
        event_ts=1000,
        recv_ts=time.monotonic_ns(),
    )
    assert book.seq == 100
    assert book.is_fresh(max_age_ms=1000.0)

    snap = book.snapshot(k=5)
    assert snap.bids[0].price == 50000.0
    assert snap.asks[0].price == 50001.0
    assert snap.mid_price == pytest.approx(50000.5)


def test_snapshot_replaces_previous():
    book = make_book()
    book.apply_snapshot(
        bids=[(100.0, 1.0)], asks=[(101.0, 1.0)],
        seq=1, event_ts=0, recv_ts=0,
    )
    book.apply_snapshot(
        bids=[(200.0, 5.0)], asks=[(201.0, 5.0)],
        seq=99, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot()
    assert len(snap.bids) == 1
    assert snap.bids[0].price == 200.0


def test_snapshot_removes_zero_qty():
    book = make_book()
    book.apply_snapshot(
        bids=[(50000.0, 0.0), (49999.0, 1.0)],
        asks=[(50001.0, 1.0)],
        seq=1, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot()
    prices = [p.price for p in snap.bids]
    assert 50000.0 not in prices
    assert 49999.0 in prices


# ------------------------------------------------------------------
# Diff apply
# ------------------------------------------------------------------

def test_diff_adds_new_level():
    book = make_book()
    book.apply_snapshot(
        bids=[(50000.0, 1.0)], asks=[(50001.0, 1.0)],
        seq=100, event_ts=0, recv_ts=0,
    )
    book.apply_diff(
        bids=[(49998.0, 3.0)], asks=[],
        seq=101, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot()
    prices = [p.price for p in snap.bids]
    assert 49998.0 in prices


def test_diff_removes_zero_qty_level():
    book = make_book()
    book.apply_snapshot(
        bids=[(50000.0, 1.0), (49999.0, 2.0)],
        asks=[(50001.0, 1.0)],
        seq=100, event_ts=0, recv_ts=0,
    )
    book.apply_diff(
        bids=[(49999.0, 0.0)], asks=[],
        seq=101, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot()
    prices = [p.price for p in snap.bids]
    assert 49999.0 not in prices
    assert 50000.0 in prices


def test_diff_updates_qty_at_existing_level():
    book = make_book()
    book.apply_snapshot(
        bids=[(50000.0, 1.0)], asks=[(50001.0, 1.0)],
        seq=100, event_ts=0, recv_ts=0,
    )
    book.apply_diff(
        bids=[(50000.0, 5.0)], asks=[],
        seq=101, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot()
    assert snap.bids[0].price == 50000.0
    assert snap.bids[0].qty == 5.0


def test_diff_ignored_when_dirty():
    book = make_book()
    # No snapshot applied → dirty
    book.apply_diff(
        bids=[(50000.0, 1.0)], asks=[],
        seq=1, event_ts=0, recv_ts=0,
    )
    assert book.seq == -1  # unchanged


def test_mark_dirty_resets_state():
    book = make_book()
    book.apply_snapshot(
        bids=[(50000.0, 1.0)], asks=[(50001.0, 1.0)],
        seq=100, event_ts=0, recv_ts=0,
    )
    book.mark_dirty()
    assert not book.is_fresh()
    assert book.seq == -1


# ------------------------------------------------------------------
# Top-k view
# ------------------------------------------------------------------

def test_top_k_truncation():
    book = make_book()
    bids = [(50000.0 - i, 1.0) for i in range(30)]  # 30 bid levels
    asks = [(50001.0 + i, 1.0) for i in range(30)]
    book.apply_snapshot(bids=bids, asks=asks, seq=1, event_ts=0, recv_ts=0)

    snap = book.snapshot(k=20)
    assert len(snap.bids) == 20
    assert len(snap.asks) == 20
    # Best bid is highest price
    assert snap.bids[0].price == 50000.0
    # Bids are descending
    for i in range(len(snap.bids) - 1):
        assert snap.bids[i].price > snap.bids[i + 1].price


def test_top_k_below_range_of_k_is_enough_levels():
    book = make_book()
    book.apply_snapshot(
        bids=[(50000.0, 1.0)], asks=[(50001.0, 1.0)],
        seq=1, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot(k=20)
    assert len(snap.bids) == 1  # only 1 level exists
    assert len(snap.asks) == 1


# ------------------------------------------------------------------
# micro_price
# ------------------------------------------------------------------

def test_micro_price_equal_volume():
    book = make_book()
    # Equal volume on both sides → micro_price == mid_price
    book.apply_snapshot(
        bids=[(100.0, 10.0)],
        asks=[(102.0, 10.0)],
        seq=1, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot()
    mp = snap.micro_price(k=1)
    assert mp == pytest.approx(101.0)


def test_micro_price_more_bid_volume_pulls_toward_ask():
    book = make_book()
    # More volume on bid side → micro_price closer to ask
    book.apply_snapshot(
        bids=[(100.0, 20.0)],
        asks=[(102.0, 5.0)],
        seq=1, event_ts=0, recv_ts=0,
    )
    snap = book.snapshot()
    mp = snap.micro_price(k=1)
    mid = snap.mid_price
    assert mid is not None and mp is not None
    assert mp > mid  # pulled toward ask


# ------------------------------------------------------------------
# is_fresh age check
# ------------------------------------------------------------------

def test_is_fresh_with_old_recv_ts():
    book = make_book()
    old_ts = time.monotonic_ns() - int(2e9)  # 2 seconds ago
    book.apply_snapshot(
        bids=[(50000.0, 1.0)], asks=[(50001.0, 1.0)],
        seq=100, event_ts=0, recv_ts=old_ts,
    )
    assert not book.is_fresh(max_age_ms=500.0)
    assert book.is_fresh(max_age_ms=5000.0)
