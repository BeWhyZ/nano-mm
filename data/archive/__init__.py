"""ArchiveManager: implements ArchiveRepo, composes all data/archive sub-modules.

Created and owned by service/archive_service.py. All methods are non-blocking:
writes are enqueued to async writers; background tasks do the actual I/O.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import structlog

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import Fill, Order
from biz.domain.quote import QuoteState
from biz.domain.trade import TradeTick
from biz.repo.archive import ArchiveRepo, FillArchiveCtx, OrderArchiveCtx
from data.archive import fills as _fills
from data.archive import fill_books as _fill_books
from data.archive import latency as _latency
from data.archive import mid_tape as _mid_tape
from data.archive import orders as _orders
from data.archive import quotes as _quotes
from data.archive import trades as _trades
from data.archive.markout_backfill import MarkoutBackfillTask
from data.archive.session import new_session_id, write_session_end, write_session_start
from pkg.storage.parquet_writer import AsyncParquetWriter
from pkg.storage.schema_sql import ALL_DDL
from pkg.storage.sqlite_writer import AsyncSqliteWriter


class ArchiveManager(ArchiveRepo):
    """
    Lifecycle-managed archive layer.

    Call await start() once after construction, await stop() on shutdown.
    All write_* methods are non-blocking hot-path safe.
    """

    def __init__(
        self,
        base_dir: Path | str,
        symbol: str,
        target_venue: str,
        reference_venue: str,
        mode: str = "paper",
        config_snapshot: dict | None = None,
        sqlite_flush_rows: int = 100,
        sqlite_flush_interval_s: float = 1.0,
        parquet_flush_rows: int = 1000,
        parquet_flush_interval_s: float = 5.0,
        lg: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        base = Path(base_dir)
        self._lg = (lg or structlog.get_logger()).bind(component="archive")
        self._symbol = symbol
        self._target_venue = target_venue
        self._reference_venue = reference_venue

        self._sqlite = AsyncSqliteWriter(
            db_path=base / "sqlite" / "nano-mm.db",
            batch_size=sqlite_flush_rows,
            flush_interval_s=sqlite_flush_interval_s,
        )
        self._parquet = AsyncParquetWriter(
            base_dir=base / "parquet",
            batch_size=parquet_flush_rows,
            flush_interval_s=parquet_flush_interval_s,
        )

        self._session_id = new_session_id()
        self._mode = mode
        self._config_snapshot = config_snapshot or {}

        self._lat_archive = _latency.LatencyArchive(
            writer=self._parquet,
            session_id=self._session_id,
        )
        self._markout_task = MarkoutBackfillTask(
            db_path=base / "sqlite" / "nano-mm.db",
            parquet_base=base / "parquet",
            reference_venue=reference_venue,
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    async def start(self) -> None:
        await self._sqlite.start(init_sqls=ALL_DDL)
        await self._parquet.start()
        write_session_start(
            self._sqlite,
            session_id=self._session_id,
            target_venue=self._target_venue,
            reference_venue=self._reference_venue,
            symbol=self._symbol,
            mode=self._mode,
            config_snapshot=self._config_snapshot,
        )
        await self._lat_archive.start()
        await self._markout_task.start()
        self._lg.info("archive_started", session_id=self._session_id)

    async def stop(self) -> None:
        write_session_end(self._sqlite, self._session_id)
        await self._lat_archive.stop()
        await self._markout_task.stop()
        await self._parquet.stop()
        await self._sqlite.stop()
        self._lg.info("archive_stopped", session_id=self._session_id)

    # ------------------------------------------------------------------
    # ArchiveRepo implementation
    # ------------------------------------------------------------------

    def write_order(self, order: Order, ctx: OrderArchiveCtx) -> None:
        _orders.write_order(self._sqlite, order, ctx)

    def write_order_event(
        self,
        order: Order,
        event_type: str,
        seq: int,
        *,
        fill: Fill | None = None,
        reason: str | None = None,
        reject_code: str | None = None,
        is_maker: bool | None = None,
    ) -> None:
        _orders.write_order_event(
            writer=self._sqlite,
            session_id=self._session_id,
            client_order_id=order.client_order_id,
            seq=seq,
            event_type=event_type,
            status_after=order.status.value,
            ladder_level=None,
            exchange_order_id=order.exchange_order_id or None,
            reason=reason,
            reject_code=reject_code,
            trade_id=fill.trade_id if fill else None,
            fill_price=fill.price if fill else None,
            fill_qty=fill.qty if fill else None,
            fill_fee=fill.fee if fill else None,
            fill_fee_asset=fill.fee_asset if fill else None,
            is_maker=int(is_maker) if is_maker is not None else None,
        )

    def write_fill(self, fill: Fill, ctx: FillArchiveCtx) -> None:
        _fills.write_fill(
            self._sqlite,
            fill=fill,
            symbol=self._symbol,
            venue=self._target_venue,
            ctx=ctx,
        )

    def write_quote_snapshot(
        self,
        state: QuoteState,
        target_mid: float,
        ref_mid: float,
        event_type: str,
    ) -> None:
        _quotes.write_quote_snapshot(
            self._parquet,
            state=state,
            target_mid=target_mid,
            ref_mid=ref_mid,
            event_type=event_type,
            session_id=self._session_id,
        )

    def write_trade_tick(self, tick: TradeTick, role: Literal["target", "reference"]) -> None:
        _trades.write_trade_tick(self._parquet, tick=tick, role=role)

    def write_mid_sample(
        self,
        snap: OrderBookSnapshot,
        mid: float,
        micro: float | None,
        role: Literal["target", "reference"],
    ) -> None:
        _mid_tape.write_mid_sample(self._parquet, snap=snap, mid=mid, micro=micro, role=role)

    def write_fill_book(self, trade_id: str, snap: OrderBookSnapshot) -> None:
        _fill_books.write_fill_book(
            self._parquet,
            trade_id=trade_id,
            snap=snap,
            session_id=self._session_id,
        )

    def observe_latency(self, metric: str, us: float) -> None:
        self._lat_archive.observe(metric, us)
