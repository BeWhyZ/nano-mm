#!/usr/bin/env python3
"""
replay_review.py — offline baseline report for a paper trading session.

Usage:
    uv run python -m cmd.replay_review                        # latest session
    uv run python -m cmd.replay_review --session <sid>        # specific session
    uv run python -m cmd.replay_review --db db/sqlite/nano-mm.db
    uv run python -m cmd.replay_review --tick 0.01            # price tick override
"""
from __future__ import annotations

import argparse
import glob as _glob
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

_DEFAULT_DB = Path("db/sqlite/nano-mm.db")
_DEFAULT_PARQUET = Path("db/parquet")
_DEFAULT_TICK = 0.01   # BTC/USDT
_TOXIC_THRESHOLD = 0.5  # × price_tick


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(vals: Sequence[float | None], q: float) -> float | None:
    clean = sorted(v for v in vals if v is not None)
    if not clean:
        return None
    n = len(clean)
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return clean[lo] + (idx - lo) * (clean[hi] - clean[lo])


def _mean(vals: Sequence[float | None]) -> float | None:
    clean = [v for v in vals if v is not None]
    return sum(clean) / len(clean) if clean else None


def _hdr(title: str) -> None:
    bar = "─" * 62
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)


def _fmt(v: float | None, decimals: int = 2, suffix: str = "") -> str:
    if v is None:
        return "  n/a"
    return f"{v:+{8}.{decimals}f}{suffix}"


def _pct_str(v: float | None) -> str:
    if v is None:
        return "  n/a"
    return f"{v * 100:6.1f}%"


# ---------------------------------------------------------------------------
# Session selection
# ---------------------------------------------------------------------------

def _pick_session(conn: sqlite3.Connection, requested: str | None) -> tuple[str, dict[str, object | None]]:
    if requested:
        row = conn.execute(
            "SELECT session_id, symbol, target_venue, reference_venue, mode, start_ts_ns, end_ts_ns"
            " FROM sessions WHERE session_id = ?", (requested,)
        ).fetchone()
        if row is None:
            sys.exit(f"session not found: {requested!r}")
    else:
        row = conn.execute(
            "SELECT session_id, symbol, target_venue, reference_venue, mode, start_ts_ns, end_ts_ns"
            " FROM sessions ORDER BY start_ts_ns DESC LIMIT 1"
        ).fetchone()
        if row is None:
            sys.exit("no sessions found — has paper_mm been run?")

    sid, symbol, target, reference, mode, start_ns, end_ns = row
    duration_s = (end_ns - start_ns) / 1e9 if end_ns else None
    return sid, {
        "session_id": sid, "symbol": symbol, "target": target,
        "reference": reference, "mode": mode,
        "start_ns": start_ns, "duration_s": duration_s,
    }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _section_session(meta: dict[str, object | None]) -> None:
    _hdr("SESSION")
    ds = float(meta["duration_s"]) if meta["duration_s"] is not None else None  # type: ignore[arg-type]
    dur = f"{ds:.0f}s  ({ds/3600:.1f}h)" if ds is not None else "running"
    print(f"  id:        {meta['session_id']}")
    print(f"  symbol:    {meta['symbol']}   mode={meta['mode']}")
    print(f"  target:    {meta['target']}   reference={meta['reference']}")
    print(f"  duration:  {dur}")


def _section_fills_summary(conn: sqlite3.Connection, sid: str, tick: float) -> int:  # noqa: ARG001
    _hdr("FILL SUMMARY")
    rows = conn.execute(
        "SELECT side, count(*) FROM fills WHERE session_id=? GROUP BY side", (sid,)
    ).fetchall()
    if not rows:
        print("  no fills yet")
        return 0
    counts = {r[0]: r[1] for r in rows}
    total: int = sum(counts.values())
    buys = counts.get("buy", 0)
    sells = counts.get("sell", 0)

    ghost = conn.execute(
        "SELECT count(*) FROM fills WHERE session_id=? AND is_ghost_fill=1", (sid,)
    ).fetchone()[0]
    marked = conn.execute(
        "SELECT count(*) FROM fills WHERE session_id=? AND markout_60s IS NOT NULL", (sid,)
    ).fetchone()[0]

    print(f"  total fills:  {total}  (buy={buys}, sell={sells}, ghost={ghost})")
    print(f"  markout done: {marked}/{total}", end="")
    if marked < total:
        print("  ← backfill pending; markout sections may be incomplete", end="")
    print()
    return total


