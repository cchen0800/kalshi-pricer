"""Tests for the bot-portfolio snapshot.

Snapshot's job:
- read bot orders from `intended_orders` (mode='live', status='submitted')
- read Kalshi `/portfolio/settlements` to know what settled and at what P&L
- compute open notional (excluding settled), per-market contracts, today's realized P&L
- fail-closed (return None) if Kalshi calls fail
- return empty snapshot if trader is None (dry-run path)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.db import connect
from src.positions import PositionSnapshot, snapshot


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clear_history_cache():
    # trade_history caches Kalshi calls for 20s; tests must not bleed.
    from src.trade_history import _cache, _cache_lock
    with _cache_lock:
        _cache.clear()
    yield
    with _cache_lock:
        _cache.clear()


class FakeTrader:
    """Minimal stand-in for KalshiTrader used by snapshot via _fetch_settlements."""

    def __init__(self, settlements: list[dict] | None = None, fail: bool = False):
        self._settlements = settlements or []
        self._fail = fail

    def _request(self, method: str, path: str, params=None):
        if self._fail:
            raise RuntimeError("simulated Kalshi outage")
        if path == "/portfolio/settlements":
            return {"settlements": self._settlements}
        return {}


def insert_intent(
    db: sqlite3.Connection,
    *,
    market: str,
    side: str = "yes",
    action: str = "buy",
    count: int = 5,
    limit_cents: int = 25,
    mode: str = "live",
    status: str = "submitted",
    coid: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO intended_orders (
            ts_ms, mode, event_ticker, market_ticker, side, action,
            limit_price_cents, count, notional_usd, model_prob, edge_cents,
            minutes_left, spot, client_order_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(time.time() * 1000), mode, market.split("-T")[0], market, side, action,
            limit_cents, count, (limit_cents / 100.0) * count, 0.5, 20.0, 30.0, 80_000.0,
            coid or f"test-{market}-{action}-{count}-{int(time.time()*1_000_000)}", status,
        ),
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def yesterday_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")


def test_trader_none_returns_empty_snapshot(db):
    snap = snapshot(db, None)
    assert snap is not None
    assert snap.open_notional_usd == 0.0
    assert snap.realized_pnl_today_usd == 0.0
    assert snap.open_contracts_by_market == {}


def test_kalshi_failure_returns_none(db):
    snap = snapshot(db, FakeTrader(fail=True))
    assert snap is None


def test_open_position_counts_against_notional(db):
    insert_intent(db, market="KXBTCD-X-T1", count=5, limit_cents=25)
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == pytest.approx(1.25)
    assert snap.open_contracts_by_market == {("KXBTCD-X-T1", "yes"): 5}


def test_settled_market_excluded_from_open_notional(db):
    insert_intent(db, market="KXBTCD-X-T1", count=5, limit_cents=25)
    insert_intent(db, market="KXBTCD-Y-T2", count=4, limit_cents=30)
    settlements = [
        {
            "ticker": "KXBTCD-X-T1",
            "event_ticker": "KXBTCD-X",
            "settled_time": now_iso(),
            "market_result": "no",
            "revenue": 0,
            "yes_total_cost_dollars": "1.25",
            "no_total_cost_dollars": "0.00",
            "fee_cost": "0.05",
        },
    ]
    snap = snapshot(db, FakeTrader(settlements=settlements))
    assert snap is not None
    # Only the unsettled position contributes
    assert snap.open_notional_usd == pytest.approx(1.20)
    assert snap.open_contracts_by_market == {("KXBTCD-Y-T2", "yes"): 4}
    # Settled today, lost: revenue 0 - cost 1.25 - fee 0.05 = -1.30
    assert snap.realized_pnl_today_usd == pytest.approx(-1.30)
    assert snap.total_loss_today_usd() == pytest.approx(1.30)


def test_yesterday_settlements_dont_count_toward_today(db):
    insert_intent(db, market="KXBTCD-OLD-T1", count=10, limit_cents=50)
    settlements = [
        {
            "ticker": "KXBTCD-OLD-T1",
            "settled_time": yesterday_iso(),
            "market_result": "no",
            "revenue": 0,
            "yes_total_cost_dollars": "5.00",
            "no_total_cost_dollars": "0.00",
            "fee_cost": "0.20",
        },
    ]
    snap = snapshot(db, FakeTrader(settlements=settlements))
    assert snap is not None
    # Settled yesterday — no contribution to today's realized P&L
    assert snap.realized_pnl_today_usd == 0.0
    # Still excluded from open notional (the market is gone)
    assert snap.open_notional_usd == 0.0


def test_settlement_pnl_winner(db):
    insert_intent(db, market="KXBTCD-W-T1", count=10, limit_cents=21)
    settlements = [
        {
            "ticker": "KXBTCD-W-T1",
            "settled_time": now_iso(),
            "market_result": "yes",
            "revenue": 1000,  # 10 contracts × $1.00 payout = $10.00 = 1000 cents
            "yes_total_cost_dollars": "2.10",
            "no_total_cost_dollars": "0.00",
            "fee_cost": "0.10",
        },
    ]
    snap = snapshot(db, FakeTrader(settlements=settlements))
    assert snap is not None
    # 10.00 revenue - 2.10 cost - 0.10 fee = 7.80
    assert snap.realized_pnl_today_usd == pytest.approx(7.80)
    assert snap.total_loss_today_usd() == 0.0


def test_dry_run_intents_ignored(db):
    insert_intent(db, market="KXBTCD-D-T1", count=5, limit_cents=25, mode="dry_run")
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == 0.0
    assert snap.open_contracts_by_market == {}


def test_unsubmitted_intents_ignored(db):
    insert_intent(db, market="KXBTCD-E-T1", count=5, limit_cents=25, status="error")
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == 0.0


def test_user_manual_market_invisible_to_bot(db):
    """The user has manual positions on KXSNL — never saw an intended_orders
    row. Snapshot must not include them in the bot's risk view."""
    settlements = [
        {
            "ticker": "KXSNLMENTION-26MAY03-AI",
            "settled_time": now_iso(),
            "market_result": "no",
            "revenue": 0,
            "yes_total_cost_dollars": "9.55",
            "no_total_cost_dollars": "0.00",
            "fee_cost": "0.45",
        },
    ]
    snap = snapshot(db, FakeTrader(settlements=settlements))
    assert snap is not None
    # User's manual SNL loss does NOT consume bot budget or trip kill switch.
    assert snap.realized_pnl_today_usd == 0.0
    assert snap.open_notional_usd == 0.0


def test_buys_and_sells_net_in_open_count(db):
    market = "KXBTCD-N-T1"
    insert_intent(db, market=market, action="buy", count=10, limit_cents=20, coid="c1")
    insert_intent(db, market=market, action="sell", count=4, limit_cents=22, coid="c2")
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_contracts_by_market == {(market, "yes"): 6}
