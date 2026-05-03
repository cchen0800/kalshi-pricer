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
    yes_bid         REAL,
    yes_ask         REAL,
    yes_bid_size    REAL,
    yes_ask_size    REAL,
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
    edge_cents          REAL    NOT NULL,
    minutes_left        REAL    NOT NULL,
    spot                REAL    NOT NULL,
    client_order_id     TEXT    NOT NULL UNIQUE,
    status              TEXT    NOT NULL,            -- 'dry_run' | 'submitted' | 'rejected' | 'error'
    reject_reason       TEXT,
    kalshi_order_id     TEXT,
    raw_response        TEXT
);
CREATE INDEX IF NOT EXISTS idx_intended_ts     ON intended_orders(ts_ms);
CREATE INDEX IF NOT EXISTS idx_intended_market ON intended_orders(market_ticker, ts_ms);

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
    proxy_source: str = "coinbase"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


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
            model_prob, yes_bid, yes_ask, yes_bid_size, yes_ask_size, volume,
            edge_cents, proxy_source
        ) VALUES (
            :ts_ms, :event_ticker, :market_ticker, :strike, :spot, :sigma, :minutes_left,
            :model_prob, :yes_bid, :yes_ask, :yes_bid_size, :yes_ask_size, :volume,
            :edge_cents, :proxy_source
        )
    """
    conn.executemany(sql, [asdict(r) for r in rows])
    return len(rows)
