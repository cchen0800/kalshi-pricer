"""Tests for the bot-portfolio snapshot.

Snapshot's job:
- read this bot's fills from Kalshi /portfolio/fills (scoped to fills whose
  `order_id` matches an intent in this DB, or whose `client_order_id` carries
  this bot's COID prefix if passed)
- read Kalshi /portfolio/settlements only for direction (yes/no) — never
  for cash totals, which are account-level and contaminate per-bot accounting
- read intended_orders ONLY as a short in-flight overlay for BUY orders
  recently submitted that haven't yet appeared in fills
- compute per-bot open notional, contracts, and today's realized P&L
- fail-closed (return None) if either Kalshi call fails
- return empty snapshot if trader is None (dry-run path)

The earlier intent-based netting (subtracting intent SELLs from intent BUYs)
was a fail-open path: SELLs that rested at the bid and got TTL-canceled were
counted as fully filled. The current contract is asymmetric and intentional:
SELLs only reduce position via confirmed fills; BUYs count against notional
even while in-flight, since the worst-case there is fail-closed (block more
trades than necessary).
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
    """Stand-in for KalshiTrader. Routes /portfolio/settlements + /portfolio/fills."""

    def __init__(
        self,
        settlements: list[dict] | None = None,
        fills: list[dict] | None = None,
        fail: bool = False,
    ):
        self._settlements = settlements or []
        self._fills = fills or []
        self._fail = fail

    def _request(self, method: str, path: str, params=None):
        if self._fail:
            raise RuntimeError("simulated Kalshi outage")
        if path == "/portfolio/settlements":
            return {"settlements": self._settlements}
        if path == "/portfolio/fills":
            return {"fills": self._fills}
        return {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def yesterday_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")


_oid_seq = 0


def _next_oid(market: str, action: str) -> str:
    global _oid_seq
    _oid_seq += 1
    return f"oid-{market}-{action}-{_oid_seq}"


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
    kalshi_order_id: str | None = None,
    ts_ms: int | None = None,
) -> str:
    """Insert one intended_orders row. Returns the kalshi_order_id used."""
    oid = kalshi_order_id if kalshi_order_id is not None else _next_oid(market, action)
    db.execute(
        """
        INSERT INTO intended_orders (
            ts_ms, mode, event_ticker, market_ticker, side, action,
            limit_price_cents, count, notional_usd, model_prob, edge_cents,
            minutes_left, spot, client_order_id, status, kalshi_order_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts_ms if ts_ms is not None else int(time.time() * 1000),
            mode, market.split("-T")[0], market, side, action,
            limit_cents, count, (limit_cents / 100.0) * count, 0.5, 20.0, 30.0, 80_000.0,
            coid or f"coid-{oid}", status, oid,
        ),
    )
    return oid


def make_fill(
    *,
    market: str,
    side: str = "yes",
    action: str = "buy",
    count: int = 5,
    fill_cents: int = 25,
    fee_usd: float = 0.0,
    order_id: str | None = None,
    client_order_id: str | None = None,
    ts_iso: str | None = None,
) -> dict:
    """Construct one element of a Kalshi /portfolio/fills response."""
    oid = order_id if order_id is not None else _next_oid(market, action)
    f: dict = {
        "order_id": oid,
        "client_order_id": client_order_id or f"coid-{oid}",
        "ticker": market,
        "side": side,
        "action": action,
        "count": count,
        "fee_cost": str(fee_usd),
        "created_time": ts_iso or now_iso(),
    }
    f["yes_price_dollars" if side == "yes" else "no_price_dollars"] = fill_cents / 100.0
    return f


