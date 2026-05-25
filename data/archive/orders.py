"""Serialize Order domain objects to SQLite archive."""
from __future__ import annotations

import time

from biz.domain.order import Order
from biz.repo.archive import OrderArchiveCtx
from pkg.storage.sqlite_writer import AsyncSqliteWriter


def write_order(writer: AsyncSqliteWriter, order: Order, ctx: OrderArchiveCtx) -> None:
    writer.put_nowait(
        """INSERT OR IGNORE INTO orders
           (client_order_id, session_id, symbol, target_venue, side, order_type,
            price, original_qty, ladder_level, quote_emit_ts_ns,
            mid_target_at_submit, mid_ref_at_submit, sigma_at_submit,
            A_at_submit, k_at_submit, gamma_at_submit, q_norm_at_submit,
            queue_ahead_at_submit, book_seq_at_submit, submit_ts_ns)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order.client_order_id,
            ctx.session_id,
            order.symbol,
            order.venue,
            order.side.value,
            order.order_type.value,
            order.price,
            order.original_qty,
            ctx.ladder_level,
            ctx.quote_emit_ts_ns,
            ctx.mid_target,
            ctx.mid_ref,
            ctx.sigma,
            ctx.A,
            ctx.k,
            ctx.gamma,
            ctx.q_norm,
            ctx.queue_ahead,
            ctx.book_seq,
            time.time_ns(),
        ),
    )


def write_order_event(
    writer: AsyncSqliteWriter,
    session_id: str,
    client_order_id: str,
    seq: int,
    event_type: str,
    status_after: str,
    *,
    ladder_level: int | None = None,
    exchange_order_id: str | None = None,
    canceled_qty: float | None = None,
    reason: str | None = None,
    reject_code: str | None = None,
    trade_id: str | None = None,
    fill_price: float | None = None,
    fill_qty: float | None = None,
    fill_fee: float | None = None,
    fill_fee_asset: str | None = None,
    is_maker: int | None = None,
) -> None:
    writer.put_nowait(
        """INSERT OR IGNORE INTO order_events
           (session_id, client_order_id, seq, ts_ns, event_type, status_after,
            ladder_level, exchange_order_id, canceled_qty, reason, reject_code,
            trade_id, fill_price, fill_qty, fill_fee, fill_fee_asset, is_maker)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            client_order_id,
            seq,
            time.time_ns(),
            event_type,
            status_after,
            ladder_level,
            exchange_order_id,
            canceled_qty,
            reason,
            reject_code,
            trade_id,
            fill_price,
            fill_qty,
            fill_fee,
            fill_fee_asset,
            is_maker,
        ),
    )
