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
    FULL_CONVICTION_EDGE_CENTS,
    MAX_CONTRACTS_PER_ORDER,
    MAX_OPEN_STRIKES_PER_EVENT_SIDE,
    MAX_CONTRACTS_PER_STRIKE,
    MAX_ORDERS_PER_MINUTE,
    MIN_MINUTES_TO_CLOSE,
    BotProfile,
    Executor,
)

# Synthetic portfolio balance for tests; effective caps derive from pct × this.
TEST_PORTFOLIO_USD = 1000.0

# Test-only profile: same risk knobs as selective but with the legacy side
# policy (BUY_YES allowed, no BUY_NO). The production selective profile now
# runs the BUY_NO entry policy (PR #3), but these executor-mechanic tests
# need a permissive entry side to exercise rate-limit / cap / kill / etc.
# logic. New tests covering policy itself live in test_engine.py.
def _legacy_profile(*, kill_path: Path | None = None) -> BotProfile:
    base = BOT_PROFILES["selective"]
    return BotProfile(
        bot_id=base.bot_id,
        coid_prefix=base.coid_prefix,
        max_notional_pct=base.max_notional_pct,
        max_daily_loss_pct=base.max_daily_loss_pct,
        min_edge_cents=base.min_edge_cents,
        kill_file=kill_path or base.kill_file,
        # policy left at default (LEGACY_POLICY)
    )

from src.executor import _HARD_NOTIONAL_CEILING_USD, _HARD_DAILY_LOSS_CEILING_USD
MAX_NOTIONAL_USD = min(BOT_PROFILES["selective"].max_notional_pct * TEST_PORTFOLIO_USD, _HARD_NOTIONAL_CEILING_USD)
MAX_DAILY_LOSS_USD = min(BOT_PROFILES["selective"].max_daily_loss_pct * TEST_PORTFOLIO_USD, _HARD_DAILY_LOSS_CEILING_USD)
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
    no_bid: float | None = None,
    no_ask: float | None = None,
    model_prob_calibrated: float | None = None,
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
        no_bid=no_bid,
        no_ask=no_ask,
        volume=1000,
        edge_cents=edge,
        model_prob_calibrated=model_prob_calibrated,
    )


def stub_snapshot(monkeypatch, snap: PositionSnapshot | None) -> None:
    """Force executor.snapshot to return `snap`. Use to isolate cap logic
    from the snapshot's own data-source plumbing."""
    monkeypatch.setattr("src.executor.snapshot", lambda conn, trader, **_: snap)


def test_dry_run_records_intent_no_post(db):
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
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
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    # model 27.5c, ask 25c → net 2.5c - 2c fee = 0.5c, positive but below the
    # selective profile's 3c floor.
    row = make_row(model_prob=0.275, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "min_edge_cents" in d.reason


def test_time_to_close_guard_blocks_near_settle(db):
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
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
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile(kill_path=kill))
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "kill file" in d.reason


