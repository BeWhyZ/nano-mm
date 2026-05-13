"""
Unit tests for OrderTracker OMS state machine.
Covers all valid transitions, ghost fill, dedup, inventory updates.
"""
from __future__ import annotations

import time

import pytest
import structlog

from biz.domain.order import Fill, Order, OrderSide, OrderStatus, OrderType
from data.exchange.oms import OrderTracker


def make_order(
    coid: str = "c001",
    symbol: str = "BTC_USDT",
    side: OrderSide = OrderSide.BUY,
    price: float = 50000.0,
    qty: float = 1.0,
) -> Order:
    return Order(
        client_order_id=coid,
        symbol=symbol,
        venue="binance_spot",
        side=side,
        order_type=OrderType.LIMIT_MAKER,
        price=price,
        original_qty=qty,
    )


def make_fill(
    trade_id: str = "t001",
    order_id: str = "c001",
    price: float = 50000.0,
    qty: float = 0.5,
    side: OrderSide = OrderSide.BUY,
) -> Fill:
    return Fill(
        trade_id=trade_id,
        order_id=order_id,
        price=price,
        qty=qty,
        side=side,
        event_ts=int(time.time() * 1000),
        recv_ts=time.monotonic_ns(),
    )


def make_tracker() -> OrderTracker:
    return OrderTracker(structlog.get_logger("test"))


def tracker_with_order(**kw) -> tuple[OrderTracker, Order]:
    t = make_tracker()
    o = make_order(**kw)
    t.add(o)
    return t, o


# ------------------------------------------------------------------
# PENDING_NEW → OPEN
# ------------------------------------------------------------------

def test_ack_transitions_to_open():
    t, o = tracker_with_order()
    t.on_ack("c001", "e001")
    assert o.status == OrderStatus.OPEN
    assert o.exchange_order_id == "e001"


def test_ack_registers_exchange_id_index():
    t, o = tracker_with_order()
    t.on_ack("c001", "e001")
    assert t.get_by_exchange_id("e001") is o


# ------------------------------------------------------------------
# PENDING_NEW → REJECTED
# ------------------------------------------------------------------

def test_reject_transitions_to_rejected():
    t, o = tracker_with_order()
    t.on_reject("c001", "insufficient_balance")
    assert o.status == OrderStatus.REJECTED
    assert o.status.is_terminal


# ------------------------------------------------------------------
# OPEN → PARTIALLY_FILLED → FILLED
# ------------------------------------------------------------------

def test_partial_fill():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(qty=0.3))
    assert o.status == OrderStatus.PARTIALLY_FILLED
    assert o.filled_qty == pytest.approx(0.3)
    assert o.remaining_qty == pytest.approx(0.7)


def test_full_fill():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(qty=1.0))
    assert o.status == OrderStatus.FILLED
    assert o.filled_qty == pytest.approx(1.0)
    assert o.remaining_qty == pytest.approx(0.0)


def test_two_partial_fills_sum_to_original():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(trade_id="t001", qty=0.4))
    t.on_fill(make_fill(trade_id="t002", qty=0.6))
    assert o.status == OrderStatus.FILLED
    assert o.filled_qty == pytest.approx(1.0)


# ------------------------------------------------------------------
# Invariant: filled + remaining + canceled == original
# ------------------------------------------------------------------

def test_invariant_after_partial_fill_and_cancel():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(qty=0.4))
    t.mark_pending_cancel("c001")
    t.on_cancel_ack("c001", canceled_qty=0.6)
    assert o.status == OrderStatus.CANCELED
    assert o.filled_qty + o.canceled_qty == pytest.approx(o.original_qty)


# ------------------------------------------------------------------
# Ghost fill: PENDING_CANCEL + fill is legal
# ------------------------------------------------------------------

def test_ghost_fill_in_pending_cancel():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.mark_pending_cancel("c001")
    assert o.status == OrderStatus.PENDING_CANCEL

    # Fill arrives while cancel is in-flight
    t.on_fill(make_fill(qty=0.5))
    assert o.status == OrderStatus.PENDING_CANCEL  # still waiting for cancel ack
    assert o.filled_qty == pytest.approx(0.5)

    # Cancel ack arrives
    t.on_cancel_ack("c001", canceled_qty=0.5)
    assert o.status == OrderStatus.CANCELED
    assert o.filled_qty + o.canceled_qty == pytest.approx(1.0)


