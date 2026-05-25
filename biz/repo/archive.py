"""ArchiveRepo: abstract write interface for the persistence layer.

biz/usecase and data/exchange/oms only import this ABC — they never touch
pkg/storage or data/archive directly. Dependency direction stays clean.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from biz.domain.book import OrderBookSnapshot
from biz.domain.order import Fill, Order
from biz.domain.quote import QuoteState
from biz.domain.trade import TradeTick
from typing import Literal


@dataclass
class OrderArchiveCtx:
    """Strategy context captured at order submit time."""
    session_id: str
    ladder_level: int
    quote_emit_ts_ns: int
    mid_target: float
    mid_ref: float
    sigma: float
    A: float
    k: float
    gamma: float
    q_norm: float
    queue_ahead: float | None   # visible depth at price level (best-effort)
    book_seq: int | None


@dataclass
class FillArchiveCtx:
    """Microstructure + strategy snapshot captured at fill time."""
    session_id: str
    mid_target: float
    mid_ref: float
    spread_bps: float | None
    obi: float | None
    micro_minus_mid_bps: float | None
    book_seq: int | None
    sigma: float | None
    sigma_norm: float | None
    q_norm: float
    ladder_level: int
    aggressor_imbalance_30s: float | None
    quote_emit_ts_ns: int
    inventory_before: float
    inventory_after: float
    q_norm_after: float
    realized_pnl_after: float | None
    unrealized_pnl_at_fill: float | None
    is_ghost_fill: bool
    is_maker: bool = True


class ArchiveRepo(ABC):

    @abstractmethod
    def write_order(self, order: Order, ctx: OrderArchiveCtx) -> None:
        """Persist a newly submitted order (called once per order). Non-blocking."""

    @abstractmethod
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
        """Persist an OMS state-transition event. Non-blocking."""

    @abstractmethod
    def write_fill(self, fill: Fill, ctx: FillArchiveCtx) -> None:
        """Persist a fill with full microstructure context. Non-blocking."""

    @abstractmethod
    def write_quote_snapshot(
        self,
        state: QuoteState,
        target_mid: float,
        ref_mid: float,
        event_type: str,
    ) -> None:
        """Persist a quote snapshot. event_type: 'requote'|'sampled'|'pre_fill'|'pre_cancel'."""

    @abstractmethod
    def write_trade_tick(self, tick: TradeTick, role: Literal["target", "reference"]) -> None:
        """Persist one public trade. role distinguishes target vs reference venue."""

    @abstractmethod
    def write_mid_sample(
        self,
        snap: OrderBookSnapshot,
        mid: float,
        micro: float | None,
        role: Literal["target", "reference"],
    ) -> None:
        """Persist a mid-price sample (for markout backfill lookup). Non-blocking."""

    @abstractmethod
    def write_fill_book(self, trade_id: str, snap: OrderBookSnapshot) -> None:
        """Persist a top-20 OB snapshot triggered by a fill. Non-blocking."""

    @abstractmethod
    def observe_latency(self, metric: str, us: float) -> None:
        """Record one latency sample (µs) into the in-process histogram. Non-blocking."""
