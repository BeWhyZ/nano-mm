#!/usr/bin/env python3
"""
paper_mm — paper-trading market-making entry point.

Runs the full MM loop (GLT spread engine + ladder) against live market data
but simulates fills internally instead of sending orders to the exchange.
All orders, fills, and PnL flow through the same archive as a live session
(mode = "paper").

Usage:
    uv run python -m cmd.paper_mm [SYMBOL]
    uv run python -m cmd.paper_mm BTC_USDT
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import aiohttp

import config
from pkg import logger
from pkg.constant import Exchange
from server.mm_server import MMServer
from service.archive_service import ArchiveService
from service.paper_executor_service import PaperExecutor

_TTY = sys.stdout.isatty()
_RED   = "\033[31m" if _TTY else ""
_GREEN = "\033[32m" if _TTY else ""
_CYAN  = "\033[36m" if _TTY else ""
_DIM   = "\033[2m"  if _TTY else ""
_RST   = "\033[0m"  if _TTY else ""

_ob_lines = 0


def _fmt_side(levels: Any) -> str:
    if not levels:
        return "  -  "
    prices = "/".join(f"{q.price:.4f}" for q in levels)
    total = sum(q.size for q in levels)
    return f"[{len(levels)}]{prices} Σ{total:g}"


def _on_quote(s: Any) -> None:
    if not s.bids and not s.asks:
        line = (
            f"{s.symbol:<10} mid={s.mid:>10.4f}  "
            f"calibrating s={s.sigma:.4f} A={s.A:.2f} k={s.k:.4f}"
        )
    else:
        line = (
            f"{s.symbol:<10} mid={s.mid:>10.4f}  "
            f"bid={_fmt_side(s.bids):<48} ask={_fmt_side(s.asks):<48}  "
            f"s={s.sigma:.4f} A={s.A:.1f} k={s.k:.4f} q={s.q_norm:+.2f}"
        )
    print("\r" + line.ljust(180), end="", flush=True)


async def run_paper(symbol: str, cfg: config.Config) -> None:
    lg = logger.get_logger("paper_mm")
    lg.info("paper_mm_start", symbol=symbol)
    print(f"Starting PAPER MM for {symbol} -- Ctrl-C to stop")

    archive_svc = ArchiveService(
        symbol=symbol,
        venues_cfg=cfg.venues,
        archive_cfg=cfg.archive,
        full_config=cfg,
        lg=lg,
        mode="paper",
    )
    await archive_svc.start()

    async with aiohttp.ClientSession() as session:
        srv = MMServer(
            symbol=symbol,
            session=session,
            cfg=cfg,
            on_quote=_on_quote,
            lg=lg,
            archive=archive_svc.manager,
            proxy=cfg.net.http_proxy,
        )

        target_venue = cfg.venues.target

        executor = PaperExecutor(
            symbol=symbol,
            venue=target_venue,
            mm_service=srv._svc,
            spread_cfg=cfg.spread_engine,
            paper_cfg=cfg.paper,
            session_id=archive_svc.session_id or "unknown",
            archive=archive_svc.manager,
            lg=lg,
        )

        try:
            await srv.run()
        except asyncio.CancelledError:
            pass
        finally:
            if _TTY:
                print()

    await archive_svc.stop()


if __name__ == "__main__":
    cfg = config.load()
    logger.configure(level=cfg.log.level, log_dir=cfg.log.dir)

    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTC_USDT"
    try:
        asyncio.run(run_paper(sym, cfg))
    except KeyboardInterrupt:
        pass
