"""Serialize Fill domain objects to SQLite archive."""
from __future__ import annotations

import time

from biz.domain.order import Fill
from biz.repo.archive import FillArchiveCtx
from pkg.storage.sqlite_writer import AsyncSqliteWriter


def write_fill(
    writer: AsyncSqliteWriter,
    fill: Fill,
    symbol: str,
    venue: str,
    ctx: FillArchiveCtx,
) -> None:
    recv_ts_wall = time.time_ns()
    # quote_emit_ts_ns uses monotonic clock; use monotonic here too to avoid
    # epoch mismatch with time_ns() (wall clock differs by ~53 years from boot).
    quote_age_ms = (
        (time.monotonic_ns() - ctx.quote_emit_ts_ns) / 1e6
        if ctx.quote_emit_ts_ns > 0
        else None
    )
    # fee_quote_ccy: normalize fee to quote currency using ref mid when fee is in base
    fee_quote_ccy = (
        fill.fee * ctx.mid_ref
        if fill.fee_asset and fill.fee_asset.upper() not in ("USDT", "USDC", "BUSD", "")
        else fill.fee
    )

    writer.put_nowait(
        """INSERT OR IGNORE INTO fills
           (trade_id, session_id, client_order_id, symbol, target_venue,
            side, price, qty, fee, fee_asset, fee_quote_ccy,
            is_maker, is_ghost_fill,
            mid_target_at_fill, mid_ref_at_fill, spread_at_fill_bps,
            obi_at_fill, micro_minus_mid_bps_at_fill, book_seq_at_fill,
            sigma_at_fill, sigma_norm_at_fill, q_norm_at_fill,
            ladder_level, aggressor_imbalance_30s,
            quote_emit_ts_ns, quote_age_ms,
            inventory_before, inventory_after, q_norm_after,
            realized_pnl_after, unrealized_pnl_at_fill,
            event_ts_ms, recv_ts_ns)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            fill.trade_id,
            ctx.session_id,
            fill.order_id,
            symbol,
            venue,
            fill.side.value,
            fill.price,
            fill.qty,
            fill.fee,
            fill.fee_asset or None,
            fee_quote_ccy,
            int(ctx.is_maker),
            int(ctx.is_ghost_fill),
            ctx.mid_target,
            ctx.mid_ref,
            ctx.spread_bps,
            ctx.obi,
            ctx.micro_minus_mid_bps,
            ctx.book_seq,
            ctx.sigma,
            ctx.sigma_norm,
            ctx.q_norm,
            ctx.ladder_level,
            ctx.aggressor_imbalance_30s,
            ctx.quote_emit_ts_ns,
            quote_age_ms,
            ctx.inventory_before,
            ctx.inventory_after,
            ctx.q_norm_after,
            ctx.realized_pnl_after,
            ctx.unrealized_pnl_at_fill,
            fill.event_ts,
            recv_ts_wall,
        ),
    )
