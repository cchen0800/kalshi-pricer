"""SQLite persistence for poll history. One row per (poll, strike)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    event_ticker    TEXT    NOT NULL,
    market_ticker   TEXT    NOT NULL,
    strike          REAL    NOT NULL,
    spot            REAL    NOT NULL,
    sigma           REAL    NOT NULL,
    minutes_left    REAL    NOT NULL,
    model_prob      REAL    NOT NULL,
    model_prob_calibrated REAL,                       -- isotonic-calibrated model_prob; NULL on legacy rows
    yes_bid         REAL,
    yes_ask         REAL,
    yes_bid_size    REAL,
    yes_ask_size    REAL,
    no_bid          REAL,                              -- NO-side bid in dollars; needed for BUY_NO entries (PR #4)
    no_ask          REAL,                              -- NO-side ask in dollars; what BUY_NO would pay
    volume          REAL,
    edge_cents      REAL    NOT NULL,
    proxy_source    TEXT    NOT NULL DEFAULT 'coinbase'
);
CREATE INDEX IF NOT EXISTS idx_polls_market_ts ON polls(market_ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_polls_event_ts  ON polls(event_ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_polls_ts        ON polls(ts_ms);

CREATE TABLE IF NOT EXISTS intended_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms               INTEGER NOT NULL,
    mode                TEXT    NOT NULL,            -- 'dry_run' | 'live'
    event_ticker        TEXT    NOT NULL,
    market_ticker       TEXT    NOT NULL,
    side                TEXT    NOT NULL,            -- 'yes' | 'no'
    action              TEXT    NOT NULL,            -- 'buy' | 'sell'
    limit_price_cents   INTEGER NOT NULL,            -- 1..99
    count               INTEGER NOT NULL,
    notional_usd        REAL    NOT NULL,
    model_prob          REAL    NOT NULL,
    model_prob_calibrated REAL,                      -- calibrated probability used for edge; NULL on legacy rows
    edge_cents          REAL    NOT NULL,
    minutes_left        REAL    NOT NULL,
    spot                REAL    NOT NULL,
    client_order_id     TEXT    NOT NULL UNIQUE,
    status              TEXT    NOT NULL,            -- 'dry_run' | 'submitted' | 'rejected' | 'error'
    reject_reason       TEXT,
    kalshi_order_id     TEXT,
    raw_response        TEXT,
    bot_id              TEXT                         -- e.g. 'sol-selective', 'sol-aggressive'
);
CREATE INDEX IF NOT EXISTS idx_intended_ts     ON intended_orders(ts_ms);
CREATE INDEX IF NOT EXISTS idx_intended_market ON intended_orders(market_ticker, ts_ms);
-- idx_intended_bot is created in _migrate() so it works on pre-existing DBs
-- where the bot_id column has to be added via ALTER TABLE first.

CREATE TABLE IF NOT EXISTS fills (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms               INTEGER NOT NULL,
    intended_order_id   INTEGER,                     -- FK into intended_orders
    market_ticker       TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    action              TEXT    NOT NULL,
    fill_price_cents    INTEGER NOT NULL,
    count               INTEGER NOT NULL,
    fee_usd             REAL    NOT NULL DEFAULT 0,
    cash_delta_usd      REAL    NOT NULL,            -- signed: negative = paid, positive = received
    kalshi_trade_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_ts     ON fills(ts_ms);
CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_ticker, ts_ms);

-- Note: a `settlements` table already exists in pricer.db for historical BTC
-- OHLC settlement prices (different schema, different purpose). We use a
-- distinct name here to avoid collision.
CREATE TABLE IF NOT EXISTS portfolio_settlements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms               INTEGER NOT NULL,
    market_ticker       TEXT    NOT NULL UNIQUE,
    settled_yes         INTEGER NOT NULL,            -- 1 if YES paid out, 0 if NO paid out
    cash_delta_usd      REAL    NOT NULL,            -- payout to our account from settlement
    raw_response        TEXT
);

-- Audit log of engine decisions: one row per (poll, market) where the model
-- saw a positive raw edge on either side, regardless of whether gates blocked
-- the trade. This is what enables retroactive "what if we'd loosened σ to
-- 0.40?" analysis without re-running the pricer over poll history.
CREATE TABLE IF NOT EXISTS shadow_signals (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms                    INTEGER NOT NULL,
    event_ticker             TEXT    NOT NULL,
    market_ticker            TEXT    NOT NULL,
    strike                   REAL    NOT NULL,
    spot                     REAL    NOT NULL,
    sigma                    REAL    NOT NULL,
    minutes_left             REAL    NOT NULL,
    model_prob               REAL    NOT NULL,
    yes_bid                  REAL,
    yes_ask                  REAL,
    buy_edge_net_cents       REAL,                   -- post-fee BUY_YES edge; NULL if no ask
    sell_edge_net_cents      REAL,                   -- post-fee SELL_YES edge; NULL if no bid
    gate_sigma_passed        INTEGER NOT NULL,       -- 0/1
    gate_dist_passed         INTEGER NOT NULL,
    gate_time_passed         INTEGER NOT NULL,
    gate_mp_band_passed      INTEGER,                -- 0/1; calibrated mp ∈ [0.05, 0.85). NULL on legacy rows.
    chosen_side              TEXT    NOT NULL,       -- 'BUY_YES' | 'BUY_NO' | 'SELL_YES' | 'NONE'
    chosen_edge_cents        REAL    NOT NULL        -- what actionable_edge() returned
);
CREATE INDEX IF NOT EXISTS idx_shadow_ts        ON shadow_signals(ts_ms);
CREATE INDEX IF NOT EXISTS idx_shadow_market_ts ON shadow_signals(market_ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_shadow_event_ts  ON shadow_signals(event_ticker, ts_ms);

-- Ground-truth resolution for every settled market (not just ones we held).
-- expiration_value is the actual final BRTI print Kalshi resolved against,
-- which is the cleanest spot ground truth for backtesting the pricer.
CREATE TABLE IF NOT EXISTS market_settlements (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_ts_ms           INTEGER NOT NULL,       -- when we scraped this row
    settlement_ts_ms         INTEGER,                -- Kalshi settlement_ts
    event_ticker             TEXT    NOT NULL,
    market_ticker            TEXT    NOT NULL UNIQUE,
    strike                   REAL    NOT NULL,
    result                   TEXT    NOT NULL,       -- 'yes' | 'no' | 'void' | other
    settled_yes              INTEGER NOT NULL,       -- 1 if result=='yes' else 0
    expiration_value         REAL,                   -- final BRTI dollars
    volume                   REAL,
    open_interest            REAL,
    raw_response             TEXT
);
CREATE INDEX IF NOT EXISTS idx_msettle_event ON market_settlements(event_ticker);
CREATE INDEX IF NOT EXISTS idx_msettle_ts    ON market_settlements(recorded_ts_ms);
"""


