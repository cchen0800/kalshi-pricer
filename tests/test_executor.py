"""Tests for the executor's hardcoded guards and dry-run path.

Each guard is exercised independently. The executor never POSTs in these
tests — `live=False` short-circuits to recording the intent.

Risk-cap tests monkeypatch `src.executor.snapshot` so the test specifies
the exact PositionSnapshot to return. Snapshot's own logic is covered in
test_positions.py.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.db import PollRow, connect
from src.executor import (
    BOT_PROFILES,
    MAX_CONTRACTS_PER_ORDER,
    MAX_ORDERS_PER_MINUTE,
    MIN_MINUTES_TO_CLOSE,
    BotProfile,
    Executor,
)

# Profile used by tests with a kill-file isolated to tmp_path. Mirrors the
# selective profile's risk knobs (so existing assertions still hold) but lets
# `test_kill_file_blocks_orders` swap in a tmpdir path without monkeypatching
# the BOT_PROFILES dict.
def _profile_with_kill(kill_path) -> BotProfile:
    base = BOT_PROFILES["selective"]
    return BotProfile(
        bot_id=base.bot_id,
        coid_prefix=base.coid_prefix,
        max_notional_usd=base.max_notional_usd,
        max_daily_loss_usd=base.max_daily_loss_usd,
        min_edge_cents=base.min_edge_cents,
        kill_file=kill_path,
    )

MAX_NOTIONAL_USD = BOT_PROFILES["selective"].max_notional_usd
MAX_DAILY_LOSS_USD = BOT_PROFILES["selective"].max_daily_loss_usd
from src.positions import PositionSnapshot


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
    strike: float = 79_500.0,  # ~0.6% from spot — passes engine.actionable_edge gates
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


def stub_snapshot(monkeypatch, snap: PositionSnapshot | None) -> None:
    """Force executor.snapshot to return `snap`. Use to isolate cap logic
    from the snapshot's own data-source plumbing."""
    monkeypatch.setattr("src.executor.snapshot", lambda conn, trader, **_: snap)


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
    # Gross 5c, net ~3c after 2c fee — below the selective profile's 8c floor.
    row = make_row(model_prob=0.30, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "min_edge_cents" in d.reason


def test_time_to_close_guard_blocks_near_settle(db):
    ex = Executor(db, trader=None, live=False)
    row = make_row(
        model_prob=0.80, yes_ask=0.25, yes_bid=0.20,
        minutes_left=MIN_MINUTES_TO_CLOSE - 1.0,
    )
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "too close to settle" in d.reason


def test_kill_file_blocks_orders(db, tmp_path):
    kill = tmp_path / ".kill"
    kill.touch()
    ex = Executor(db, trader=None, live=False, profile=_profile_with_kill(kill))
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "kill file" in d.reason


def test_notional_cap_caps_count(db, monkeypatch):
    # $25 of $30 budget already open. Per-order cap is 5; per-strike room is 10.
    # At 50c each, $5 budget → 10 contracts → capped to 5 by per-order.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=25.0,
        realized_pnl_today_usd=0.0,
    ))
    ex = Executor(db, trader=None, live=False)
    row = make_row(model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.count == 5

    # $30 of $30 used → should block on notional.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD,
        realized_pnl_today_usd=0.0,
    ))
    row2 = make_row(market="KXBTCD-OTHER2", model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
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
    # No positions in the empty default snapshot.
    # Big sell-yes edge: model 10c, bid 50c → sell edge 40c.
    row = make_row(model_prob=0.10, yes_ask=0.55, yes_bid=0.50)
    d = ex.handle_poll([row])
    assert d.placed is False


def test_sell_yes_allowed_with_long(db, monkeypatch):
    market = "KXBTCD-SELLT"
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=1.25,
        realized_pnl_today_usd=0.0,
        open_contracts_by_market={(market, "yes"): 5},
    ))
    ex = Executor(db, trader=None, live=False)
    row = make_row(market=market, model_prob=0.10, yes_ask=0.55, yes_bid=0.50)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.action == "sell"
    assert d.ticket.count <= 5


def test_daily_loss_limit_blocks(db, monkeypatch):
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=0.0,
        realized_pnl_today_usd=-float(MAX_DAILY_LOSS_USD),
    ))
    ex = Executor(db, trader=None, live=False)
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


def test_order_notification_escapes_markdown_specials(db, monkeypatch):
    """Regression: previously the post-fill status 'submitted (order_id=...)'
    contained an underscore that opened an unclosed Markdown italic, and the
    Telegram API rejected every order alert with a 400. The notifier swallows
    400s by design (must not block trading), so trades went unnotified.

    Verify the message body contains no bare underscores or asterisks outside
    the literal '*[LIVE] BUY YES*' bold markers."""
    captured: list[str] = []

    class FakeNotifier:
        enabled = True
        def send(self, text: str) -> None:
            captured.append(text)

    class FakeTrader:
        def place_order(self, **kw):
            return {"order_id": "fecc03f0-4efb-4787-a639_with_underscores"}

    ex = Executor(db, trader=FakeTrader(), live=True, notifier=FakeNotifier())
    stub_snapshot(monkeypatch, PositionSnapshot.empty() if hasattr(PositionSnapshot, "empty")
                  else PositionSnapshot(0.0, 0.0))
    row = make_row(market="KXBTCD-X-T1", model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert len(captured) == 1
    msg = captured[0]
    # The only allowed unescaped * are the two surrounding *[LIVE] BUY YES*.
    # Strip those, then any remaining * is a bug.
    body_after_bold = msg.split("*", 2)[-1]  # everything after the closing bold *
    assert "*" not in body_after_bold
    # Underscores must always be escaped. The order_id contained _with_ which
    # would have crashed the parser; check it's escaped now.
    assert "with_underscores" not in msg
    assert "with\\_underscores" in msg


def test_fail_closed_when_snapshot_unavailable(db, monkeypatch):
    """If snapshot() returns None (Kalshi unreachable in live mode), the
    executor must refuse to trade. This is the P0 fix — uncapped positions
    were previously possible because snapshot silently read empty tables."""
    stub_snapshot(monkeypatch, None)
    ex = Executor(db, trader=object(), live=False)  # trader presence triggers the live path
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "fail-closed" in d.reason
