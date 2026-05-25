"""
Unit tests for QuoteDiffer.

Each test verifies one behavioural invariant; see the plan for the full matrix.
"""
from __future__ import annotations

import time

import pytest

from biz.domain.book import OrderBookSnapshot, PriceLevel
from biz.domain.order import Order, OrderSide, OrderStatus, OrderType
from biz.domain.quote import Quote, QuoteState
from biz.usecase.quote_differ import CancelAction, PlaceAction, diff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
) -> OrderBookSnapshot:
    bids = bids or [(50000.0, 1.0), (49999.0, 2.0)]
    asks = asks or [(50001.0, 1.0), (50002.0, 2.0)]
    return OrderBookSnapshot(
        symbol="BTC_USDT",
        venue="paper",
        bids=[PriceLevel(p, q) for p, q in bids],
        asks=[PriceLevel(p, q) for p, q in asks],
        event_ts=int(time.time() * 1000),
        send_ts=0,
        recv_ts=time.monotonic_ns(),
        seq=1,
    )


def _state(
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
) -> QuoteState:
    if bids is None:
        bids = [(49999.0, 0.001)]
    if asks is None:
        asks = [(50001.0, 0.001)]
    return QuoteState(
        symbol="BTC_USDT",
        venue="paper",
        mid=50000.0,
        bids=tuple(Quote(OrderSide.BUY, p, q) for p, q in bids),
        asks=tuple(Quote(OrderSide.SELL, p, q) for p, q in asks),
        sigma=0.001,
        A=5.0,
        k=0.0005,
        gamma=0.1,
        q_norm=0.0,
        ts_ns=time.monotonic_ns(),
    )


def _order(
    coid: str,
    side: OrderSide,
    price: float,
    qty: float,
    status: OrderStatus = OrderStatus.OPEN,
) -> Order:
    o = Order(
        client_order_id=coid,
        symbol="BTC_USDT",
        venue="paper",
        side=side,
        order_type=OrderType.LIMIT_MAKER,
        price=price,
        original_qty=qty,
    )
    o.status = status
    return o


TICK = 0.01
STEP = 0.00001


def do_diff(state, snap, active) -> list:
    return diff(state, snap, active, price_tick=TICK, qty_step=STEP)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_active_all_desired_become_places():
    state = _state(bids=[(49999.0, 0.001)], asks=[(50001.0, 0.001)])
    actions = do_diff(state, _snap(), [])
    places = [a for a in actions if isinstance(a, PlaceAction)]
    cancels = [a for a in actions if isinstance(a, CancelAction)]
    assert len(places) == 2
    assert len(cancels) == 0
    sides = {a.side for a in places}
    assert sides == {OrderSide.BUY, OrderSide.SELL}


def test_empty_desired_all_active_become_cancels():
    state = _state(bids=[], asks=[])
    active = [
        _order("b1", OrderSide.BUY, 49999.0, 0.001),
        _order("a1", OrderSide.SELL, 50001.0, 0.001),
    ]
    actions = do_diff(state, _snap(), active)
    cancels = [a for a in actions if isinstance(a, CancelAction)]
    places = [a for a in actions if isinstance(a, PlaceAction)]
    assert len(cancels) == 2
    assert len(places) == 0


def test_exact_match_no_actions():
    state = _state(bids=[(49999.0, 0.001)], asks=[(50001.0, 0.001)])
    active = [
        _order("b1", OrderSide.BUY, 49999.0, 0.001),
        _order("a1", OrderSide.SELL, 50001.0, 0.001),
    ]
    actions = do_diff(state, _snap(), active)
    assert actions == []


def test_price_drift_one_tick_cancel_then_place():
    """Desired bid moves from 49999 to 49998 → cancel old, place new."""
    state = _state(bids=[(49998.0, 0.001)], asks=[(50001.0, 0.001)])
    active = [
        _order("b1", OrderSide.BUY, 49999.0, 0.001),
        _order("a1", OrderSide.SELL, 50001.0, 0.001),
    ]
    actions = do_diff(state, _snap(), active)
    cancels = {a.client_order_id for a in actions if isinstance(a, CancelAction)}
    places = [a for a in actions if isinstance(a, PlaceAction)]
    assert "b1" in cancels
    assert any(a.side == OrderSide.BUY and abs(a.price - 49998.0) < 0.001 for a in places)
    # Ask unchanged — no action for it
    assert "a1" not in cancels


