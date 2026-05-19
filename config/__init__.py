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


class Config(msgspec.Struct, frozen=True):
    log: LogConfig = msgspec.field(default_factory=LogConfig)
    net: NetConfig = msgspec.field(default_factory=NetConfig)
    pricing_engine: PricingConfig = msgspec.field(default_factory=PricingConfig)
    spread_engine: SpreadConfig = msgspec.field(default_factory=SpreadConfig)


def load(path: str | Path = "etc/nano-mm.yaml") -> Config:
    """Parse and validate *path*, return a Config instance.

    Raises FileNotFoundError if missing, msgspec.ValidationError on schema mismatch.
    """
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return msgspec.convert(raw, Config)
