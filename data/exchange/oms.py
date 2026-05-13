"""
In-memory order tracker (OMS state machine).
Implements OrderTrackerRepo; holds all orders in process memory.

Key invariants enforced here (verified in tests):
  - filled_qty + remaining_qty + canceled_qty == original_qty
  - filled_qty is monotonically non-decreasing
  - terminal orders ignore further events (warn + drop, never raise)
  - ghost fill (PENDING_CANCEL + fill) is legal and applied
  - every Fill is deduplicated by trade_id
"""
from __future__ import annotations

import time
from typing import Dict

import structlog

from biz.domain.order import Fill, Order, OrderSide, OrderStatus
from biz.repo.order_tracker import OrderTrackerRepo
from pkg import metrics


class OrderTracker(OrderTrackerRepo):

    def __init__(self, lg: structlog.stdlib.BoundLogger) -> None:
        self.lg = lg.bind(component="order_tracker")
        self._orders: Dict[str, Order] = {}                    # client_order_id → Order
        self._by_exchange_id: Dict[str, str] = {}              # exchange_order_id → client_order_id
        self._inventory: float = 0.0                           # net base asset qty

    # ------------------------------------------------------------------
    # OrderTrackerRepo
    # ------------------------------------------------------------------

    def add(self, order: Order) -> None:
        if order.client_order_id in self._orders:
            self.lg.warning("order_already_exists", coid=order.client_order_id)
            return
        self._orders[order.client_order_id] = order

    def on_ack(self, client_order_id: str, exchange_order_id: str) -> Order:
        order = self._require(client_order_id)
        if order.status.is_terminal:
            self.lg.warning("ack_on_terminal", coid=client_order_id, status=order.status.value)
            return order
        order.exchange_order_id = exchange_order_id
        order.status = OrderStatus.OPEN
        order.updated_ts = time.monotonic_ns()
        if exchange_order_id:
            self._by_exchange_id[exchange_order_id] = client_order_id
        return order

    def on_reject(self, client_order_id: str, reason: str) -> Order:
        order = self._require(client_order_id)
        if order.status.is_terminal:
            self.lg.warning("reject_on_terminal", coid=client_order_id, status=order.status.value)
            return order
        order.status = OrderStatus.REJECTED
        order.updated_ts = time.monotonic_ns()
        self.lg.info("order_rejected", coid=client_order_id, reason=reason)
        metrics.inc("order_rejected_total", venue=order.venue, symbol=order.symbol)
        return order

    def on_fill(self, fill: Fill) -> Order:
        order = self._get_by_fill(fill)
        if order is None:
            self.lg.warning("fill_unknown_order", trade_id=fill.trade_id, coid=fill.order_id)
            return  # type: ignore[return-value]

        if order.status.is_terminal:
            # FILLED is terminal but we may receive a duplicate fill; dedup handles it
            if fill.trade_id in order.applied_trade_ids:
                return order
            self.lg.warning("fill_on_terminal", coid=fill.order_id,
                        status=order.status.value, trade_id=fill.trade_id)
            return order

        applied = order.apply_fill(fill)
        if not applied:
            return order  # duplicate

        # Update inventory: BUY increases base, SELL decreases
        delta = fill.qty if fill.side == OrderSide.BUY else -fill.qty
        self._inventory += delta

        metrics.inc("fill_total", venue=order.venue, symbol=order.symbol)
        metrics.gauge("inventory", self._inventory, symbol=order.symbol, venue=order.venue)

        if fill.trade_id and fill.trade_id in order.applied_trade_ids:
            # Ghost fill detection: PENDING_CANCEL → still PENDING_CANCEL (handled in apply_fill)
            if order.status == OrderStatus.PENDING_CANCEL:
                metrics.inc("ghost_fill_total", venue=order.venue, symbol=order.symbol)

        return order

    def on_cancel_ack(self, client_order_id: str, canceled_qty: float) -> Order:
        order = self._require(client_order_id)
        if order.status == OrderStatus.FILLED:
            # Race: fully filled before cancel reached exchange — no-op
            return order
        if order.status.is_terminal:
            self.lg.warning("cancel_ack_on_terminal", coid=client_order_id, status=order.status.value)
            return order
        order.canceled_qty = canceled_qty
        order.status = OrderStatus.CANCELED
        order.updated_ts = time.monotonic_ns()
        return order

    def on_cancel_reject(self, client_order_id: str, reason: str) -> Order:
        order = self._require(client_order_id)
        if order.status != OrderStatus.PENDING_CANCEL:
            self.lg.warning("cancel_reject_unexpected_state",
                        coid=client_order_id, status=order.status.value)
            return order

        # Cancel rejected: order is likely already filled
        if order.status == OrderStatus.FILLED:
            return order

        # Revert: if partially filled, go back to PARTIALLY_FILLED; else OPEN
        if order.filled_qty > 0:
            order.status = OrderStatus.PARTIALLY_FILLED
        else:
            order.status = OrderStatus.OPEN
        order.updated_ts = time.monotonic_ns()
        metrics.inc("cancel_reject_filled_total", venue=order.venue, symbol=order.symbol)
        self.lg.warning("cancel_rejected", coid=client_order_id, reason=reason,
                    filled_qty=order.filled_qty)
        return order

    def on_expired(self, client_order_id: str) -> Order:
        order = self._require(client_order_id)
        if order.status.is_terminal:
            return order
        order.status = OrderStatus.EXPIRED
        order.updated_ts = time.monotonic_ns()
        return order

    def mark_pending_cancel(self, client_order_id: str) -> Order:
        order = self._require(client_order_id)
        if order.status.is_terminal:
            self.lg.warning("cancel_of_terminal", coid=client_order_id, status=order.status.value)
            return order
        if order.status == OrderStatus.PENDING_CANCEL:
            return order  # already in-flight
        order.status = OrderStatus.PENDING_CANCEL
        order.updated_ts = time.monotonic_ns()
        return order

    def mark_failed(self, client_order_id: str) -> Order:
        order = self._require(client_order_id)
        if order.status.is_terminal:
            return order
        order.status = OrderStatus.FAILED
        order.updated_ts = time.monotonic_ns()
        metrics.inc("order_timeout_total", venue=order.venue, symbol=order.symbol)
        self.lg.error("order_failed_needs_reconcile", coid=client_order_id)
        return order

    def get(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def get_by_exchange_id(self, exchange_order_id: str) -> Order | None:
        coid = self._by_exchange_id.get(exchange_order_id)
        return self._orders.get(coid) if coid else None

    def active_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status.is_active]

    def inventory(self) -> float:
        return self._inventory

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require(self, client_order_id: str) -> Order:
        order = self._orders.get(client_order_id)
        if order is None:
            raise KeyError(f"order not found: {client_order_id}")
        return order

    def _get_by_fill(self, fill: Fill) -> Order | None:
        """Try client_order_id first, then exchange_order_id lookup."""
        order = self._orders.get(fill.order_id)
        if order is None:
            order = self.get_by_exchange_id(fill.order_id)
        return order
