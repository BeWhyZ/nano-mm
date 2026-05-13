from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"
    LIMIT_MAKER = "limit_maker"  # post-only


class OrderStatus(Enum):
    PENDING_NEW = "pending_new"       # submitted, awaiting venue ACK
    OPEN = "open"                     # venue ACKed, resting
    PARTIALLY_FILLED = "partially_filled"
    PENDING_CANCEL = "pending_cancel" # cancel submitted, awaiting ACK
    FILLED = "filled"                 # terminal
    CANCELED = "canceled"             # terminal (may have partial fills)
    REJECTED = "rejected"             # terminal: venue refused submit
    EXPIRED = "expired"               # terminal: TIF expired
    FAILED = "failed"                 # terminal: unknown state, needs REST reconcile

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """Resting on book (fills can arrive)."""
        return self in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}


_TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
    OrderStatus.FAILED,
})


@dataclass(slots=True)
class Fill:
    trade_id: str    # exchange trade id; global dedup key
    order_id: str    # client_order_id
    price: float
    qty: float
    side: OrderSide
    event_ts: int    # exchange fill time, ms
    recv_ts: int     # local monotonic_ns at receipt
    fee: float = 0.0
    fee_asset: str = ""


@dataclass
class Order:
    client_order_id: str
    symbol: str
    venue: str
    side: OrderSide
    order_type: OrderType
    price: float
    original_qty: float

    status: OrderStatus = field(default=OrderStatus.PENDING_NEW)
    exchange_order_id: str = ""
    filled_qty: float = 0.0
    cost_basis: float = 0.0        # sum(fill.price * fill.qty) for avg price calc
    canceled_qty: float = 0.0
    applied_trade_ids: set[str] = field(default_factory=set)
    created_ts: int = field(default_factory=time.monotonic_ns)
    updated_ts: int = field(default_factory=time.monotonic_ns)

    @property
    def remaining_qty(self) -> float:
        return self.original_qty - self.filled_qty - self.canceled_qty

    @property
    def avg_fill_price(self) -> float:
        return self.cost_basis / self.filled_qty if self.filled_qty > 0 else 0.0

    def apply_fill(self, fill: Fill) -> bool:
        """
        Apply a fill to this order. Returns True if fill was applied, False if
        duplicate. Raises ValueError on invariant violation.
        Ghost fill (status=PENDING_CANCEL) is legal and handled here.
        """
        if fill.trade_id in self.applied_trade_ids:
            return False  # idempotent dedup

        if self.status.is_terminal:
            # Already terminal — this should not happen; log upstream
            return False

        new_filled = self.filled_qty + fill.qty
        eps = 1e-10
        if new_filled > self.original_qty + eps:
            raise ValueError(
                f"fill overfills order {self.client_order_id}: "
                f"filled={self.filled_qty}, fill.qty={fill.qty}, "
                f"original={self.original_qty}"
            )

        self.filled_qty = min(new_filled, self.original_qty)
        self.cost_basis += fill.price * fill.qty
        self.applied_trade_ids.add(fill.trade_id)
        self.updated_ts = fill.recv_ts

        if abs(self.filled_qty - self.original_qty) < eps:
            self.status = OrderStatus.FILLED
        elif self.status not in (OrderStatus.PENDING_CANCEL,):
            # In PENDING_CANCEL we stay there; cancel_ack will finalize
            self.status = OrderStatus.PARTIALLY_FILLED

        return True