def test_notional_cap_caps_count(db, monkeypatch):
    # Most of the budget already open; at 50c each, only 5 contracts fit.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD - 2.50,
        realized_pnl_today_usd=0.0,
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    row = make_row(model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.count == 5

    # Full budget used → should block on notional.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD,
        realized_pnl_today_usd=0.0,
    ))
    row2 = make_row(market="KXBTCD-OTHER2", model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
    d2 = ex.handle_poll([row2])
    assert d2.placed is False


def test_rate_limit_blocks_after_n_orders(db):
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
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
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
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
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    row = make_row(market=market, model_prob=0.10, yes_ask=0.55, yes_bid=0.50)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.action == "sell"
    assert d.ticket.count <= 5


def test_sell_no_requires_existing_no_inventory(db):
    """SELL_NO surfaces from actionable_edge when no_bid > (1 - model_prob),
    but the executor must refuse to sell what we don't hold."""
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    # No NO held in default snapshot. Big SELL_NO edge:
    # model 0.30 → NO worth 70¢; no_bid=0.85 → gross 15¢ overpay.
    row = make_row(model_prob=0.30, yes_bid=0.05, yes_ask=0.40,
                   no_bid=0.85, no_ask=0.95)
    d = ex.handle_poll([row])
    assert d.placed is False


def test_sell_no_allowed_with_no_position(db, monkeypatch):
    """When we DO hold NO contracts, SELL_NO closes them — and the count is
    capped at the held quantity (can't sell more than we own)."""
    market = "KXBTCD-SELLNOT"
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=4.00,
        realized_pnl_today_usd=0.0,
        open_contracts_by_market={(market, "no"): 7},
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    row = make_row(market=market, model_prob=0.30, yes_bid=0.05, yes_ask=0.40,
                   no_bid=0.85, no_ask=0.95)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.action == "sell"
    assert d.ticket.side == "no"
    assert d.ticket.count <= 7


def test_daily_loss_limit_blocks(db, monkeypatch):
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=0.0,
        realized_pnl_today_usd=-float(MAX_DAILY_LOSS_USD),
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "daily loss" in d.reason


def test_no_rows_above_threshold(db):
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
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

    ex = Executor(db, trader=FakeTrader(), live=True, notifier=FakeNotifier(),
                  profile=_legacy_profile(), portfolio_usd=TEST_PORTFOLIO_USD)
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
    # The order_id is uuid-shaped (contains underscores in some Kalshi builds)
    # and used to break Markdown parsing. The current notifier drops it from
    # the user-facing message entirely — verify it's nowhere in the body.
    assert "fecc03f0" not in msg
    assert "with_underscores" not in msg
    assert "order_id" not in msg


def test_fail_closed_when_snapshot_unavailable(db, monkeypatch):
    """If snapshot() returns None (Kalshi unreachable in live mode), the
    executor must refuse to trade. This is the P0 fix — uncapped positions
    were previously possible because snapshot silently read empty tables."""
    stub_snapshot(monkeypatch, None)
    ex = Executor(db, trader=object(), live=False, profile=_legacy_profile())  # trader presence triggers the live path
    row = make_row(model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d = ex.handle_poll([row])
    assert d.placed is False
    assert "fail-closed" in d.reason


# ---- PR #4: BUY_NO branch ----

def _selective_profile() -> BotProfile:
    """Production-like selective profile (BUY_NO entry policy) for PR #4 tests."""
    return BOT_PROFILES["selective"]


def test_buy_no_dry_run_records_intent_with_side_no(db):
    ex = Executor(db, trader=None, live=False, profile=_selective_profile())
    # Model says YES at 10c → NO worth ~90c. no_ask=50c → BUY_NO net ≈ 38c.
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.50)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.side == "no"
    assert d.ticket.action == "buy"
    assert d.ticket.limit_price_cents == 50

    # Intent row in DB has side='no'
    rows = list(db.execute(
        "SELECT side, action, limit_price_cents, model_prob_calibrated FROM intended_orders"
    ))
    assert len(rows) == 1
    side_db, action_db, price_db, cal_db = rows[0]
    assert side_db == "no"
    assert action_db == "buy"
    assert price_db == 50
    assert cal_db is None


def test_buy_no_concentration_cap_keyed_by_no_side(db, monkeypatch):
    """Already holding 10 NO contracts in a market → no room for more BUY_NO
    in that market. A YES holding in the same market does NOT consume the
    NO-side slot (and vice versa)."""
    market = "KXBTCD-26MAY0113-T80000"
    from src.executor import MAX_CONTRACTS_PER_STRIKE
    # Saturated on NO side; YES side untouched.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=0.50,
        realized_pnl_today_usd=0.0,
        open_contracts_by_market={(market, "no"): MAX_CONTRACTS_PER_STRIKE},
    ))
    ex = Executor(db, trader=None, live=False, profile=_selective_profile())
    row = make_row(market=market, model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.50)
    d = ex.handle_poll([row])
    assert d.placed is False  # NO side full

    # Now move the saturation to YES side — BUY_NO should be unaffected.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=2.0,
        realized_pnl_today_usd=0.0,
        open_contracts_by_market={(market, "yes"): MAX_CONTRACTS_PER_STRIKE},
    ))
    d2 = ex.handle_poll([row])
    assert d2.placed is True
    assert d2.ticket.side == "no"


def test_buy_no_notional_cap_uses_aggregate_open_notional(db, monkeypatch):
    """Notional cap binds across YES + NO sides. With most of the budget used,
    at no_ask=50c some contracts still fit, capped by remaining notional."""
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD - 4.00,
        realized_pnl_today_usd=0.0,
    ))
    ex = Executor(db, trader=None, live=False, profile=_selective_profile())
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.50)
    d = ex.handle_poll([row])
    assert d.placed is True
    # $4.00 remaining / $0.50 per contract = 8 max by notional.
    assert d.ticket.count == 8

    # Budget exhausted → only $0.01 left, no contract fits at 50c.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD - 0.01,
        realized_pnl_today_usd=0.0,
    ))
    row2 = make_row(market="KXBTCD-OTHER-T1", model_prob=0.10, yes_bid=0.20,
                    yes_ask=0.25, no_ask=0.50)
    d2 = ex.handle_poll([row2])
    assert d2.placed is False