def insert_filled(
    db: sqlite3.Connection,
    **kwargs,
) -> dict:
    """Insert a matching intent + return the corresponding fill dict.

    The intent and fill share the same kalshi_order_id, so snapshot()
    sees the fill as belonging to this bot. `kwargs` are forwarded to both.
    """
    intent_kwargs = {
        k: kwargs[k] for k in (
            "market", "side", "action", "count", "mode", "status"
        )
        if k in kwargs
    }
    if "fill_cents" in kwargs:
        intent_kwargs["limit_cents"] = kwargs["fill_cents"]
    if "ts_iso" in kwargs:
        ts_ms = int(datetime.fromisoformat(
            kwargs["ts_iso"].replace("Z", "+00:00")
        ).timestamp() * 1000)
        intent_kwargs["ts_ms"] = ts_ms
    oid = insert_intent(db, **intent_kwargs)
    fill_kwargs = {k: v for k, v in kwargs.items() if k != "mode" and k != "status"}
    fill_kwargs["order_id"] = oid
    return make_fill(**fill_kwargs)


# ---- existing contract: trader-None and Kalshi-failure paths ----

def test_trader_none_returns_empty_snapshot(db):
    snap = snapshot(db, None)
    assert snap is not None
    assert snap.open_notional_usd == 0.0
    assert snap.realized_pnl_today_usd == 0.0
    assert snap.open_contracts_by_market == {}


def test_kalshi_failure_returns_none(db):
    snap = snapshot(db, FakeTrader(fail=True))
    assert snap is None


# ---- in-flight overlay (intent without fill) ----

def test_in_flight_buy_intent_counts_against_notional(db):
    """A submitted BUY intent that hasn't yet appeared in /portfolio/fills
    is treated as in-flight exposure. This is the fail-closed direction:
    we'd rather block another BUY than admit one past the cap."""
    insert_intent(db, market="KXETHD-X-T1", count=5, limit_cents=25)
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == pytest.approx(1.25)
    assert snap.open_contracts_by_market == {("KXETHD-X-T1", "yes"): 5}


def test_in_flight_buy_excluded_for_settled_market(db):
    """If the market settled while an in-flight BUY intent was outstanding,
    no live exposure remains — the order can't fill on a closed market."""
    insert_intent(db, market="KXETHD-X-T1", count=5, limit_cents=25)
    settlements = [{
        "ticker": "KXETHD-X-T1",
        "settled_time": now_iso(),
        "market_result": "no",
    }]
    snap = snapshot(db, FakeTrader(settlements=settlements))
    assert snap is not None
    assert snap.open_notional_usd == 0.0
    assert snap.open_contracts_by_market == {}


def test_in_flight_overlay_expires_after_window(db):
    """Past IN_FLIGHT_WINDOW_S (default 180s), an unsynced intent is presumed
    canceled. Otherwise a stuck `submitted` row would forever block new BUYs."""
    old_ts = int(time.time() * 1000) - int(200 * 1000)
    insert_intent(db, market="KXETHD-OLD-T1", count=5, limit_cents=25, ts_ms=old_ts)
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == 0.0


def test_in_flight_sell_intent_does_not_subtract(db):
    """The audit-caught fail-open: a SELL intent that hasn't filled must NOT
    reduce open position. Pre-fix this would have netted to 6 contracts and
    admitted a BUY past the strike cap when the SELL never actually filled."""
    market = "KXETHD-N-T1"
    fill = make_fill(market=market, action="buy", count=10, fill_cents=20, fee_usd=0.0)
    insert_intent(
        db, market=market, action="buy", count=10, limit_cents=20,
        kalshi_order_id=fill["order_id"],
    )
    # SELL intent submitted but no matching fill — simulates the post-place,
    # pre-cancel resting state on a SELL whose limit price wasn't hit.
    insert_intent(db, market=market, action="sell", count=4, limit_cents=22)
    snap = snapshot(db, FakeTrader(fills=[fill]))
    assert snap is not None
    # Full 10 long position retained: SELL didn't fill so it doesn't reduce.
    assert snap.open_contracts_by_market == {(market, "yes"): 10}
    assert snap.open_notional_usd == pytest.approx(2.00)