def test_ghost_fill_fully_fills_order():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.mark_pending_cancel("c001")

    # Full ghost fill before cancel ack
    t.on_fill(make_fill(qty=1.0))
    assert o.status == OrderStatus.FILLED


# ------------------------------------------------------------------
# Fill deduplication
# ------------------------------------------------------------------

def test_duplicate_fill_ignored():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(trade_id="t001", qty=0.5))
    t.on_fill(make_fill(trade_id="t001", qty=0.5))  # duplicate
    assert o.filled_qty == pytest.approx(0.5)


# ------------------------------------------------------------------
# Inventory
# ------------------------------------------------------------------

def test_inventory_increases_on_buy_fill():
    t, o = tracker_with_order(side=OrderSide.BUY, qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(qty=0.5, side=OrderSide.BUY))
    assert t.inventory() == pytest.approx(0.5)


def test_inventory_decreases_on_sell_fill():
    t = make_tracker()
    buy_order = make_order(coid="c001", side=OrderSide.BUY, qty=1.0)
    sell_order = make_order(coid="c002", side=OrderSide.SELL, qty=0.3)
    t.add(buy_order)
    t.add(sell_order)
    t.on_ack("c001", "e001")
    t.on_ack("c002", "e002")
    t.on_fill(make_fill(trade_id="t001", order_id="c001", qty=1.0, side=OrderSide.BUY))
    t.on_fill(make_fill(trade_id="t002", order_id="c002", qty=0.3, side=OrderSide.SELL))
    assert t.inventory() == pytest.approx(0.7)


# ------------------------------------------------------------------
# Terminal state guard
# ------------------------------------------------------------------

def test_fill_after_terminal_is_dropped():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(trade_id="t001", qty=1.0))  # FILLED
    assert o.status == OrderStatus.FILLED
    # Second fill with different trade_id after terminal
    t.on_fill(make_fill(trade_id="t999", qty=0.1))
    assert o.filled_qty == pytest.approx(1.0)  # unchanged


def test_cancel_ack_after_filled_is_noop():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(qty=1.0))
    assert o.status == OrderStatus.FILLED
    t.on_cancel_ack("c001", canceled_qty=0.0)
    assert o.status == OrderStatus.FILLED  # not overwritten


# ------------------------------------------------------------------
# cancel_reject: revert to active state
# ------------------------------------------------------------------

def test_cancel_reject_reverts_to_open():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.mark_pending_cancel("c001")
    t.on_cancel_reject("c001", "already_filled")
    assert o.status == OrderStatus.OPEN


def test_cancel_reject_reverts_to_partially_filled_if_has_fills():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(qty=0.3))
    t.mark_pending_cancel("c001")
    t.on_cancel_reject("c001", "already_filled")
    assert o.status == OrderStatus.PARTIALLY_FILLED


# ------------------------------------------------------------------
# avg_fill_price
# ------------------------------------------------------------------

def test_avg_fill_price_single_fill():
    t, o = tracker_with_order(qty=1.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(trade_id="t001", price=50000.0, qty=1.0))
    assert o.avg_fill_price == pytest.approx(50000.0)


def test_avg_fill_price_two_fills_at_different_prices():
    t, o = tracker_with_order(qty=2.0)
    t.on_ack("c001", "e001")
    t.on_fill(make_fill(trade_id="t001", price=50000.0, qty=1.0))
    t.on_fill(make_fill(trade_id="t002", price=50002.0, qty=1.0))
    assert o.avg_fill_price == pytest.approx(50001.0)


# ------------------------------------------------------------------
# active_orders
# ------------------------------------------------------------------

def test_active_orders_excludes_terminal():
    t = make_tracker()
    o1 = make_order(coid="c001", qty=1.0)
    o2 = make_order(coid="c002", qty=1.0)
    t.add(o1)
    t.add(o2)
    t.on_ack("c001", "e001")
    t.on_ack("c002", "e002")
    t.on_fill(make_fill(trade_id="t001", order_id="c001", qty=1.0))  # c001 → FILLED

    active = t.active_orders()
    assert len(active) == 1
    assert active[0].client_order_id == "c002"


# ------------------------------------------------------------------
# PENDING_NEW direct to FILLED (instant taker fill)
# ------------------------------------------------------------------

def test_pending_new_direct_to_filled():
    t, o = tracker_with_order(qty=1.0)
    # Fill arrives before ACK (very fast venue)
    t.on_fill(make_fill(qty=1.0))
    assert o.status == OrderStatus.FILLED
