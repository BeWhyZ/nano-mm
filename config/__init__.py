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


class Config(msgspec.Struct, frozen=True):
    log: LogConfig = msgspec.field(default_factory=LogConfig)
    net: NetConfig = msgspec.field(default_factory=NetConfig)


def load(path: str | Path = "etc/nano-mm.yaml") -> Config:
    """Parse and validate *path*, return a Config instance.

    Raises FileNotFoundError if missing, msgspec.ValidationError on schema mismatch.
    """
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return msgspec.convert(raw, Config)
