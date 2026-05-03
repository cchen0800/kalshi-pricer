"""Tests for the executor's hardcoded guards and dry-run path.

Each guard is exercised independently. The executor never POSTs in these
tests — `live=False` short-circuits to recording the intent.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.db import PollRow, connect
from src.executor import (
    KILL_FILE,
    MAX_CONTRACTS_PER_ORDER,
    MAX_DAILY_LOSS_USD,
    MAX_NOTIONAL_USD,
    MAX_ORDERS_PER_MINUTE,
    MIN_EDGE_CENTS,
    MIN_MINUTES_TO_CLOSE,
    Executor,
)


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


def make_row(
    *,
    market: str = "KXBTCD-26MAY0113-T80000",
    event: str = "KXBTCD-26MAY0113",
    model_prob: float = 0.50,
    yes_bid: float | None = 0.20,
    yes_ask: float | None = 0.25,
    minutes_left: float = 30.0,
    spot: float = 80_000.0,
    strike: float = 80_000.0,
) -> PollRow:
    edge = model_prob * 100 - (yes_bid + yes_ask) * 50 if yes_bid and yes_ask else 0.0
    return PollRow(
        ts_ms=int(time.time() * 1000),
        event_ticker=event,
        market_ticker=market,
        strike=strike,
        spot=spot,
        sigma=0.50,
        minutes_left=minutes_left,
        model_prob=model_prob,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=100,
        yes_ask_size=100,
        volume=1000,
        edge_cents=edge,
    )


def test_dry_run_records_intent_no_post(db):
    ex = Executor(db, trader=None, live=False)
    # Big edge: model 80c, ask 25c → buy edge 55c (well above 15c).
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket is not None
    assert d.ticket.action == "buy"

    [(mode, status, price, count)] = list(
        db.execute("SELECT mode, status, limit_price_cents, count FROM intended_orders")
    )
    assert mode == "dry_run"
    assert status == "dry_run"
    assert price == 25
    assert 1 <= count <= MAX_CONTRACTS_PER_ORDER


def test_min_edge_floor_blocks_small_edges(db):
    ex = Executor(db, trader=None, live=False)
    # 5c edge — below MIN_EDGE_CENTS (15c).
    row = make_row(model_prob=0.30, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "MIN_EDGE_CENTS" in d.reason


def test_time_to_close_guard_blocks_near_settle(db):
    ex = Executor(db, trader=None, live=False)
    row = make_row(
        model_prob=0.80, yes_ask=0.25, yes_bid=0.20,
        minutes_left=MIN_MINUTES_TO_CLOSE - 1.0,
    )
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "too close to settle" in d.reason


def test_kill_file_blocks_orders(db, tmp_path, monkeypatch):
    kill = tmp_path / ".kill"
    monkeypatch.setattr("src.executor.KILL_FILE", kill)
    kill.touch()
    ex = Executor(db, trader=None, live=False)
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "kill file" in d.reason


def test_notional_cap_caps_count(db):
    ex = Executor(db, trader=None, live=False)
    # Pre-load fills equivalent to $25 of open notional (5 contracts @ 500c).
    # Wait, prices are 1..99 cents. $25 = 2500 cents = e.g. 50 contracts at 50c.
    # Let's open 50 contracts @ 50c on a different market.
    db.execute(
        "INSERT INTO fills (ts_ms, market_ticker, side, action, fill_price_cents, count, "
        "cash_delta_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(time.time() * 1000), "KXBTCD-OTHER-T1", "yes", "buy", 50, 50, -25.0),
    )
    # Now $25 of $30 budget is used. Try a 50c order — should cap count to floor(5/0.50) = 10.
    # But MAX_CONTRACTS_PER_ORDER = 5, MAX_CONTRACTS_PER_STRIKE = 10, so cap is 5.
    row = make_row(model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
    d = ex.handle_poll([row])
    assert d.placed is True
    # Remaining budget = $5. At 50c each, that's 10 contracts. Per-order cap is 5.
    assert d.ticket.count == 5

    # Now budget is fully used. Next call should block on notional.
    db.execute(
        "INSERT INTO fills (ts_ms, market_ticker, side, action, fill_price_cents, count, "
        "cash_delta_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(time.time() * 1000), "KXBTCD-OTHER-T1", "yes", "buy", 50, 10, -5.0),
    )
    row2 = make_row(model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
    d2 = ex.handle_poll([row2])
    assert d2.placed is False


def test_rate_limit_blocks_after_n_orders(db):
    ex = Executor(db, trader=None, live=False)
    # Place MAX_ORDERS_PER_MINUTE = 4 orders (each on different markets).
    for i in range(MAX_ORDERS_PER_MINUTE):
        row = make_row(market=f"KXBTCD-RATE-T{i}", model_prob=0.80, yes_ask=0.10, yes_bid=0.05)
        d = ex.handle_poll([row])
        assert d.placed is True, f"order #{i} should have placed: {d.reason}"
    # 5th should hit the rate limit, regardless of edge.
    row = make_row(market="KXBTCD-RATE-T99", model_prob=0.80, yes_ask=0.10, yes_bid=0.05)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "rate limit" in d.reason


def test_sell_yes_requires_existing_long(db):
    ex = Executor(db, trader=None, live=False)
    # Big sell-yes edge: model 10c, bid 50c → sell edge 40c.
    row = make_row(model_prob=0.10, yes_ask=0.55, yes_bid=0.50)
    d = ex.handle_poll([row])
    # No prior fills, so we don't own any YES — sell is rejected.
    assert d.placed is False


def test_sell_yes_allowed_with_long(db):
    # First, dry-run a buy to take a position and record a fill, then try selling.
    ex = Executor(db, trader=None, live=False)
    # Manually insert a fill: we own 5 YES @ 25c on this market.
    market = "KXBTCD-SELLT"
    db.execute(
        "INSERT INTO fills (ts_ms, market_ticker, side, action, fill_price_cents, count, "
        "cash_delta_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(time.time() * 1000), market, "yes", "buy", 25, 5, -1.25),
    )
    # Now try to sell-yes on that market: model 10c, bid 50c → 40c edge to sell.
    row = make_row(market=market, model_prob=0.10, yes_ask=0.55, yes_bid=0.50)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.action == "sell"
    assert d.ticket.count <= 5  # can't sell more than we own


def test_daily_loss_limit_blocks(db):
    ex = Executor(db, trader=None, live=False)
    # Insert a fill today with a huge realized loss.
    db.execute(
        "INSERT INTO fills (ts_ms, market_ticker, side, action, fill_price_cents, count, "
        "cash_delta_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(time.time() * 1000), "KXBTCD-X", "yes", "buy", 50, 10, -float(MAX_DAILY_LOSS_USD)),
    )
    db.execute(
        "INSERT INTO portfolio_settlements (ts_ms, market_ticker, settled_yes, cash_delta_usd) "
        "VALUES (?, ?, ?, ?)",
        (int(time.time() * 1000), "KXBTCD-X", 0, 0.0),
    )
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "daily loss" in d.reason


def test_no_rows_above_threshold(db):
    ex = Executor(db, trader=None, live=False)
    # Tiny edge.
    row = make_row(model_prob=0.225, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
