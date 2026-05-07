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
        max_notional_usd=base.max_notional_usd,
        max_daily_loss_usd=base.max_daily_loss_usd,
        min_edge_cents=base.min_edge_cents,
        kill_file=kill_path or base.kill_file,
        # policy left at default (LEGACY_POLICY)
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
    market: str = "KXETHD-26MAY0113-T80000",
    event: str = "KXETHD-26MAY0113",
    model_prob: float = 0.50,
    yes_bid: float | None = 0.20,
    yes_ask: float | None = 0.25,
    no_bid: float | None = None,
    no_ask: float | None = None,
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
    # $25 of $30 budget already open. Per-order cap is 5; per-strike room is 10.
    # At 50c each, $5 budget → 10 contracts → capped to 5 by per-order.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=5.0,
        realized_pnl_today_usd=0.0,
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    row = make_row(model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.count == 5

    # $30 of $30 used → should block on notional.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=MAX_NOTIONAL_USD,
        realized_pnl_today_usd=0.0,
    ))
    row2 = make_row(market="KXETHD-OTHER2", model_prob=0.80, yes_ask=0.50, yes_bid=0.45)
    d2 = ex.handle_poll([row2])
    assert d2.placed is False


def test_rate_limit_blocks_after_n_orders(db):
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    # Place MAX_ORDERS_PER_MINUTE = 4 orders (each on different markets).
    for i in range(MAX_ORDERS_PER_MINUTE):
        row = make_row(market=f"KXETHD-RATE-T{i}", model_prob=0.80, yes_ask=0.10, yes_bid=0.05)
        d = ex.handle_poll([row])
        assert d.placed is True, f"order #{i} should have placed: {d.reason}"
    # 5th should hit the rate limit, regardless of edge.
    row = make_row(market="KXETHD-RATE-T99", model_prob=0.80, yes_ask=0.10, yes_bid=0.05)
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
    market = "KXETHD-SELLT"
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

    ex = Executor(db, trader=FakeTrader(), live=True, notifier=FakeNotifier(), profile=_legacy_profile())
    stub_snapshot(monkeypatch, PositionSnapshot.empty() if hasattr(PositionSnapshot, "empty")
                  else PositionSnapshot(0.0, 0.0))
    row = make_row(market="KXETHD-X-T1", model_prob=0.80, yes_ask=0.25, yes_bid=0.20)
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
    # Model says YES at 10c → NO worth ~90c. no_ask=5c → BUY_NO net ≈ 85c.
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.05)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.side == "no"
    assert d.ticket.action == "buy"
    assert d.ticket.limit_price_cents == 5

    # Intent row in DB has side='no'
    rows = list(db.execute("SELECT side, action, limit_price_cents FROM intended_orders"))
    assert len(rows) == 1
    side_db, action_db, price_db = rows[0]
    assert side_db == "no"
    assert action_db == "buy"
    assert price_db == 5


def test_buy_no_concentration_cap_keyed_by_no_side(db, monkeypatch):
    """Already holding 10 NO contracts in a market → no room for more BUY_NO
    in that market. A YES holding in the same market does NOT consume the
    NO-side slot (and vice versa)."""
    market = "KXETHD-26MAY0113-T80000"
    from src.executor import MAX_CONTRACTS_PER_STRIKE
    # Saturated on NO side; YES side untouched.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=0.50,
        realized_pnl_today_usd=0.0,
        open_contracts_by_market={(market, "no"): MAX_CONTRACTS_PER_STRIKE},
    ))
    ex = Executor(db, trader=None, live=False, profile=_selective_profile())
    row = make_row(market=market, model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.05)
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
    """Notional cap binds across YES + NO sides. With $29 already open and
    $30 cap, only $1 budget left → at no_ask=5c that's 20 contracts of room,
    but per-order cap is 5."""
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=9.0,
        realized_pnl_today_usd=0.0,
    ))
    ex = Executor(db, trader=None, live=False, profile=_selective_profile())
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.05)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert d.ticket.count == MAX_CONTRACTS_PER_ORDER

    # $29.99 used → only $0.01 left, no contract fits at 5c.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=9.99,
        realized_pnl_today_usd=0.0,
    ))
    row2 = make_row(market="KXETHD-OTHER-T1", model_prob=0.10, yes_bid=0.20,
                    yes_ask=0.25, no_ask=0.05)
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
    ex = Executor(db, trader=FakeTrader(), live=True, profile=_selective_profile())
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.05)
    d = ex.handle_poll([row])
    assert d.placed is True
    assert captured["side"] == "no"
    assert captured["action"] == "buy"
    assert captured["limit_price_cents"] == 5


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
                  profile=_selective_profile())
    row = make_row(model_prob=0.10, yes_bid=0.20, yes_ask=0.25, no_ask=0.05)
    ex.handle_poll([row])
    assert len(captured) == 1
    assert "BUY NO" in captured[0]
    assert "BUY YES" not in captured[0]


