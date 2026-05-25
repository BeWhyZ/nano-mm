"""
Unit tests for FillSimulator — strict FIFO queue model.
"""
from __future__ import annotations

import time

import pytest
import structlog

from biz.domain.book import OrderBookSnapshot, PriceLevel
from biz.domain.order import Fill, OrderSide
from biz.domain.trade import TradeTick
from biz.usecase.fill_simulator import FillSimulator


def _sim() -> FillSimulator:
    return FillSimulator("BTC_USDT", "paper", structlog.get_logger("test"))


def _snap(bid_qty_at_49999: float = 1.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTC_USDT",
        venue="paper",
        bids=[PriceLevel(49999.0, bid_qty_at_49999), PriceLevel(49998.0, 2.0)],
        asks=[PriceLevel(50001.0, 1.0), PriceLevel(50002.0, 2.0)],
        event_ts=int(time.time() * 1000),
        send_ts=0,
        recv_ts=time.monotonic_ns(),
        seq=1,
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


# ---------------------------------------------------------------------------
# BUY resting maker order tests
# ---------------------------------------------------------------------------

def test_buy_sell_trade_below_queue_no_fill():
    sim = _sim()
    snap = _snap(bid_qty_at_49999=0.5)  # queue_ahead = 0.5
    sim.set_book(snap)
    sim.add_order("b1", OrderSide.BUY, 49999.0, 0.001)

    trade = _trade(49999.0, 0.3, OrderSide.SELL)  # 0.3 < 0.5 queue
    fills = sim.on_trade(trade)
    assert fills == []


def test_buy_sell_trade_exhausts_queue_and_fills():
    sim = _sim()
    snap = _snap(bid_qty_at_49999=0.3)
    sim.set_book(snap)
    sim.add_order("b1", OrderSide.BUY, 49999.0, 0.001)

    # Trade of 0.4 consumes 0.3 queue and leaves 0.1 residual → fills min(0.001, 0.1)
    trade = _trade(49999.0, 0.4, OrderSide.SELL)
    fills = sim.on_trade(trade)
    assert len(fills) == 1
    assert fills[0].order_id == "b1"
    assert fills[0].qty == pytest.approx(0.001)
    assert fills[0].side == OrderSide.BUY


def test_buy_walkthrough_fill_bounded_by_tick_qty():
    """Trade at price below our bid → walk-through; fill bounded by tick.qty."""
    sim = _sim()
    snap = _snap(bid_qty_at_49999=0.5)
    sim.set_book(snap)
    sim.add_order("b1", OrderSide.BUY, 49999.0, 1.0)  # large order

    # Sell aggressor hits 49998 — walked through our 49999 level
    trade = _trade(49998.0, 0.3, OrderSide.SELL)
    fills = sim.on_trade(trade)
    assert len(fills) == 1
    assert fills[0].qty == pytest.approx(0.3)  # bounded by tick.qty, not remaining_qty


def test_buy_sell_trade_at_worse_price_no_fill():
    """Sell at 50001 (above our 49999 bid) cannot reach us."""
    sim = _sim()
    sim.set_book(_snap())
    sim.add_order("b1", OrderSide.BUY, 49999.0, 0.001)
    fills = sim.on_trade(_trade(50001.0, 1.0, OrderSide.SELL))
    assert fills == []


def test_buy_trade_same_side_no_fill():
    """BUY aggressor trade is irrelevant for a BUY resting maker."""
    sim = _sim()
    sim.set_book(_snap())
    sim.add_order("b1", OrderSide.BUY, 49999.0, 0.001)
    fills = sim.on_trade(_trade(49999.0, 1.0, OrderSide.BUY))
    assert fills == []


def test_buy_partial_fill_remaining_tracked():
    sim = _sim()
    snap = _snap(bid_qty_at_49999=0.0)  # no queue
    sim.set_book(snap)
    sim.add_order("b1", OrderSide.BUY, 49999.0, 0.005)

    # Trade of 0.003 → fills 0.003; remaining = 0.002
    fills = sim.on_trade(_trade(49999.0, 0.003, OrderSide.SELL))
    assert len(fills) == 1
    assert fills[0].qty == pytest.approx(0.003)
    assert sim._orders["b1"].remaining_qty == pytest.approx(0.002)

    # Second trade of 0.002 → fully fills
    fills2 = sim.on_trade(_trade(49999.0, 0.002, OrderSide.SELL))
    assert len(fills2) == 1
    assert fills2[0].qty == pytest.approx(0.002)
    assert "b1" not in sim._orders  # fully consumed, removed


# ---------------------------------------------------------------------------
# SELL resting maker order tests (mirror)
# ---------------------------------------------------------------------------

def _snap_ask(ask_qty_at_50001: float = 1.0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol="BTC_USDT",
        venue="paper",
        bids=[PriceLevel(49999.0, 1.0)],
        asks=[PriceLevel(50001.0, ask_qty_at_50001), PriceLevel(50002.0, 2.0)],
        event_ts=int(time.time() * 1000),
        send_ts=0,
        recv_ts=time.monotonic_ns(),
        seq=1,
    )


def test_sell_buy_trade_above_queue_no_fill():
    sim = _sim()
    snap = _snap_ask(ask_qty_at_50001=0.5)
    sim.set_book(snap)
    sim.add_order("a1", OrderSide.SELL, 50001.0, 0.001)

    fills = sim.on_trade(_trade(50001.0, 0.2, OrderSide.BUY))
    assert fills == []


def test_sell_buy_trade_exhausts_queue_and_fills():
    sim = _sim()
    snap = _snap_ask(ask_qty_at_50001=0.2)
    sim.set_book(snap)
    sim.add_order("a1", OrderSide.SELL, 50001.0, 0.001)

    fills = sim.on_trade(_trade(50001.0, 0.5, OrderSide.BUY))
    assert len(fills) == 1
    assert fills[0].order_id == "a1"
    assert fills[0].qty == pytest.approx(0.001)


def test_sell_walkthrough_bounded_by_tick_qty():
    sim = _sim()
    sim.set_book(_snap_ask(ask_qty_at_50001=0.5))
    sim.add_order("a1", OrderSide.SELL, 50001.0, 1.0)

    # BUY aggressor at 50002 — walked through 50001
    fills = sim.on_trade(_trade(50002.0, 0.4, OrderSide.BUY))
    assert len(fills) == 1
    assert fills[0].qty == pytest.approx(0.4)


def test_sell_buy_trade_at_worse_price_no_fill():
    sim = _sim()
    sim.set_book(_snap_ask())
    sim.add_order("a1", OrderSide.SELL, 50001.0, 0.001)
    fills = sim.on_trade(_trade(49999.0, 1.0, OrderSide.BUY))
    assert fills == []


# ---------------------------------------------------------------------------
# remove_order stops fills
# ---------------------------------------------------------------------------

def test_remove_order_stops_fills():
    sim = _sim()
    snap = _snap(bid_qty_at_49999=0.0)
    sim.set_book(snap)
    sim.add_order("b1", OrderSide.BUY, 49999.0, 0.001)
    sim.remove_order("b1")

    fills = sim.on_trade(_trade(49999.0, 1.0, OrderSide.SELL))
    assert fills == []


# ---------------------------------------------------------------------------
# Multiple orders — evaluated independently
# ---------------------------------------------------------------------------

def test_multiple_orders_both_evaluated():
    sim = _sim()
    sim.set_book(_snap(bid_qty_at_49999=0.0))
    sim.add_order("b1", OrderSide.BUY, 49999.0, 0.001)

    snap2 = OrderBookSnapshot(
        symbol="BTC_USDT", venue="paper",
        bids=[PriceLevel(49998.0, 0.0)],
        asks=[PriceLevel(50001.0, 1.0)],
        event_ts=int(time.time() * 1000), send_ts=0,
        recv_ts=time.monotonic_ns(), seq=2,
    )
    sim.set_book(snap2)
    sim.add_order("b2", OrderSide.BUY, 49998.0, 0.002)

    # Walk-through sweep from 49999 down to 49997
    trade = _trade(49997.0, 1.0, OrderSide.SELL)
    fills = sim.on_trade(trade)
    coids = {f.order_id for f in fills}
    assert "b1" in coids
    assert "b2" in coids


# ---------------------------------------------------------------------------
# Walkthrough partial — tick.qty < remaining_qty
# ---------------------------------------------------------------------------

def test_walkthrough_partial_tick_qty_smaller():
    sim = _sim()
    sim.set_book(_snap(bid_qty_at_49999=0.0))
    sim.add_order("b1", OrderSide.BUY, 49999.0, 1.0)

    # Walk-through but only 0.1 in the trade
    trade = _trade(49998.0, 0.1, OrderSide.SELL)
    fills = sim.on_trade(trade)
    assert len(fills) == 1
    assert fills[0].qty == pytest.approx(0.1)  # bounded by tick.qty
    assert sim._orders["b1"].remaining_qty == pytest.approx(0.9)