def _section_markout(conn: sqlite3.Connection, sid: str, tick: float) -> None:
    _hdr("MARKOUT  (bps = markout / fill_price × 10000)")
    rows = conn.execute(
        "SELECT price, markout_1s, markout_5s, markout_30s, markout_60s"
        " FROM fills WHERE session_id=? AND markout_1s IS NOT NULL", (sid,)
    ).fetchall()
    if not rows:
        print("  no markout data yet — wait for backfill (fills need to be ≥65s old)")
        return

    horizons = [
        ("1s",  [r[1] / r[0] * 1e4 for r in rows]),
        ("5s",  [r[2] / r[0] * 1e4 for r in rows if r[2] is not None]),
        ("30s", [r[3] / r[0] * 1e4 for r in rows if r[3] is not None]),
        ("60s", [r[4] / r[0] * 1e4 for r in rows if r[4] is not None]),
    ]
    print(f"  {'horizon':>8}  {'n':>5}  {'mean':>8}  {'p10':>8}  {'p50':>8}  {'p90':>8}")
    for label, vals in horizons:
        if not vals:
            continue
        print(
            f"  {label:>8}  {len(vals):>5}"
            f"  {_fmt(_mean(vals), 3):>8}"
            f"  {_fmt(_pct(vals, 0.1), 3):>8}"
            f"  {_fmt(_pct(vals, 0.5), 3):>8}"
            f"  {_fmt(_pct(vals, 0.9), 3):>8}"
        )

    # Guidance
    m1 = _mean(horizons[0][1])
    if m1 is not None:
        flag = "  ← WARN: toxic" if m1 < -0.5 else ("  ← ok" if m1 >= -0.2 else "  ← watch")
        print(f"\n  markout@1s mean = {m1:+.3f} bps{flag}")


def _section_toxic(conn: sqlite3.Connection, sid: str, tick: float) -> None:
    threshold = _TOXIC_THRESHOLD * tick
    _hdr(f"TOXIC RATIO  (markout < −{_TOXIC_THRESHOLD}×tick = −{threshold} price units)")
    rows = conn.execute(
        "SELECT markout_1s, markout_5s, markout_30s, markout_60s"
        " FROM fills WHERE session_id=? AND markout_1s IS NOT NULL", (sid,)
    ).fetchall()
    if not rows:
        print("  no markout data yet")
        return

    n = len(rows)
    print(f"  {'horizon':>8}  {'toxic':>8}  {'total':>6}")
    for label, idx in [("1s", 0), ("5s", 1), ("30s", 2), ("60s", 3)]:
        valid = [r[idx] for r in rows if r[idx] is not None]
        toxic = sum(1 for v in valid if v < -threshold)
        ratio = toxic / len(valid) if valid else None
        flag = ""
        if ratio is not None:
            flag = "  ← WARN" if ratio > 0.35 else ("  ← ok" if ratio < 0.25 else "")
        print(f"  {label:>8}  {_pct_str(ratio):>8}  {len(valid):>6}{flag}")


