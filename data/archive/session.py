"""Session management: create/end session rows."""
from __future__ import annotations

import json
import platform
import subprocess
import time
import uuid

from pkg.storage.sqlite_writer import AsyncSqliteWriter


def _git_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def new_session_id() -> str:
    return str(uuid.uuid4())


def write_session_start(
    writer: AsyncSqliteWriter,
    session_id: str,
    target_venue: str,
    reference_venue: str,
    symbol: str,
    mode: str,
    config_snapshot: dict,
) -> None:
    writer.put_nowait(
        """INSERT OR REPLACE INTO sessions
           (session_id, start_ts_ns, git_sha, hostname, mode,
            target_venue, reference_venue, symbol, config_snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            time.time_ns(),
            _git_sha(),
            platform.node(),
            mode,
            target_venue,
            reference_venue,
            symbol,
            json.dumps(config_snapshot),
        ),
    )


def write_session_end(writer: AsyncSqliteWriter, session_id: str) -> None:
    writer.put_nowait(
        "UPDATE sessions SET end_ts_ns = ? WHERE session_id = ?",
        (time.time_ns(), session_id),
    )
