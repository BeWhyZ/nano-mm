"""SQLite DDL for all archive tables."""

SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    start_ts_ns       INTEGER NOT NULL,
    end_ts_ns         INTEGER,
    git_sha           TEXT,
    hostname          TEXT,
    mode              TEXT NOT NULL,
    target_venue      TEXT NOT NULL,
    reference_venue   TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    config_snapshot   TEXT NOT NULL,
    schema_version    INTEGER NOT NULL DEFAULT 1
)
"""

ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id        TEXT NOT NULL,
    session_id             TEXT NOT NULL,
    exchange_order_id      TEXT,
    symbol                 TEXT NOT NULL,
    target_venue           TEXT NOT NULL,
    side                   TEXT NOT NULL,
    order_type             TEXT NOT NULL,
    price                  REAL NOT NULL,
    original_qty           REAL NOT NULL,
    ladder_level           INTEGER NOT NULL,
    quote_emit_ts_ns       INTEGER NOT NULL,
    mid_target_at_submit   REAL,
    mid_ref_at_submit      REAL,
    sigma_at_submit        REAL,
    A_at_submit            REAL,
    k_at_submit            REAL,
    gamma_at_submit        REAL,
    q_norm_at_submit       REAL,
    queue_ahead_at_submit  REAL,
    book_seq_at_submit     INTEGER,
    submit_ts_ns           INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, client_order_id)
)
"""

ORDER_EVENTS = """
CREATE TABLE IF NOT EXISTS order_events (
    session_id        TEXT NOT NULL,
    client_order_id   TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    ts_ns             INTEGER NOT NULL,
    event_type        TEXT NOT NULL,
    status_after      TEXT NOT NULL,
    ladder_level      INTEGER,
    exchange_order_id TEXT,
    canceled_qty      REAL,
    reason            TEXT,
    reject_code       TEXT,
    trade_id          TEXT,
    fill_price        REAL,
    fill_qty          REAL,
    fill_fee          REAL,
    fill_fee_asset    TEXT,
    is_maker          INTEGER,
    schema_version    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, client_order_id, seq)
)
"""

FILLS = """
CREATE TABLE IF NOT EXISTS fills (
    trade_id                     TEXT PRIMARY KEY,
    session_id                   TEXT NOT NULL,
    client_order_id              TEXT NOT NULL,
    symbol                       TEXT NOT NULL,
    target_venue                 TEXT NOT NULL,
    side                         TEXT NOT NULL,
    price                        REAL NOT NULL,
    qty                          REAL NOT NULL,
    fee                          REAL NOT NULL DEFAULT 0.0,
    fee_asset                    TEXT,
    fee_quote_ccy                REAL,
    is_maker                     INTEGER NOT NULL DEFAULT 1,
    is_ghost_fill                INTEGER NOT NULL DEFAULT 0,
    mid_target_at_fill           REAL,
    mid_ref_at_fill              REAL,
    spread_at_fill_bps           REAL,
    obi_at_fill                  REAL,
    micro_minus_mid_bps_at_fill  REAL,
    book_seq_at_fill             INTEGER,
    sigma_at_fill                REAL,
    sigma_norm_at_fill           REAL,
    q_norm_at_fill               REAL,
    ladder_level                 INTEGER,
    aggressor_imbalance_30s      REAL,
    quote_emit_ts_ns             INTEGER,
    quote_age_ms                 REAL,
    inventory_before             REAL,
    inventory_after              REAL,
    q_norm_after                 REAL,
    realized_pnl_after           REAL,
    unrealized_pnl_at_fill       REAL,
    mid_ref_1s                   REAL,
    mid_ref_5s                   REAL,
    mid_ref_30s                  REAL,
    mid_ref_60s                  REAL,
    mid_samples_count_1s         INTEGER,
    mid_samples_count_5s         INTEGER,
    mid_samples_count_30s        INTEGER,
    mid_samples_count_60s        INTEGER,
    markout_1s                   REAL,
    markout_5s                   REAL,
    markout_30s                  REAL,
    markout_60s                  REAL,
    markout_from_emit_5s         REAL,
    markout_from_emit_60s        REAL,
    event_ts_ms                  INTEGER,
    recv_ts_ns                   INTEGER NOT NULL,
    schema_version               INTEGER NOT NULL DEFAULT 1
)
"""

FILLS_IDX_SYMBOL_TS = (
    "CREATE INDEX IF NOT EXISTS idx_fills_symbol_ts ON fills (symbol, recv_ts_ns)"
)
FILLS_IDX_UNMARKED = (
    "CREATE INDEX IF NOT EXISTS idx_fills_unmarked ON fills (recv_ts_ns)"
    " WHERE markout_60s IS NULL"
)
ORDERS_IDX_SYMBOL_TS = (
    "CREATE INDEX IF NOT EXISTS idx_orders_symbol_ts ON orders (symbol, submit_ts_ns)"
)
ORDER_EVENTS_IDX_TS = (
    "CREATE INDEX IF NOT EXISTS idx_order_events_ts ON order_events (ts_ns)"
)
ORDER_EVENTS_IDX_TYPE = (
    "CREATE INDEX IF NOT EXISTS idx_order_events_type ON order_events (event_type, ts_ns)"
)

ALL_DDL: list[str] = [
    SESSIONS,
    ORDERS,
    ORDERS_IDX_SYMBOL_TS,
    ORDER_EVENTS,
    ORDER_EVENTS_IDX_TS,
    ORDER_EVENTS_IDX_TYPE,
    FILLS,
    FILLS_IDX_SYMBOL_TS,
    FILLS_IDX_UNMARKED,
]