def _section_spread_capture(
    conn: sqlite3.Connection, sid: str, parquet_base: Path
) -> None:
    """Spread capture using OUR inner-quote spread as denominator.

    Denominator priority:
      1. quote_snapshots.inner_spread_bps via ASOF JOIN on fills.recv_ts_ns
         (both wall clock — quote_emit_ts_ns is monotonic and not comparable)
      2. fall back to BBO `spread_at_fill_bps` with explicit warning
    """
    _hdr("SPREAD CAPTURE  (denominator = our quoted inner spread)")
    rows = conn.execute(
        "SELECT trade_id, recv_ts_ns, price, markout_1s, spread_at_fill_bps"
        " FROM fills WHERE session_id=? AND markout_1s IS NOT NULL",
        (sid,)
    ).fetchall()
    if not rows:
        print("  no data")
        return

    inner_by_trade: dict[str, float] = {}
    qs_glob = str(parquet_base / "quote_snapshots" / "**" / "*.parquet")
    qs_files = _glob.glob(qs_glob, recursive=True)

    if qs_files:
        try:
            import duckdb
            duck = duckdb.connect()
            try:
                duck.execute("SET threads=1")
                duck.execute(
                    "CREATE TEMP TABLE _fills(trade_id TEXT, recv_ts_ns BIGINT)"
                )
                duck.executemany(
                    "INSERT INTO _fills VALUES (?,?)",
                    [(r[0], r[1]) for r in rows],
                )
                # ASOF JOIN: latest quote_snapshot before fill.recv_ts (wall ts).
                # Same session only.
                join_rows = duck.execute(
                    """
                    SELECT f.trade_id, q.inner_spread_bps
                    FROM   _fills f
                    ASOF LEFT JOIN read_parquet($files, union_by_name=true) q
                      ON   q.session_id    = $sid
                     AND   f.recv_ts_ns   >= q.ts_ns
                    """,
                    {"files": qs_files, "sid": sid},
                ).fetchall()
                for tid, inner in join_rows:
                    if inner is not None:
                        inner_by_trade[tid] = float(inner)
            finally:
                duck.close()
        except Exception as e:  # noqa: BLE001
            print(f"  duckdb JOIN failed ({e}); falling back to BBO denominator.")

    bbo_vals: list[float] = []
    inner_vals: list[float] = []
    drift_vals: list[float] = []  # 2 × markout in bps; negative = adverse
    for trade_id, _ts, price, m1, bbo in rows:
        if price <= 0 or m1 is None:
            continue
        drift_vals.append(m1 / price * 1e4 * 2)
        if bbo is not None:
            bbo_vals.append(bbo)
        inner = inner_by_trade.get(trade_id)
        if inner is not None:
            inner_vals.append(inner)

    q_inner = _mean(inner_vals)
    q_bbo = _mean(bbo_vals)
    drift = _mean(drift_vals)

    print(f"  our inner spread (n={len(inner_vals)}/{len(rows)}):  {_fmt(q_inner, 3)} bps")
    print(f"  BBO at fill (target venue):              {_fmt(q_bbo, 3)} bps  (informational)")
    print(f"  2×markout @1s (post-fill mid drift):     {_fmt(drift, 3)} bps")

    if q_inner is not None and drift is not None and q_inner > 0:
        # Hasbrouck-style: realized = quoted + 2*markout_signed_toward_us
        # markout > 0 = good for us, so we add (not subtract)
        realized = q_inner + drift
        capture = realized / q_inner
        flag = "← ok" if capture >= 0.3 else "← WARN: spread mostly eaten by adverse selection"
        print(f"  realized = quoted + 2×markout:           {_fmt(realized, 3)} bps")
        print(f"  capture ratio @1s:                       {_fmt(capture, 3)} {flag}")
    else:
        print("  capture ratio: n/a (no inner_spread_bps overlap with fills)")


def _section_by_level(conn: sqlite3.Connection, sid: str, tick: float) -> None:
    threshold = _TOXIC_THRESHOLD * tick
    _hdr("MARKOUT BY LADDER LEVEL  (markout_1s bps)")
    rows = conn.execute(
        "SELECT ladder_level, price, markout_1s"
        " FROM fills WHERE session_id=? AND markout_1s IS NOT NULL"
        " ORDER BY ladder_level", (sid,)
    ).fetchall()
    if not rows:
        print("  no data")
        return

    from collections import defaultdict
    by_level: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for level, price, m1 in rows:
        by_level[level].append((price, m1))

    print(f"  {'level':>6}  {'n':>5}  {'mean_1s':>9}  {'toxic_1s':>10}")
    for level in sorted(by_level):
        vals = by_level[level]
        m1_bps = [m / p * 1e4 for p, m in vals]
        toxic = sum(1 for _, m in vals if m < -threshold)
        print(
            f"  {level:>6}  {len(vals):>5}"
            f"  {_fmt(_mean(m1_bps), 3):>9}"
            f"  {_pct_str(toxic / len(vals)):>10}"
        )