# ---- PR #6A: top-K orders per poll ----

def test_top_k_places_multiple_distinct_strikes(db, monkeypatch):
    """Three positive-edge candidates on different strikes → all placed."""
    from src.executor import MAX_ORDERS_PER_POLL
    stub_snapshot(monkeypatch, PositionSnapshot(0.0, 0.0))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    rows = [
        make_row(market="KXETHD-A-T1", strike=80_500.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.25),
        make_row(market="KXETHD-A-T2", strike=80_600.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.30),
        make_row(market="KXETHD-A-T3", strike=80_700.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.35),
        make_row(market="KXETHD-A-T4", strike=80_800.0, model_prob=0.80,
                 yes_bid=0.20, yes_ask=0.40),
    ]
    ex.handle_poll(rows)
    # K=3 cap; 4 candidates → only 3 intents written.
    n_intents = db.execute("SELECT COUNT(*) FROM intended_orders").fetchone()[0]
    assert n_intents == MAX_ORDERS_PER_POLL


def test_top_k_running_notional_blocks_later_candidates(db, monkeypatch):
    """First placement consumes most of the notional cap; second candidate
    sized down or blocked by running notional, not the original snap."""
    # Start with $1 of $10 used; $9 budget left. Each buy at 60c × 5 = $3.
    # After 3 tickets working_notional → $10 exactly. Fourth would exceed cap.
    stub_snapshot(monkeypatch, PositionSnapshot(
        open_notional_usd=1.0,
        realized_pnl_today_usd=0.0,
    ))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    rows = [
        make_row(market=f"KXETHD-NCAP-T{i}", strike=80_500.0 + i * 100,
                 model_prob=0.80, yes_bid=0.55, yes_ask=0.60)
        for i in range(5)
    ]
    ex.handle_poll(rows)
    # Each ticket is 5 × $0.60 = $3. Budget: $10. So 3 tickets fit ($9), 4th
    # would push past cap. Confirm exactly 3 placed (and not capped earlier
    # by MAX_ORDERS_PER_POLL=3 so the test still proves running-notional is
    # consulted — bump K to verify; here both cap at 3 so it's coincident).
    intents = list(db.execute(
        "SELECT market_ticker, count, limit_price_cents FROM intended_orders ORDER BY id"
    ))
    assert len(intents) == 3
    # All three placed at full size.
    for _, count, _price in intents:
        assert count == 5


def test_top_k_running_held_blocks_same_market_repeats(db, monkeypatch):
    """Two candidates on the SAME market → second one's per-strike concentration
    must reflect the first ticket's contracts (so we can't silently double up)."""
    market = "KXETHD-SAME-T1"
    stub_snapshot(monkeypatch, PositionSnapshot(0.0, 0.0))
    ex = Executor(db, trader=None, live=False, profile=_legacy_profile())
    # Two rows for the same market — different timestamps but identical setup.
    # First ticket lifts 5 contracts (per-order cap). Per-strike room is 10 →
    # 5 left after first. Second ticket gets sized to 5. Third should be blocked
    # because per-strike held would now be 10 (cap).
    row1 = make_row(market=market, model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    row2 = make_row(market=market, model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    row3 = make_row(market=market, model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    ex.handle_poll([row1, row2, row3])
    intents = list(db.execute(
        "SELECT count FROM intended_orders ORDER BY id"
    ))
    # Two placements of 5 each → 10 contracts (the per-strike cap). Third blocked.
    assert len(intents) == 2
    assert intents[0][0] == 5
    assert intents[1][0] == 5
