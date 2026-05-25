"""Typed configuration loader backed by YAML.

Usage
-----
    from pkg.config import load
    cfg = load("etc/nano-mm.yaml")   # validates on load, raises on bad input
    configure_logger(cfg.log.level, cfg.log.dir)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msgspec
import yaml


class LogConfig(msgspec.Struct, frozen=True):
    level: str = "INFO"
    dir: str = "log"


class NetConfig(msgspec.Struct, frozen=True):
    # HTTP(S) proxy URL applied to both REST and WS clients.
    # Use http://host:port even for SOCKS-capable local proxies (e.g. Clash mixed-port)
    # to avoid the python-socks dependency.
    http_proxy: str | None = None


class PricingConfig(msgspec.Struct, frozen=True):
    # Depth-decay factor for micro-price imbalance weighting
    micro_k: float = 5.0
    # When true, cmd.mm skips the MM engine and shows a raw L2 orderbook view instead.
    debug: bool = False


class SpreadConfig(msgspec.Struct, frozen=True):
    """GLT (Cartea-Jaimungal) spread engine parameters."""
    # γ: risk aversion, units of 1/price. Higher → wider quotes when σ is large.
    # Typical crypto-spot starting range: 0.01 .. 0.5
    gamma: float = 0.1

    # Inventory normalization divisor in lot units.
    # q_lot fed to the formula = q_norm × Q_max.
    # Modeling parameter, NOT a risk control (use q_hard_cap for that).
    Q_max: float = 10.0

    # Base order size (in base asset units) per quote, before inventory taper.
    lot_size: float = 0.001

    # Hard stop: stop quoting one side when |q_norm| crosses this cap on the
    # corresponding inventory direction.
    q_hard_cap: float = 0.95

    # Realized-vol window over mid-price log-returns.
    vol_window_sec: float = 30.0
    vol_min_samples: int = 30

    # Fill-intensity calibration window over aggTrade stream.
    intensity_window_sec: float = 60.0
    intensity_min_trades: int = 50
    intensity_min_filled_bins: int = 3

    # Price tick size for quote rounding (asset-specific).
    # 0.0 = no rounding (caller is responsible).
    price_tick: float = 0.0

    # Half-spread minimum floor (bps).  Prevents GLT from quoting inside the
    # adverse-selection band.  Applied to both δ_b and δ_a after GLT output.
    # 0.0 = disabled.  Recommended starting value: 0.75 (= 1.5 bps full spread).
    spread_floor_bps: float = 0.0

    # Ladder shape: how single GLT (δ_b, δ_a) is expanded into N levels per side.
    ladder_n_levels: int = 3
    # Δ between adjacent levels = ladder_delta_coef · u, where u is the GLT
    # dispersion unit. Auto-scales with σ/k/A.
    ladder_delta_coef: float = 0.5
    # Per-level size weight; len must equal ladder_n_levels, sum to 1.0.
    # Default is internal-thin / external-thick (institutional shape).
    ladder_weights: tuple[float, ...] = (0.15, 0.30, 0.55)
    # Max outer levels dropped on the hit side when |q_norm| = 1.
    # 0 disables ladder-level inventory asymmetry (price skew remains via GLT).
    ladder_n_shrink: int = 2

    # ── Re-quote hysteresis (Ex-4 queue-value protection) ────────────────
    # Each fresh L2 tick recomputes the GLT ladder.  If we publish a new
    # QuoteState every tick, QuoteDiffer cancels/replaces every resting order
    # whenever the quantized ladder shifts by even one tick — destroying queue
    # position.  These thresholds suppress emit unless something meaningful
    # changes.  See CLAUDE.md Ex-4 ("公平价移动 > 1 tick 或库存超阈值才动").
    #
    # Emit (replace self._state with a fresh QuoteState) iff:
    #   - first non-empty state, OR ladder shape (which sides are quoted) changes
    #   - |Δinner_bid| OR |Δinner_ask| > requote_inner_move_ticks × price_tick
    #   - |Δq_norm| ≥ requote_q_norm_threshold
    #   - elapsed since last emit ≥ requote_max_age_ms (heartbeat)
    requote_inner_move_ticks: int = 1
    requote_q_norm_threshold: float = 0.05
    requote_max_age_ms: float = 1000.0


class VenuesConfig(msgspec.Struct, frozen=True):
    # target: the exchange where you place/cancel orders.
    # reference: the exchange you read fair price from (may equal target for self-quoting).
    target: str = "binance_spot"
    reference: str = "binance_spot"


class ArchiveConfig(msgspec.Struct, frozen=True):
    enabled: bool = True
    base_dir: str = "logs"
    # Parquet: flush after this many rows or flush_interval_s seconds (whichever first).
    parquet_flush_rows: int = 1000
    parquet_flush_interval_s: float = 5.0
    # SQLite: flush after this many statements or flush_interval_s seconds.
    sqlite_flush_rows: int = 100
    sqlite_flush_interval_s: float = 1.0


class PaperConfig(msgspec.Struct, frozen=True):
    # Minimum qty increment (asset-specific; BTC default).
    qty_step: float = 0.00001
    # Flat maker fee in bps; set negative for a rebate scenario (0 = no fees).
    maker_fee_bps: float = 0.0
    # Starting normalized inventory fed to GLT on session start.
    initial_q_norm: float = 0.0
    # Force-cancel any resting quote older than this many ms on each book tick.
    # 0 = disabled.  Recommended: 2000 to prune the long tail of stale quotes.
    max_quote_age_ms: float = 0.0
    # In cross-venue mode, cancel the adverse side immediately on a reference
    # aggTrade.  0 = disabled (all trades trigger); positive = min notional USD
    # threshold to filter noise (e.g. 5000 to require ≥ $5k trade).
    ref_trade_cancel_min_notional: float = 0.0


class Config(msgspec.Struct, frozen=True):
    log: LogConfig = msgspec.field(default_factory=LogConfig)
    net: NetConfig = msgspec.field(default_factory=NetConfig)
    pricing_engine: PricingConfig = msgspec.field(default_factory=PricingConfig)
    spread_engine: SpreadConfig = msgspec.field(default_factory=SpreadConfig)
    venues: VenuesConfig = msgspec.field(default_factory=VenuesConfig)
    archive: ArchiveConfig = msgspec.field(default_factory=ArchiveConfig)
    paper: PaperConfig = msgspec.field(default_factory=PaperConfig)


def load(path: str | Path = "etc/nano-mm.yaml") -> Config:
    """Parse and validate *path*, return a Config instance.

    Raises FileNotFoundError if missing, msgspec.ValidationError on schema mismatch.
    """
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return msgspec.convert(raw, Config)