def test_filled_buys_and_sells_net_correctly(db):
    """When BOTH a BUY and a SELL produced confirmed fills, the SELL fill
    correctly reduces the open position (this is what the intent-based
    netting was approximating, but only when the SELL actually filled)."""
    market = "KXETHD-N-T1"
    buy_fill = make_fill(market=market, action="buy", count=10, fill_cents=20, fee_usd=0.0)
    sell_fill = make_fill(market=market, action="sell", count=4, fill_cents=22, fee_usd=0.0)
    insert_intent(
        db, market=market, action="buy", count=10, limit_cents=20,
        kalshi_order_id=buy_fill["order_id"],
    )
    insert_intent(
        db, market=market, action="sell", count=4, limit_cents=22,
        kalshi_order_id=sell_fill["order_id"],
    )
    snap = snapshot(db, FakeTrader(fills=[buy_fill, sell_fill]))
    assert snap is not None
    assert snap.open_contracts_by_market == {(market, "yes"): 6}
    # 6 contracts × 20¢ VWAP buy price = $1.20.
    assert snap.open_notional_usd == pytest.approx(1.20)


def test_fully_sold_position_releases_all_notional(db):
    """Bought 10 @ 50¢, sold 10 @ 20¢ on a losing trade. Both fills confirmed.
    Net long = 0, no open notional. The pre-settle realized P&L portion
    contributes a -$3.00 loss to today's running tally (sells today)."""
    market = "KXETHD-LOSER-T1"
    buy_fill = make_fill(market=market, action="buy", count=10, fill_cents=50)
    sell_fill = make_fill(market=market, action="sell", count=10, fill_cents=20)
    insert_intent(
        db, market=market, action="buy", count=10, limit_cents=50,
        kalshi_order_id=buy_fill["order_id"],
    )
    insert_intent(
        db, market=market, action="sell", count=10, limit_cents=20,
        kalshi_order_id=sell_fill["order_id"],
    )
    snap = snapshot(db, FakeTrader(fills=[buy_fill, sell_fill]))
    assert snap is not None
    assert snap.open_contracts_by_market == {}
    assert snap.open_notional_usd == 0.0
    # Pre-settle realized: 10 × (20¢ - 50¢) = -$3.00.
    assert snap.realized_pnl_today_usd == pytest.approx(-3.00)
    assert snap.total_loss_today_usd() == pytest.approx(3.00)


def test_avg_buy_price_used_after_partial_sell_at_different_prices(db):
    """Multiple BUY fills at different prices, then a partial SELL fill.
    Remaining contracts should be valued at VWAP of the buys."""
    market = "KXETHD-AVG-T1"
    b1 = make_fill(market=market, action="buy", count=4, fill_cents=40)
    b2 = make_fill(market=market, action="buy", count=6, fill_cents=60)
    s1 = make_fill(market=market, action="sell", count=3, fill_cents=10)
    insert_intent(db, market=market, action="buy", count=4, limit_cents=40,
                  kalshi_order_id=b1["order_id"])
    insert_intent(db, market=market, action="buy", count=6, limit_cents=60,
                  kalshi_order_id=b2["order_id"])
    insert_intent(db, market=market, action="sell", count=3, limit_cents=10,
                  kalshi_order_id=s1["order_id"])
    snap = snapshot(db, FakeTrader(fills=[b1, b2, s1]))
    assert snap is not None
    assert snap.open_contracts_by_market == {(market, "yes"): 7}
    # VWAP buys = (4·40 + 6·60) / 10 = 52¢. 7 × 52¢ = $3.64.
    assert snap.open_notional_usd == pytest.approx(3.64)


# ---- per-bot settlement attribution (multi-bot collision protection) ----

def test_settled_winner_uses_per_bot_payout_not_account_revenue(db):
    """Settlement payout is computed from THIS bot's filled count × $1, not
    from the account-level `revenue` field (which would double-count if a
    sibling bot also held a position in the same market)."""
    market = "KXETHD-W-T1"
    fill = make_fill(market=market, action="buy", count=10, fill_cents=21, fee_usd=0.10)
    insert_intent(db, market=market, action="buy", count=10, limit_cents=21,
                  kalshi_order_id=fill["order_id"])
    settlements = [{
        "ticker": market,
        "settled_time": now_iso(),
        "market_result": "yes",
        # These cash totals are deliberately huge — if snapshot were reading
        # them, it would inflate this bot's P&L. Per-bot logic ignores them.
        "revenue": 999999,
        "yes_total_cost_dollars": "999.99",
        "no_total_cost_dollars": "0.00",
        "fee_cost": "9.99",
    }]
    snap = snapshot(db, FakeTrader(settlements=settlements, fills=[fill]))
    assert snap is not None
    # 10 × $1 payout - 10 × 21¢ cost - $0.10 fee = $7.80.
    assert snap.realized_pnl_today_usd == pytest.approx(7.80)
    assert snap.total_loss_today_usd() == 0.0


