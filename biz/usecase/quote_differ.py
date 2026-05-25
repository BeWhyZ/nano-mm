"""
QuoteDiffer: diff a desired QuoteState ladder against currently active orders and
emit the minimal set of cancel/place actions to reconcile them.

Pure logic — no I/O, no async.

Queue-value discipline (Ex-4):
  An active order at the exact desired (side, quantized-price, quantized-qty) is KEPT
  intact.  Cancelling it would throw away all queued-time accumulated at the venue, so
  we only cancel when the price has moved by ≥ 1 tick or the qty has changed by ≥ 1 step.

Exact-match-after-quantization semantics (no tolerance windows):
  GltSpreadEngine already emits prices snapped to price_tick.  We quantize both sides
  to the same grid and compare integers — no floating-point fuzzy windows, which would
  mask bugs and cause flapping near the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import Order, OrderSide, OrderStatus


@dataclass(frozen=True, slots=True)
class PlaceAction:
    side: OrderSide
    price: float
    qty: float
    ladder_level: int
    snap: OrderBookSnapshot


@dataclass(frozen=True, slots=True)
class CancelAction:
    client_order_id: str


Action = PlaceAction | CancelAction

# States that mean "the order is on (or going to) the venue and should be
# considered when matching desired quotes."
_LIVE_STATUSES = frozenset({
    OrderStatus.PENDING_NEW,
    OrderStatus.OPEN,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.PENDING_CANCEL,
})


def _quantize(value: float, step: float) -> int:
    """Round value to nearest step and return as an integer multiple of step × 1e8."""
    if step <= 0.0:
        # No rounding: represent as fixed-point with 8 decimal places.
        return round(value * 1e8)
    return round(value / step)


def diff(
    state,  # QuoteState — avoid circular import; duck-typed
    snap: OrderBookSnapshot,
    active_orders: list[Order],
    *,
    price_tick: float,
    qty_step: float,
) -> list[Action]:
    """
    Returns a list of Action objects (CancelAction | PlaceAction) representing
    the minimal reconciliation between *state* (desired) and *active_orders* (current).

    Parameters
    ----------
    state       : QuoteState emitted by GltSpreadEngine
    snap        : OrderBookSnapshot that produced *state* (threaded into PlaceAction
                  so PaperExchange can run the post-only check against the right book)
    active_orders: All orders that the strategy owns — any OrderStatus; terminals are
                  filtered internally.
    price_tick  : Venue price increment (e.g. 0.01 for BTC/USDT on Binance)
    qty_step    : Minimum qty increment (e.g. 0.00001 BTC)
    """
    actions: list[Action] = []

    # ── Build desired sets ────────────────────────────────────────────────────
    # key: (side, price_int)  →  (qty_int, ladder_level)
    desired: dict[tuple[OrderSide, int], tuple[int, int]] = {}
    for level_idx, quote in enumerate(state.bids):
        pk = (OrderSide.BUY, _quantize(quote.price, price_tick))
        desired[pk] = (_quantize(quote.size, qty_step), level_idx)
    for level_idx, quote in enumerate(state.asks):
        pk = (OrderSide.SELL, _quantize(quote.price, price_tick))
        desired[pk] = (_quantize(quote.size, qty_step), level_idx)

    # ── Build active sets ─────────────────────────────────────────────────────
    # key: (side, price_int)  →  list[Order]  (insertion order preserved)
    active: dict[tuple[OrderSide, int], list[Order]] = {}
    for order in active_orders:
        if order.status not in _LIVE_STATUSES:
            continue
        pk = (order.side, _quantize(order.price, price_tick))
        active.setdefault(pk, []).append(order)

    # ── Match desired against active ──────────────────────────────────────────
    kept: set[str] = set()  # client_order_ids we decided to keep

    for (side, price_int), (qty_int, level_idx) in desired.items():
        candidates = active.get((side, price_int), [])

        kept_one = False
        for order in candidates:
            order_qty_int = _quantize(order.original_qty, qty_step)
            if not kept_one and order_qty_int == qty_int:
                # Exact match — preserve queue position.
                # PENDING_CANCEL: the cancel is already in-flight; don't try to keep it
                # (it will be gone soon). Place a fresh one.
                if order.status != OrderStatus.PENDING_CANCEL:
                    kept.add(order.client_order_id)
                    kept_one = True
                    continue
            # Extras or qty-mismatch or PENDING_CANCEL → cancel
            if order.client_order_id not in kept:
                actions.append(CancelAction(client_order_id=order.client_order_id))

        # If no match was kept and there's no PENDING_NEW at this key, place a new one.
        if not kept_one:
            # Skip if there is already a PENDING_NEW at this key — avoids double-submit
            # on rapid requote bursts before the first ACK arrives.
            pending_new_exists = any(
                o.status == OrderStatus.PENDING_NEW
                for o in candidates
                if o.client_order_id not in kept
            )
            if not pending_new_exists:
                raw_price = price_int * price_tick if price_tick > 0.0 else price_int / 1e8
                raw_qty = qty_int * qty_step if qty_step > 0.0 else qty_int / 1e8
                actions.append(PlaceAction(
                    side=side,
                    price=raw_price,
                    qty=raw_qty,
                    ladder_level=level_idx,
                    snap=snap,
                ))

    # ── Cancel active orders with no matching desired ─────────────────────────
    for (side, price_int), orders in active.items():
        if (side, price_int) not in desired:
            for order in orders:
                if order.client_order_id not in kept:
                    actions.append(CancelAction(client_order_id=order.client_order_id))

    return actions
