#!/usr/bin/env python3
"""
check_archive — pre-flight sanity check for paper_mm data collection.

Run after a short warm-up (~1-2 min of paper_mm) to confirm every metric
needed for replay_review is actually being written before committing to a
6-24h run.

Usage:
    uv run python -m cmd.check_archive
    uv run python -m cmd.check_archive --dir logs
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import duckdb

_TTY = sys.stdout.isatty()
GREEN  = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED    = "\033[31m" if _TTY else ""
DIM    = "\033[2m"  if _TTY else ""
RST    = "\033[0m"  if _TTY else ""

OK   = f"{GREEN}✓{RST}"
WARN = f"{YELLOW}~{RST}"
FAIL = f"{RED}✗{RST}"


def _parquet_count(parquet_base: Path, table: str) -> int:
    tdir = parquet_base / table
    if not tdir.exists() or not list(tdir.rglob("*.parquet")):
        return 0
    try:
        return duckdb.sql(
            f"SELECT count(*) FROM read_parquet('{tdir}/**/*.parquet')"
        ).fetchone()[0]
    except Exception:
        return 0


def _parquet_groups(parquet_base: Path, table: str, col: str) -> dict[str, int]:
    tdir = parquet_base / table
    if not tdir.exists() or not list(tdir.rglob("*.parquet")):
        return {}
    try:
        rows = duckdb.sql(
            f"SELECT {col}, count(*) FROM read_parquet('{tdir}/**/*.parquet')"
            f" GROUP BY {col} ORDER BY {col}"
        ).fetchall()
        return {str(r[0]): r[1] for r in rows}
    except Exception:
        return {}


def _sql_count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _groups_str(groups: dict[str, int]) -> str:
    if not groups:
        return ""
    return "  " + DIM + "  ".join(f"{k}:{v}" for k, v in groups.items()) + RST


def check(base_dir: str = "logs") -> int:
    base = Path(base_dir)
    sqlite_path = base / "sqlite" / "nano-mm.db"
    parquet_base = base / "parquet"
    hard_failures = 0

    if not sqlite_path.exists():
        print(f"{FAIL} SQLite not found at {sqlite_path}")
        print("  Start paper_mm first: uv run python -m cmd.paper_mm BTC_USDT")
        return 1

    db = sqlite3.connect(str(sqlite_path))

    # ── Session ──────────────────────────────────────────────────────────────
    print("\n[Session]")
    row = db.execute(
        "SELECT session_id, start_ts_ns, target_venue, reference_venue, symbol, mode"
        " FROM sessions ORDER BY start_ts_ns DESC LIMIT 1"
    ).fetchone()
    if row is None:
        print(f"  {FAIL} no session rows — archive has not started")
        db.close()
        return 1

    sid, start_ns, target, reference, symbol, mode = row
    age_min = (time.time_ns() - start_ns) / 1e9 / 60
    print(f"  {OK} {sid[:8]}…  mode={mode}  symbol={symbol}"
          f"  target={target}  reference={reference}"
          f"  {DIM}started {age_min:.1f}m ago{RST}")

    if target == reference:
        print(f"  {WARN} target==reference  {DIM}markout will be self-polluted;"
              f" switch to cross-venue before the real run (WIP §3.1){RST}")

    # ── SQLite tables ─────────────────────────────────────────────────────────
    print("\n[SQLite]")

    n_orders = _sql_count(db, "orders")
    icon = OK if n_orders > 0 else WARN
    print(f"  {icon} orders          {n_orders:>6} rows")
    if n_orders == 0:
        print(f"         {DIM}no orders yet — wait for at least one quote cycle{RST}")

    evt_counts: dict[str, int] = dict(db.execute(
        "SELECT event_type, count(*) FROM order_events GROUP BY event_type"
    ).fetchall())
    n_events = sum(evt_counts.values())
    icon = OK if n_events > 0 else WARN
    evt_str = DIM + "  ".join(f"{k}:{v}" for k, v in sorted(evt_counts.items())) + RST
    print(f"  {icon} order_events    {n_events:>6} rows  {evt_str}")

    n_rejects = evt_counts.get("REJECT", 0)
    if n_rejects:
        print(f"         {WARN} {n_rejects} REJECT — check post_only_cross rate after long run")

    n_fills = _sql_count(db, "fills")
    print(f"  {'!' if n_fills == 0 else OK} fills           {n_fills:>6} rows")

    if n_fills > 0:
        # Critical context columns — all must be non-NULL for baseline to work
        CRITICAL_COLS = [
            "mid_ref_at_fill",
            "mid_target_at_fill",
            "sigma_at_fill",
            "q_norm_at_fill",
            "ladder_level",
            "spread_at_fill_bps",
            "quote_age_ms",
        ]
        for col in CRITICAL_COLS:
            nulls = db.execute(
                f"SELECT count(*) FROM fills WHERE {col} IS NULL"
            ).fetchone()[0]
            null_pct = nulls / n_fills * 100
            if nulls == 0:
                icon = OK
            elif null_pct < 20:
                icon = WARN
            else:
                icon = FAIL
                hard_failures += 1
            print(f"         {icon} {col:<34} {nulls:>4} NULL / {n_fills}"
                  f"  ({null_pct:.0f}%)")

        # Markout columns come from the async backfill task — NULL is expected
        # during a live session; they get filled in within ~60-90s after fill event.
        markout_null = db.execute(
            "SELECT count(*) FROM fills WHERE markout_1s IS NULL"
        ).fetchone()[0]
        if markout_null == n_fills:
            print(f"         {WARN} markout_1s/5s/60s  all NULL  "
                  f"{DIM}(backfill task runs ~60s after fill — normal for short runs){RST}")
        else:
            filled = n_fills - markout_null
            print(f"         {OK} markout filled  {filled}/{n_fills}")
    else:
        print(f"         {DIM}no fills yet — fill columns will be checked once paper engine fires{RST}")

    db.close()

    # ── Parquet tables ────────────────────────────────────────────────────────
    print("\n[Parquet]")

    PARQUET_TABLES: list[tuple[str, str | None, bool]] = [
        # (table,            group_col,     required)
        ("quote_snapshots", "event_type",  True),
        ("mid_tape",        "role",        True),
        ("trade_tape",      "role",        True),
        ("latency_histograms", "metric_name", False),
        ("fill_books",      None,          False),
    ]

    for table, group_col, required in PARQUET_TABLES:
        n = _parquet_count(parquet_base, table)
        if n > 0:
            icon = OK
        elif required:
            icon = FAIL
            hard_failures += 1
        else:
            icon = WARN

        line = f"  {icon} {table:<24} {n:>6} rows"
        if group_col and n > 0:
            groups = _parquet_groups(parquet_base, table, group_col)
            line += _groups_str(groups)
        print(line)

        if table == "latency_histograms" and n == 0:
            print(f"         {DIM}first dump happens at the 60s interval mark — recheck after a minute{RST}")

    # ── Markout backfill readiness ────────────────────────────────────────────
    print("\n[Markout backfill readiness]")

    ref_count = _parquet_groups(parquet_base, "mid_tape", "role").get("reference", 0)
    icon = OK if ref_count > 0 else FAIL
    if ref_count == 0:
        hard_failures += 1
    print(f"  {icon} mid_tape reference samples  {ref_count}"
          f"  {DIM}(markout_backfill joins on this){RST}")

    if ref_count > 0 and target == reference:
        print(f"  {WARN} reference==target — mid_tape 'reference' samples are the same OB;"
              f" markout will be biased")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if hard_failures == 0:
        print(f"{GREEN}All critical checks passed.{RST}  Archive looks good for a long run.")
    else:
        print(f"{RED}{hard_failures} critical check(s) failed.{RST}"
              f"  Fix before starting the long run.")
    print()
    return 0 if hard_failures == 0 else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Sanity-check the paper_mm archive before a long run.")
    ap.add_argument("--dir", default="logs", help="Archive base_dir (default: logs)")
    args = ap.parse_args()
    sys.exit(check(args.dir))


if __name__ == "__main__":
    main()