def test_buy_no_live_posts_side_no(db, monkeypatch):
    """End-to-end: live mode passes side='no' to KalshiTrader.place_order."""
    captured: dict = {}

    class FakeTrader:
        def place_order(self, **kw):
            captured.update(kw)
            return {"order_id": "abc-123"}

    stub_snapshot(monkeypatch, PositionSnapshot(0.0, 0.0))
    ex = Executor(db, trader=FakeTrader(), live=True, profile=_selective_profile(),
                  portfolio_usd=TEST_PORTFOLIO_USD)
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.50)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert captured["side"] == "no"
    assert captured["action"] == "buy"
    assert captured["limit_price_cents"] == 50


def test_buy_no_telegram_label(db, monkeypatch):
    """BUY_NO must show 'BUY NO' in the Telegram alert, not 'BUY YES'."""
    captured: list[str] = []

    class FakeNotifier:
        enabled = True
        def send(self, text: str) -> None:
            captured.append(text)

    class FakeTrader:
        def place_order(self, **kw):
            return {"order_id": "x-1"}

    stub_snapshot(monkeypatch, PositionSnapshot(0.0, 0.0))
    ex = Executor(db, trader=FakeTrader(), live=True, notifier=FakeNotifier(),
                  profile=_selective_profile(), portfolio_usd=TEST_PORTFOLIO_USD)
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.50)
    ex.handle_poll([row])
    assert len(captured) == 1
    assert "BUY NO" in captured[0]
    assert "BUY YES" not in captured[0]


def test_intended_orders_migration_adds_calibrated_prob(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        """
        CREATE TABLE intended_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            mode TEXT NOT NULL,
            event_ticker TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            limit_price_cents INTEGER NOT NULL,
            count INTEGER NOT NULL,
            notional_usd REAL NOT NULL,
            model_prob REAL NOT NULL,
            edge_cents REAL NOT NULL,
            minutes_left REAL NOT NULL,
            spot REAL NOT NULL,
            client_order_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            reject_reason TEXT,
            kalshi_order_id TEXT,
            raw_response TEXT,
            bot_id TEXT
        )
        """
    )
    raw.close()
    conn = connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(intended_orders)")}
        assert "model_prob_calibrated" in cols
    finally:
        conn.close()


def test_intended_order_records_calibrated_prob(db):
    ex = Executor(db, trader=None, live=False, profile=_selective_profile())
    row = make_row(
        model_prob=0.10,
        model_prob_calibrated=0.12,
        yes_bid=0.20,
        yes_ask=0.25,
        no_ask=0.50,
    )
    d = ex.handle_poll([row])
    assert d.placed is True
    [(raw, cal)] = list(db.execute(
        "SELECT model_prob, model_prob_calibrated FROM intended_orders"
    ))
    assert raw == pytest.approx(0.10)
    assert cal == pytest.approx(0.12)


# ---- PR #6A: top-K orders per poll ----

def test_top_k_places_multiple_distinct_strikes(db, monkeypatch):
    """Distinct-strike cap stops a single event from fanning out too far."""
    stub_snapshot(monkeypatch, PositionSnapshot(0.0, 0.0))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    rows = [
        make_row(market="KXBTCD-26MAY0113-T80500", strike=80_500.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.25),
        make_row(market="KXBTCD-26MAY0113-T80600", strike=80_600.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.30),
        make_row(market="KXBTCD-26MAY0113-T80700", strike=80_700.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.35),
        make_row(market="KXBTCD-26MAY0113-T80800", strike=80_800.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.40),
    ]
    ex.handle_poll(rows)
    # Event-strike cap binds before K=3.
    n_intents = db.execute("SELECT COUNT(*) FROM intended_orders").fetchone()[0]
    assert n_intents == MAX_OPEN_STRIKES_PER_EVENT_SIDE