def _section_inventory(conn: sqlite3.Connection, sid: str) -> None:
    _hdr("INVENTORY & PNL")
    rows = conn.execute(
        "SELECT q_norm_after, inventory_after, realized_pnl_after, unrealized_pnl_at_fill"
        " FROM fills WHERE session_id=? ORDER BY recv_ts_ns", (sid,)
    ).fetchall()
    if not rows:
        print("  no fills")
        return

    q_norms = [r[0] for r in rows if r[0] is not None]
    last = rows[-1]
    cap_touches = sum(1 for q in q_norms if abs(q) > 0.95)

    print(f"  inventory now:  {last[1]:+.6f}   q_norm: {last[0]:+.3f}")
    if q_norms:
        print(f"  q_norm range:   {min(q_norms):+.3f} .. {max(q_norms):+.3f}")
    print(f"  cap touches (|q|>0.95): {cap_touches}")
    print(f"  realized PnL:   {_fmt(last[2], 4)} USDT")
    print(f"  unrealized est: {_fmt(last[3], 4)} USDT  (mark-to-mid proxy at last fill)")


def _section_rejects(conn: sqlite3.Connection, sid: str) -> None:
    _hdr("ORDER REJECTS")
    total_orders = conn.execute(
        "SELECT count(*) FROM orders WHERE session_id=?", (sid,)
    ).fetchone()[0]
    rejects = conn.execute(
        "SELECT reject_code, count(*) FROM order_events"
        " WHERE session_id=? AND event_type='REJECT'"
        " GROUP BY reject_code ORDER BY count(*) DESC", (sid,)
    ).fetchall()
    total_rejects = sum(r[1] for r in rejects)

    print(f"  total orders placed: {total_orders}   rejects: {total_rejects}", end="")
    if total_orders:
        print(f"  ({total_rejects / total_orders * 100:.1f}%)")
    else:
        print()
    for code, n in rejects:
        print(f"    {code or 'unknown':30s}: {n}")


def _section_markout_from_emit(conn: sqlite3.Connection, sid: str) -> None:
    """Compare markout-from-fill vs markout-from-emit.

    Δ = from_emit − from_fill:
      Δ > 0  → quote was already off-ref when emitted (stale-at-emit)
      Δ ≈ 0  → quote was tight at emit; lost in the ≤1s window after fill
      Δ < 0  → ref drifted in our favor during quote rest (rare for taker side)

    NOTE: depends on mid_ref_at_fill being correctly populated. Sessions
    written before the 2026-05-25 mid_ref fix had mid_ref ≡ mid_target;
    Δ on those sessions only reflects target-vs-ref offset, not staleness.
    """
    _hdr("MARKOUT FROM EMIT  (from_emit vs from_fill, mean bps)")
    rows = conn.execute(
        "SELECT price, markout_5s, markout_60s,"
        " markout_from_emit_5s, markout_from_emit_60s"
        " FROM fills WHERE session_id=? AND markout_5s IS NOT NULL",
        (sid,),
    ).fetchall()
    if not rows:
        print("  no data")
        return

    def _bps(idx: int) -> list[float]:
        return [r[idx] / r[0] * 1e4 for r in rows if r[idx] is not None and r[0]]

    f5, f60 = _bps(1), _bps(2)
    e5, e60 = _bps(3), _bps(4)
    if not e5 and not e60:
        print("  markout_from_emit_* not populated yet (needs backfill or mid_ref fix)")
        return

    print(f"  {'horizon':>8}  {'n':>4}  {'from_fill':>12}  {'from_emit':>12}  {'Δ (emit−fill)':>16}")
    for label, ff, fe in [("5s", f5, e5), ("60s", f60, e60)]:
        m_ff = _mean(ff)
        m_fe = _mean(fe)
        delta = (m_fe - m_ff) if (m_fe is not None and m_ff is not None) else None
        print(
            f"  {label:>8}  {len(fe):>4}"
            f"  {_fmt(m_ff, 3):>12}  {_fmt(m_fe, 3):>12}"
            f"  {_fmt(delta, 3):>16}"
        )