def test_settled_loser_attributes_only_bot_cost(db):
    """We held YES, market settled NO. Loss = our buy cost + our fees,
    NOT the account-level total cost (which may include another bot's buys)."""
    market = "KXETHD-X-T1"
    fill = make_fill(market=market, action="buy", count=5, fill_cents=25, fee_usd=0.05)
    insert_intent(db, market=market, action="buy", count=5, limit_cents=25,
                  kalshi_order_id=fill["order_id"])
    insert_intent(db, market="KXETHD-Y-T2", action="buy", count=4, limit_cents=30)
    settlements = [{
        "ticker": market,
        "settled_time": now_iso(),
        "market_result": "no",
        "revenue": 0,
        "yes_total_cost_dollars": "1.25",
        "no_total_cost_dollars": "0.00",
        "fee_cost": "0.05",
    }]
    snap = snapshot(db, FakeTrader(settlements=settlements, fills=[fill]))
    assert snap is not None
    # Settled market excluded from open notional; only the in-flight Y intent counts.
    assert snap.open_notional_usd == pytest.approx(1.20)
    assert snap.open_contracts_by_market == {("KXETHD-Y-T2", "yes"): 4}
    # 5 × 25¢ cost + $0.05 fee, no payout = -$1.30.
    assert snap.realized_pnl_today_usd == pytest.approx(-1.30)
    assert snap.total_loss_today_usd() == pytest.approx(1.30)


def test_yesterday_settlements_dont_count_toward_today(db):
    market = "KXETHD-OLD-T1"
    fill = make_fill(market=market, action="buy", count=10, fill_cents=50,
                     ts_iso=yesterday_iso())
    insert_intent(db, market=market, action="buy", count=10, limit_cents=50,
                  kalshi_order_id=fill["order_id"],
                  ts_ms=int(datetime.fromisoformat(yesterday_iso().replace("Z", "+00:00")).timestamp() * 1000))
    settlements = [{
        "ticker": market,
        "settled_time": yesterday_iso(),
        "market_result": "no",
    }]
    snap = snapshot(db, FakeTrader(settlements=settlements, fills=[fill]))
    assert snap is not None
    # Settled yesterday — no contribution to today's running P&L.
    assert snap.realized_pnl_today_usd == 0.0
    # Settled markets carry no live exposure regardless of side.
    assert snap.open_notional_usd == 0.0


def test_dry_run_intents_ignored(db):
    insert_intent(db, market="KXETHD-D-T1", count=5, limit_cents=25, mode="dry_run")
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == 0.0
    assert snap.open_contracts_by_market == {}


def test_unsubmitted_intents_ignored(db):
    insert_intent(db, market="KXETHD-E-T1", count=5, limit_cents=25, status="error")
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == 0.0


def test_pending_intent_counts_as_in_flight_buy(db):
    """status='pending' is the brief window between writing the intent and
    POST returning. Treat it like 'submitted' for risk-cap purposes (the
    POST may have actually succeeded; we just don't know the order_id yet)."""
    insert_intent(db, market="KXETHD-P-T1", count=5, limit_cents=20, status="pending")
    snap = snapshot(db, FakeTrader())
    assert snap is not None
    assert snap.open_notional_usd == pytest.approx(1.00)