@dataclass
class PollRow:
    ts_ms: int
    event_ticker: str
    market_ticker: str
    strike: float
    spot: float
    sigma: float
    minutes_left: float
    model_prob: float
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float | None
    yes_ask_size: float | None
    volume: float | None
    edge_cents: float
    model_prob_calibrated: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    proxy_source: str = "coinbase"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent in-place migrations for older DBs.

    `CREATE TABLE IF NOT EXISTS` won't add columns to a pre-existing table, so
    schema changes for the live DB live here.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(intended_orders)")}
    if "bot_id" not in cols:
        conn.execute("ALTER TABLE intended_orders ADD COLUMN bot_id TEXT")
    if "model_prob_calibrated" not in cols:
        conn.execute("ALTER TABLE intended_orders ADD COLUMN model_prob_calibrated REAL")
    # Always ensure the index exists (covers both fresh and migrated DBs;
    # has to run after the ALTER above on old DBs).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_intended_bot "
        "ON intended_orders(bot_id, ts_ms)"
    )

    poll_cols = {r[1] for r in conn.execute("PRAGMA table_info(polls)")}
    if "model_prob_calibrated" not in poll_cols:
        # Pre-existing rows get NULL — fine, the value is only consumed by
        # PR #3 onward and the engine reads/writes the new column from now.
        conn.execute("ALTER TABLE polls ADD COLUMN model_prob_calibrated REAL")
    if "no_bid" not in poll_cols:
        conn.execute("ALTER TABLE polls ADD COLUMN no_bid REAL")
    if "no_ask" not in poll_cols:
        conn.execute("ALTER TABLE polls ADD COLUMN no_ask REAL")

    shadow_cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_signals)")}
    if "gate_mp_band_passed" not in shadow_cols:
        conn.execute("ALTER TABLE shadow_signals ADD COLUMN gate_mp_band_passed INTEGER")


@contextmanager
def open_db(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def insert_polls(conn: sqlite3.Connection, rows: Iterable[PollRow]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """
        INSERT INTO polls (
            ts_ms, event_ticker, market_ticker, strike, spot, sigma, minutes_left,
            model_prob, model_prob_calibrated, yes_bid, yes_ask, yes_bid_size,
            yes_ask_size, no_bid, no_ask, volume, edge_cents, proxy_source
        ) VALUES (
            :ts_ms, :event_ticker, :market_ticker, :strike, :spot, :sigma, :minutes_left,
            :model_prob, :model_prob_calibrated, :yes_bid, :yes_ask, :yes_bid_size,
            :yes_ask_size, :no_bid, :no_ask, :volume, :edge_cents, :proxy_source
        )
    """
    conn.executemany(sql, [asdict(r) for r in rows])
    return len(rows)


@dataclass
class ShadowSignal:
    ts_ms: int
    event_ticker: str
    market_ticker: str
    strike: float
    spot: float
    sigma: float
    minutes_left: float
    model_prob: float
    yes_bid: float | None
    yes_ask: float | None
    buy_edge_net_cents: float | None
    sell_edge_net_cents: float | None
    gate_sigma_passed: int
    gate_dist_passed: int
    gate_time_passed: int
    chosen_side: str
    chosen_edge_cents: float
    gate_mp_band_passed: int | None = None


def insert_shadow_signals(conn: sqlite3.Connection, rows: Iterable[ShadowSignal]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """
        INSERT INTO shadow_signals (
            ts_ms, event_ticker, market_ticker, strike, spot, sigma, minutes_left,
            model_prob, yes_bid, yes_ask, buy_edge_net_cents, sell_edge_net_cents,
            gate_sigma_passed, gate_dist_passed, gate_time_passed, gate_mp_band_passed,
            chosen_side, chosen_edge_cents
        ) VALUES (
            :ts_ms, :event_ticker, :market_ticker, :strike, :spot, :sigma, :minutes_left,
            :model_prob, :yes_bid, :yes_ask, :buy_edge_net_cents, :sell_edge_net_cents,
            :gate_sigma_passed, :gate_dist_passed, :gate_time_passed, :gate_mp_band_passed,
            :chosen_side, :chosen_edge_cents
        )
    """
    conn.executemany(sql, [asdict(r) for r in rows])
    return len(rows)