def _section_order_activity(conn: sqlite3.Connection, sid: str) -> None:
    """Order placement / cancel / fill activity + rest-time distribution.

    Diagnostic for QuoteDiffer churn vs queue patience.
    """
    _hdr("ORDER ACTIVITY")
    total_orders = conn.execute(
        "SELECT count(*) FROM orders WHERE session_id=?", (sid,)
    ).fetchone()[0]
    total_fills = conn.execute(
        "SELECT count(*) FROM fills WHERE session_id=?", (sid,)
    ).fetchone()[0]
    cancels = conn.execute(
        "SELECT count(*) FROM order_events"
        " WHERE session_id=? AND event_type='CANCEL_ACK'",
        (sid,),
    ).fetchone()[0]
    rejects = conn.execute(
        "SELECT count(*) FROM order_events"
        " WHERE session_id=? AND event_type='REJECT'",
        (sid,),
    ).fetchone()[0]

    if not total_orders:
        print("  no orders")
        return

    fill_rate = total_fills / total_orders * 100
    cancel_rate = cancels / total_orders * 100
    reject_rate = rejects / total_orders * 100
    print(f"  orders placed:    {total_orders}")
    print(f"  fills:            {total_fills}  ({fill_rate:.2f}% per order)")
    print(f"  cancels acked:    {cancels}  ({cancel_rate:.1f}% per order)")
    print(f"  rejects:          {rejects}  ({reject_rate:.2f}% per order)")
    if total_fills:
        ctf = cancels / total_fills
        churn = (
            "← QuoteDiffer churns heavily — re-quote threshold likely too tight"
            if ctf > 50 else
            "← reasonable patience" if ctf > 5 else
            "← quotes rest long (queue-patient regime)"
        )
        print(f"  cancel-to-fill:   {ctf:.1f}x  {churn}")

    # Rest time = submit_ts_ns → first terminal event (CANCEL_ACK or FILL)
    rest_rows = conn.execute(
        """
        SELECT o.submit_ts_ns,
               MIN(e.ts_ns) AS first_terminal_ns
        FROM   orders o
        JOIN   order_events e
          ON   e.session_id = o.session_id
         AND   e.client_order_id = o.client_order_id
         AND   e.event_type IN ('CANCEL_ACK', 'FILL')
        WHERE  o.session_id = ?
        GROUP  BY o.client_order_id, o.submit_ts_ns
        """,
        (sid,),
    ).fetchall()
    rest_ms = [(e - s) / 1e6 for s, e in rest_rows if e and s and e >= s]
    if rest_ms:
        print(
            f"  rest time:        mean={_mean(rest_ms):.0f}ms"
            f"  p50={_pct(rest_ms, 0.5):.0f}ms"
            f"  p90={_pct(rest_ms, 0.9):.0f}ms"
            f"  p99={_pct(rest_ms, 0.99):.0f}ms"
        )


def _section_quote_age(conn: sqlite3.Connection, sid: str) -> None:
    _hdr("QUOTE AGE AT FILL  (GLT emit → fill recv)")
    rows = conn.execute(
        "SELECT quote_age_ms FROM fills WHERE session_id=? AND quote_age_ms IS NOT NULL",
        (sid,)
    ).fetchall()
    if not rows:
        print("  no data")
        return
    vals = [r[0] for r in rows]
    print(
        f"  mean={_mean(vals):.0f}ms  "
        f"p50={_pct(vals, 0.5):.0f}ms  "
        f"p99={_pct(vals, 0.99):.0f}ms"
    )
    note = "  (how old your quote was when it got hit — NOT tick-to-order latency)"
    print(note)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Offline review of a paper trading session")
    ap.add_argument("--db", type=Path, default=_DEFAULT_DB)
    ap.add_argument("--parquet", type=Path, default=_DEFAULT_PARQUET, help="parquet base dir")
    ap.add_argument("--session", default=None, help="session_id (default: latest)")
    ap.add_argument("--tick", type=float, default=_DEFAULT_TICK, help="price tick size")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}\nRun paper_mm first to generate archive data.")

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row

    sid, meta = _pick_session(conn, args.session)

    _section_session(meta)
    total_fills = _section_fills_summary(conn, sid, args.tick)

    if total_fills > 0:
        _section_markout(conn, sid, args.tick)
        _section_markout_from_emit(conn, sid)
        _section_toxic(conn, sid, args.tick)
        _section_spread_capture(conn, sid, args.parquet)
        _section_by_level(conn, sid, args.tick)
        _section_inventory(conn, sid)

    _section_order_activity(conn, sid)
    _section_rejects(conn, sid)
    _section_quote_age(conn, sid)

    print()
    conn.close()


if __name__ == "__main__":
    main()
