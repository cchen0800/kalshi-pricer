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
