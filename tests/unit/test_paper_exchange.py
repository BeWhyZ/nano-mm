"""
Unit tests for PaperExchange.
"""
from __future__ import annotations

import asyncio
import time

import pytest
import structlog

from biz.domain.book import OrderBookSnapshot, PriceLevel
from biz.domain.order import OrderSide
from data.exchange.paper import PaperExchange


def _snap(best_bid: float = 49999.0, best_ask: float = 50001.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTC_USDT",
        venue="paper",
        bids=[PriceLevel(best_bid, 1.0)],
        asks=[PriceLevel(best_ask, 1.0)],
        event_ts=int(time.time() * 1000),
        send_ts=0,
        recv_ts=time.monotonic_ns(),
        seq=1,
    )


def _make_exch():
    acks = []
    rejects = []
    cancel_acks = []
    exch = PaperExchange(
        symbol="BTC_USDT",
        venue="paper",
        on_ack=lambda coid, exid: acks.append((coid, exid)),
        on_reject=lambda coid, reason: rejects.append((coid, reason)),
        on_cancel_ack=lambda coid, qty: cancel_acks.append((coid, qty)),
        lg=structlog.get_logger("test"),
    )
    return exch, acks, rejects, cancel_acks


# ---------------------------------------------------------------------------
# submit_limit — ACK path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buy_below_best_ask_acks():
    exch, acks, rejects, _ = _make_exch()
    exch.set_book(_snap(best_bid=49999.0, best_ask=50001.0))
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50000.0, 0.001, snap=_snap())
    assert len(acks) == 1
    assert acks[0][0] == "c1"
    assert acks[0][1].startswith("PAPER-")
    assert rejects == []


@pytest.mark.asyncio
async def test_sell_above_best_bid_acks():
    exch, acks, rejects, _ = _make_exch()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.SELL, 50000.5, 0.001, snap=_snap())
    assert len(acks) == 1
    assert rejects == []


# ---------------------------------------------------------------------------
# submit_limit — post-only reject
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buy_at_or_above_best_ask_rejects():
    exch, acks, rejects, _ = _make_exch()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50001.0, 0.001, snap=_snap())
    assert len(rejects) == 1
    assert rejects[0] == ("c1", "post_only_cross")
    assert acks == []


@pytest.mark.asyncio
async def test_buy_strictly_above_best_ask_rejects():
    exch, acks, rejects, _ = _make_exch()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50002.0, 0.001, snap=_snap())
    assert len(rejects) == 1
    assert acks == []


@pytest.mark.asyncio
async def test_sell_at_or_below_best_bid_rejects():
    exch, acks, rejects, _ = _make_exch()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.SELL, 49999.0, 0.001, snap=_snap())
    assert len(rejects) == 1
    assert rejects[0][1] == "post_only_cross"
    assert acks == []


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_open_order_fires_cancel_ack():
    exch, acks, rejects, cancel_acks = _make_exch()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50000.0, 0.001, snap=_snap())
    assert len(acks) == 1

    await exch.cancel_order("c1", "BTC_USDT")
    assert len(cancel_acks) == 1
    coid, qty = cancel_acks[0]
    assert coid == "c1"
    assert qty == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_cancel_unknown_coid_no_callback():
    exch, _, _, cancel_acks = _make_exch()
    await exch.cancel_order("unknown", "BTC_USDT")
    assert cancel_acks == []


# ---------------------------------------------------------------------------
# cancel_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_all_cancels_every_open_order():
    exch, acks, _, cancel_acks = _make_exch()
    snap = _snap()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50000.0, 0.001, snap=snap)
    await exch.submit_limit("c2", "BTC_USDT", OrderSide.SELL, 50000.5, 0.001, snap=snap)
    assert len(acks) == 2

    await exch.cancel_all("BTC_USDT")
    cancelled = {c[0] for c in cancel_acks}
    assert cancelled == {"c1", "c2"}


# ---------------------------------------------------------------------------
# get_open_orders / get_position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_open_orders_returns_list():
    exch, _, _, _ = _make_exch()
    result = await exch.get_open_orders("BTC_USDT")
    assert result == []


@pytest.mark.asyncio
async def test_get_position_returns_zero():
    exch, _, _, _ = _make_exch()
    pos = await exch.get_position("BTC_USDT")
    assert pos == 0.0


# ---------------------------------------------------------------------------
# notify_fill updates remaining
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_fill_decrements_remaining():
    exch, _, _, _ = _make_exch()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50000.0, 0.005, snap=_snap())
    exch.notify_fill("c1", 0.003)
    assert exch._remaining["c1"] == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_notify_fill_full_pops_order():
    exch, _, _, _ = _make_exch()
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50000.0, 0.001, snap=_snap())
    exch.notify_fill("c1", 0.001)
    assert "c1" not in exch._remaining


# ---------------------------------------------------------------------------
# No snap — falls back to set_book
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_snap_uses_latest_book():
    exch, acks, rejects, _ = _make_exch()
    exch.set_book(_snap(best_ask=50001.0))
    # No snap kwarg passed — uses set_book value
    await exch.submit_limit("c1", "BTC_USDT", OrderSide.BUY, 50000.0, 0.001)
    assert len(acks) == 1
    assert rejects == []
