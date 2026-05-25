"""ArchiveService: lifecycle wrapper for ArchiveManager.

Owns the ArchiveManager and wires it into MMService listeners.
Provides a single start()/stop() pair for cmd/mm.py.
"""
from __future__ import annotations

import msgspec
import structlog

from config import ArchiveConfig, VenuesConfig
from data.archive import ArchiveManager


class ArchiveService:
    """Thin lifecycle owner around ArchiveManager."""

    def __init__(
        self,
        symbol: str,
        venues_cfg: VenuesConfig,
        archive_cfg: ArchiveConfig,
        full_config: object,
        lg: structlog.stdlib.BoundLogger,
        mode: str = "paper",
    ) -> None:
        self._enabled = archive_cfg.enabled
        self._manager: ArchiveManager | None = None

        if not self._enabled:
            return

        try:
            config_snapshot = msgspec.to_builtins(full_config)  # type: ignore[arg-type]
        except Exception:
            config_snapshot = {}

        self._manager = ArchiveManager(
            base_dir=archive_cfg.base_dir,
            symbol=symbol,
            target_venue=venues_cfg.target,
            reference_venue=venues_cfg.reference,
            mode=mode,
            config_snapshot=config_snapshot,
            sqlite_flush_rows=archive_cfg.sqlite_flush_rows,
            sqlite_flush_interval_s=archive_cfg.sqlite_flush_interval_s,
            parquet_flush_rows=archive_cfg.parquet_flush_rows,
            parquet_flush_interval_s=archive_cfg.parquet_flush_interval_s,
            lg=lg,
        )

    @property
    def manager(self) -> ArchiveManager | None:
        return self._manager

    @property
    def session_id(self) -> str | None:
        return self._manager.session_id if self._manager else None

    async def start(self) -> None:
        if self._manager is not None:
            await self._manager.start()

    async def stop(self) -> None:
        if self._manager is not None:
            await self._manager.stop()