def test_material_qty_change_cancel_then_place():
    """Desired qty shifts by 2 steps → cancel old, place new."""
    new_qty = 0.001 + 2 * STEP  # 0.00102
    state = _state(bids=[(49999.0, new_qty)], asks=[])
    active = [_order("b1", OrderSide.BUY, 49999.0, 0.001)]
    actions = do_diff(state, _snap(), active)
    cancels = {a.client_order_id for a in actions if isinstance(a, CancelAction)}
    places = [a for a in actions if isinstance(a, PlaceAction)]
    assert "b1" in cancels
    assert len(places) == 1


def test_sub_step_qty_change_no_action():
    """Qty differs by less than one step → KEEP."""
    sub_step_qty = 0.001 + STEP * 0.4  # 0.001004 — rounds to same step bucket as 0.001
    state = _state(bids=[(49999.0, sub_step_qty)], asks=[])
    active = [_order("b1", OrderSide.BUY, 49999.0, 0.001)]
    actions = do_diff(state, _snap(), active)
    assert actions == []


def test_pending_new_blocks_duplicate_place():
    """PENDING_NEW at the same key prevents emitting a second PlaceAction."""
    state = _state(bids=[(49999.0, 0.001)], asks=[])
    active = [_order("b1", OrderSide.BUY, 49999.0, 0.001, status=OrderStatus.PENDING_NEW)]
    actions = do_diff(state, _snap(), active)
    places = [a for a in actions if isinstance(a, PlaceAction)]
    assert places == []


def test_pending_cancel_still_emits_place():
    """A PENDING_CANCEL order at our price is treated as leaving; we place a fresh one."""
    state = _state(bids=[(49999.0, 0.001)], asks=[])
    active = [_order("b1", OrderSide.BUY, 49999.0, 0.001, status=OrderStatus.PENDING_CANCEL)]
    actions = do_diff(state, _snap(), active)
    places = [a for a in actions if isinstance(a, PlaceAction)]
    assert len(places) == 1
    assert places[0].side == OrderSide.BUY


def test_extra_active_at_same_key_cancelled():
    """If two active orders land at the same tick price, keep one, cancel the other."""
    state = _state(bids=[(49999.0, 0.001)], asks=[])
    active = [
        _order("b1", OrderSide.BUY, 49999.0, 0.001),
        _order("b2", OrderSide.BUY, 49999.0, 0.001),
    ]
    actions = do_diff(state, _snap(), active)
    cancels = {a.client_order_id for a in actions if isinstance(a, CancelAction)}
    assert len(cancels) == 1
    # Exactly one of b1/b2 should be cancelled
    assert cancels.issubset({"b1", "b2"})


def test_mixed_bid_and_ask():
    state = _state(
        bids=[(49999.0, 0.001), (49998.0, 0.002)],
        asks=[(50001.0, 0.001), (50002.0, 0.002)],
    )
    active = [
        _order("b1", OrderSide.BUY, 49999.0, 0.001),
        # 49998 bid missing
        _order("a1", OrderSide.SELL, 50001.0, 0.001),
        _order("a2", OrderSide.SELL, 50003.0, 0.002),  # stale ask level
    ]
    actions = do_diff(state, _snap(), active)
    cancel_ids = {a.client_order_id for a in actions if isinstance(a, CancelAction)}
    place_sides_prices = {(a.side, round(a.price, 2)) for a in actions if isinstance(a, PlaceAction)}

    # Stale a2 at 50003 should be cancelled
    assert "a2" in cancel_ids
    # b1 and a1 are kept (no actions)
    assert "b1" not in cancel_ids
    assert "a1" not in cancel_ids
    # New bid at 49998 and new ask at 50002 should be placed
    assert (OrderSide.BUY, 49998.0) in place_sides_prices
    assert (OrderSide.SELL, 50002.0) in place_sides_prices


def test_ladder_level_assigned_correctly():
    """ladder_level in PlaceAction matches the index in state.bids/asks."""
    state = _state(
        bids=[(49999.0, 0.001), (49998.0, 0.002)],
        asks=[(50001.0, 0.001)],
    )
    actions = do_diff(state, _snap(), [])
    places = {(a.side, round(a.price, 2)): a.ladder_level for a in actions if isinstance(a, PlaceAction)}
    assert places[(OrderSide.BUY, 49999.0)] == 0
    assert places[(OrderSide.BUY, 49998.0)] == 1
    assert places[(OrderSide.SELL, 50001.0)] == 0