def test_event_strike_cap_allows_adding_to_existing_market(db, monkeypatch):
    market = "KXBTCD-26MAY0113-T80500"
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=2.50,
        realized_pnl_today_usd=0.0,
        open_contracts_by_market={
            (market, "yes"): 1,
            ("KXBTCD-26MAY0113-T80600", "yes"): 1,
        },
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    row = make_row(market=market, strike=80_500.0, model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    d = ex.handle_poll([row])
    assert d.placed is True


def test_top_k_running_notional_blocks_later_candidates(db, monkeypatch):
    """First placement consumes most of the notional cap; second candidate
    blocked by running notional, not the original snap."""
    # $6.50 remaining at 60c/contract. Per-strike cap (10) allows 10 contracts,
    # but notional cap binds at floor(6.50/0.60)=10. First ticket takes $6.00,
    # leaving $0.50 — not enough for another at 60c.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD - 6.50,
        realized_pnl_today_usd=0.0,
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    rows = [
        make_row(market=f"KXBTCD-NCAP-T{i}", strike=80_500.0 + i * 100,
                 model_prob=0.80, yes_bid=0.55, yes_ask=0.60)
        for i in range(5)
    ]
    ex.handle_poll(rows)
    intents = list(db.execute(
        "SELECT market_ticker, count, limit_price_cents FROM intended_orders ORDER BY id"
    ))
    assert len(intents) == 1
    assert intents[0][1] == 10  # per-strike cap binds at 10


def test_top_k_running_held_blocks_same_market_repeats(db, monkeypatch):
    """Candidates on the SAME market → second one's per-strike concentration
    must reflect the first ticket's contracts (so we can't silently double up)."""
    market = "KXBTCD-SAME-T1"
    stub_snapshot(monkeypatch, PositionSnapshot(0.0, 0.0))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    # First ticket fills all MAX_CONTRACTS_PER_STRIKE slots (notional is ample).
    # Remaining candidates on the same market are blocked by concentration cap.
    row1 = make_row(market=market, model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    row2 = make_row(market=market, model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    row3 = make_row(market=market, model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    ex.handle_poll([row1, row2, row3])
    intents = list(db.execute(
        "SELECT count FROM intended_orders ORDER BY id"
    ))
    assert len(intents) == 1
    assert intents[0][0] == MAX_CONTRACTS_PER_STRIKE


def test_candidate_ordering_prefers_sell_to_close(db, monkeypatch):
    market_sell = "KXBTCD-ORDER-TSELL"
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=2.50,
        realized_pnl_today_usd=0.0,
        open_contracts_by_market={(market_sell, "yes"): 5},
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    sell = make_row(market=market_sell, model_prob=0.10, yes_bid=0.50, yes_ask=0.55)
    buy = make_row(market="KXBTCD-ORDER-TBUY", model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    ex.handle_poll([buy, sell])
    [(side, action, market)] = list(db.execute(
        "SELECT side, action, market_ticker FROM intended_orders ORDER BY id LIMIT 1"
    ))
    assert (side, action, market) == ("yes", "sell", market_sell)


def test_candidate_ordering_ranks_buys_by_roi_not_cents(db, monkeypatch):
    stub_snapshot(monkeypatch, PositionSnapshot(0.0, 0.0))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    high_roi = make_row(
        market="KXBTCD-ROI-THIGH",
        strike=80_500.0,
        model_prob=0.20,
        yes_bid=0.01,
        yes_ask=0.10,
    )
    high_cents = make_row(
        market="KXBTCD-ROI-TCENTS",
        strike=80_600.0,
        model_prob=0.80,
        yes_bid=0.01,
        yes_ask=0.60,
    )
    ex.handle_poll([high_cents, high_roi])
    [(market,)] = list(db.execute(
        "SELECT market_ticker FROM intended_orders ORDER BY id LIMIT 1"
    ))
    assert market == "KXBTCD-ROI-THIGH"


# ---- Edge-proportional sizing ----

def test_edge_proportional_sizing_scales_with_conviction(db, monkeypatch):
    """Higher net edge → more contracts (log scaling). With tight notional
    budget so edge scaling is the binding constraint, not per-strike."""
    import math

    # $2 remaining at 25c/contract → 8 max by notional (under per-strike cap).
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD - 2.0,
        realized_pnl_today_usd=0.0,
    ))
    ex_low = Executor(db, trader=None, live=False, profile=_legacy_profile())
    # Moderate edge (~8c net) — well above floor but below full conviction.
    row_low = make_row(market="KXBTCD-LO-T1", model_prob=0.35, yes_ask=0.25, yes_bid=0.20)
    d_low = ex_low.handle_poll([row_low])

    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD - 2.0,
        realized_pnl_today_usd=0.0,
    ))
    ex_high = Executor(db, trader=None, live=False, profile=_legacy_profile())
    # Huge edge (~53c net) — full conviction.
    row_high = make_row(market="KXBTCD-HI-T1", model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
    d_high = ex_high.handle_poll([row_high])

    assert d_low.placed and d_high.placed
    assert d_high.ticket.count > d_low.ticket.count
    # Full conviction should deploy all 8 contracts (floor(2.0 / 0.25)).
    assert d_high.ticket.count == 8