def test_user_manual_market_invisible_to_bot(db):
    """User has manual fills on a market the bot never touched. Per-bot scoping
    means none of it shows up in the bot's risk view."""
    settlements = [{
        "ticker": "KXSNLMENTION-26MAY03-AI",
        "settled_time": now_iso(),
        "market_result": "no",
        "revenue": 0,
        "yes_total_cost_dollars": "9.55",
        "no_total_cost_dollars": "0.00",
        "fee_cost": "0.45",
    }]
    # User's manual fill on the same market — no matching intent in this DB.
    user_fill = make_fill(
        market="KXSNLMENTION-26MAY03-AI", action="buy", count=10, fill_cents=95,
        fee_usd=0.45, order_id="some-other-order",
    )
    snap = snapshot(db, FakeTrader(settlements=settlements, fills=[user_fill]))
    assert snap is not None
    # Despite the fill existing on Kalshi's side, no intent in our DB → not ours.
    assert snap.realized_pnl_today_usd == 0.0
    assert snap.open_notional_usd == 0.0


def test_coid_prefix_attribution_recovers_unlinked_fill(db):
    """If a fill arrives at Kalshi tagged with our COID prefix but the intent
    row in our DB doesn't yet have its kalshi_order_id (the POST-before-DB
    race), the COID prefix lets snapshot still recognize it as ours."""
    market = "KXETHD-Z-T1"
    fill = make_fill(
        market=market, action="buy", count=5, fill_cents=30,
        client_order_id="ethp-abc123", order_id="external-order-id",
    )
    snap = snapshot(db, FakeTrader(fills=[fill]), bot_coid_prefix="ethp-")
    assert snap is not None
    assert snap.open_contracts_by_market == {(market, "yes"): 5}
    assert snap.open_notional_usd == pytest.approx(1.50)


def test_sell_yes_fill_reported_as_no_side_decrements_yes_held(db):
    """Kalshi mirrors a `sell yes` order into the fills feed as
    `side=no, action=sell` (yes_price + no_price = 1.00). Without translating
    back via the intent, the (yes) bucket's sell_count never increments,
    `held_yes` stays inflated, the executor's sell-yes guard keeps passing,
    and each subsequent sell-yes opens an unbounded long-NO that bypasses
    the notional cap. Caused real overage on the 6am EDT 2026-05-06 ETH
    event."""
    market = "KXETHD-N-T1"
    # We bought 10 yes @ 12¢, and submitted sell-yes 10 @ 60¢.
    buy_fill = make_fill(market=market, side="yes", action="buy",
                         count=10, fill_cents=12)
    insert_intent(db, market=market, side="yes", action="buy",
                  count=10, limit_cents=12,
                  kalshi_order_id=buy_fill["order_id"])
    sell_oid = insert_intent(db, market=market, side="yes", action="sell",
                             count=10, limit_cents=60)
    # Kalshi reports the matching fill under the no-side mirror:
    # sell yes @ 60¢ ⇔ no_price = 0.40.
    sell_fill = {
        "order_id": sell_oid,
        "client_order_id": f"coid-{sell_oid}",
        "ticker": market,
        "side": "no",
        "action": "sell",
        "count": 10,
        "no_price_dollars": 0.40,
        "fee_cost": "0.00",
        "created_time": now_iso(),
    }
    snap = snapshot(db, FakeTrader(fills=[buy_fill, sell_fill]))
    assert snap is not None
    # Position fully closed — must NOT show as 10 held with $0 notional held.
    assert snap.open_contracts_by_market == {}
    assert snap.open_notional_usd == 0.0
    # Realized P&L: 10 × (60¢ - 12¢) = $4.80 today (sells today, not settled).
    assert snap.realized_pnl_today_usd == pytest.approx(4.80)


def test_sibling_bot_fill_not_attributed(db):
    """Fill carries a different bot's COID prefix and order_id we don't know.
    Must NOT show up in our snapshot, even though the fill is on the same
    KXETHD market our bot also trades."""
    market = "KXETHD-SHARED-T1"
    sibling_fill = make_fill(
        market=market, action="buy", count=5, fill_cents=30,
        client_order_id="etha-other", order_id="sibling-order",
    )
    snap = snapshot(db, FakeTrader(fills=[sibling_fill]), bot_coid_prefix="ethp-")
    assert snap is not None
    assert snap.open_notional_usd == 0.0
    assert snap.open_contracts_by_market == {}
