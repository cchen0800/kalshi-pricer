"""Bot-portfolio risk snapshot.

Source of truth for the live executor's risk caps. Joins:

  - DB `intended_orders` (mode='live', this DB belongs to one bot) — used
    to attribute Kalshi fills to this bot via `kalshi_order_id`, and as a
    short in-flight overlay for orders just submitted that haven't appeared
    in /portfolio/fills yet.
  - Kalshi `/portfolio/fills`         — authoritative fill data (count, price, fee)
  - Kalshi `/portfolio/settlements`   — settlement direction (yes/no) only

Per-bot scoping. Each bot has its own DB; only fills whose `order_id` matches
an intent in this DB (or whose `client_order_id` carries this bot's COID
prefix) are this bot's. Sibling bots, or the user's manual trades on the same
market on the same Kalshi account, do NOT contaminate this bot's accounting.

Critically: the `revenue` / `*_total_cost_dollars` / `fee_cost` fields on a
settlement record are ACCOUNT-LEVEL — they include every fill on the account
in that market, regardless of which bot or human placed it. Using them
directly would lump multi-bot or manual-trade P&L into this bot's daily-loss
kill switch. We use the settlement only for direction (yes/no won), then
recompute payout against this bot's own filled count.

Fail-closed: if either Kalshi call fails, `snapshot()` returns None. The
executor MUST refuse to place new orders in that case.

Asymmetric overlay (the part the audit caught): a SELL intent that hasn't
yet produced a fill is NOT subtracted from open positions. Pre-fix, a SELL
that rested at the bid and got TTL-canceled was counted as fully filled,
under-reporting open notional and admitting BUYs past the cap. Now SELLs
only reduce position via confirmed fills. BUY intents that haven't yet
produced a fill are still counted (as in-flight exposure) for the duration
of `IN_FLIGHT_WINDOW_S`, which is a fail-closed direction.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger("positions")

# How long to treat a submitted intent as in-flight exposure when no matching
# fill has appeared. The TTL cancel runs at 3s after place; Kalshi fill
# propagation is typically sub-second; we pad generously to absorb brief
# /portfolio/fills outages or pagination races. After this window an unsynced
# intent is presumed canceled (conservative — true exposure is at most what
# the fills API now reports).
IN_FLIGHT_WINDOW_S = 180.0


@dataclass
class _MarketAgg:
    buy_count: int = 0
    buy_cost_usd: float = 0.0           # Σ fill_price × count over all buys for this bot
    sell_count: int = 0
    sell_revenue_usd: float = 0.0       # Σ fill_price × count over all sells
    fees_usd: float = 0.0               # Σ fee_cost over all fills (lifetime)
    sell_count_today: int = 0
    sell_revenue_today_usd: float = 0.0
    fees_today_usd: float = 0.0         # fees on TODAY's fills only (any side)


@dataclass
class PositionSnapshot:
    open_notional_usd: float
    realized_pnl_today_usd: float                              # signed, +ve = profit
    open_contracts_by_market: dict[tuple[str, str], int] = field(default_factory=dict)

    def total_loss_today_usd(self) -> float:
        """Positive number = how much we've lost today (realized only)."""
        return max(0.0, -self.realized_pnl_today_usd)

    @classmethod
    def empty(cls) -> "PositionSnapshot":
        return cls(open_notional_usd=0.0, realized_pnl_today_usd=0.0)


def _midnight_et_ms() -> int:
    now_et = datetime.now(ET)
    midnight_et = datetime.combine(now_et.date(), dtime.min, tzinfo=ET)
    return int(midnight_et.astimezone(timezone.utc).timestamp() * 1000)


def _iso_to_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    s = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        log.warning("can't parse iso time %r", iso)
        return None


def _f(x: Any) -> float:
    if x is None or x == "":
        return 0.0
    return float(x)


def snapshot(
    conn: sqlite3.Connection,
    trader: Any | None,
    *,
    bot_coid_prefix: str | None = None,
    in_flight_window_s: float = IN_FLIGHT_WINDOW_S,
) -> PositionSnapshot | None:
    """Return the bot's risk snapshot, or None to fail-closed.

    `trader=None` → empty snapshot (dry-run path; nothing live to read).
    `trader` set but Kalshi unreachable on either call → None.

    Scoping:
      - This DB's intents define `bot_order_ids` (kalshi_order_id known).
      - `bot_coid_prefix`, if given, also matches fills by client_order_id.
        Pass it from the executor (`profile.coid_prefix`) to close the
        POST-before-DB-record race: a fill arrives at Kalshi tagged with
        our COID before we've finished writing the intent row.
    """
    if trader is None:
        return PositionSnapshot.empty()

    try:
        sett_data = trader._request("GET", "/portfolio/settlements", params={"limit": 200})
        settlements = sett_data.get("settlements", []) or []
    except Exception:
        log.exception("snapshot: settlements fetch failed; failing closed")
        return None

    try:
        fills_data = trader._request("GET", "/portfolio/fills", params={"limit": 200})
        all_fills = fills_data.get("fills", []) or []
    except Exception:
        log.exception("snapshot: fills fetch failed; failing closed")
        return None

    sett_by_market: dict[str, dict] = {}
    for s in settlements:
        mt = s.get("ticker") or s.get("market_ticker")
        if mt:
            sett_by_market[mt] = s

    # Side-translation map: Kalshi mirrors a `sell yes` fill into the fills
    # feed as `side=no, action=sell` (yes/no being symmetric representations of
    # the same trade — yes_price + no_price = 1.00). If we key the per-side
    # aggregation below on Kalshi's reported side, the (yes) bucket's
    # sell_count never increments after a yes-sell, leaving `held_yes`
    # permanently inflated. The executor's sell-yes guard
    # (`held_yes > 0`) then passes forever, so each subsequent sell-yes
    # opens an unbounded NO long that bypasses the notional cap. We undo
    # the translation by looking up our submitted side from the intent.
    bot_order_ids: set[str] = set()
    intent_by_oid: dict[str, tuple[str, str]] = {}
    intent_by_coid: dict[str, tuple[str, str]] = {}
    for oid, coid, i_side, i_action in conn.execute(
        "SELECT kalshi_order_id, client_order_id, side, action "
        "FROM intended_orders WHERE mode='live'"
    ):
        if oid:
            bot_order_ids.add(oid)
            intent_by_oid[oid] = (i_side, i_action)
        if coid:
            intent_by_coid[coid] = (i_side, i_action)

    def _is_bot_fill(f: dict) -> bool:
        oid = f.get("order_id")
        if oid and oid in bot_order_ids:
            return True
        if bot_coid_prefix:
            coid = f.get("client_order_id") or ""
            if coid.startswith(bot_coid_prefix):
                return True
        return False

    midnight_ms = _midnight_et_ms()

    market_state: dict[tuple[str, str], _MarketAgg] = {}
    fills_oids: set[str] = set()
    for f in all_fills:
        if not _is_bot_fill(f):
            continue
        market = f.get("ticker") or f.get("market_ticker") or ""
        if not market:
            continue
        raw_side = f.get("side") or "yes"
        oid = f.get("order_id")
        coid = f.get("client_order_id") or ""
        intent = (
            intent_by_oid.get(oid)
            if oid and oid in intent_by_oid
            else intent_by_coid.get(coid)
        )
        # Use our submitted (side, action) when known; the fill's reported
        # side may be the mirror representation (see comment above).
        if intent is not None:
            side, action = intent
        else:
            side = raw_side
            action = f.get("action") or ""
        count = int(round(_f(f.get("count_fp") or f.get("count"))))
        if count <= 0:
            continue
        # Price is always reported under Kalshi's side. If we flipped sides
        # we need the complement (yes_price = 1.00 - no_price).
        raw_price = _f(
            f.get("yes_price_dollars") if raw_side == "yes"
            else f.get("no_price_dollars")
        )
        price_per = (1.0 - raw_price) if side != raw_side else raw_price
        fee = _f(f.get("fee_cost"))
        if oid:
            fills_oids.add(oid)

        ts_ms = _iso_to_ms(f.get("created_time"))
        is_today = ts_ms is not None and ts_ms >= midnight_ms

        agg = market_state.setdefault((market, side), _MarketAgg())
        if action == "buy":
            agg.buy_count += count
            agg.buy_cost_usd += count * price_per
            agg.fees_usd += fee
            if is_today:
                agg.fees_today_usd += fee
        elif action == "sell":
            agg.sell_count += count
            agg.sell_revenue_usd += count * price_per
            agg.fees_usd += fee
            if is_today:
                agg.sell_count_today += count
                agg.sell_revenue_today_usd += count * price_per
                agg.fees_today_usd += fee

    # In-flight overlay: BUY intents recently submitted that haven't yet
    # produced a fill (Kalshi propagation lag, or never will because they
    # rested past the TTL cancel). NOT applied to SELLs — see module docstring.
    cutoff_ms = int(time.time() * 1000) - int(in_flight_window_s * 1000)
    in_flight_buys: dict[tuple[str, str], tuple[int, float]] = {}
    for market, side, count, limit_cents, oid in conn.execute(
        "SELECT market_ticker, side, count, limit_price_cents, kalshi_order_id "
        "FROM intended_orders "
        "WHERE mode='live' AND status IN ('submitted', 'pending') "
        "AND action='buy' AND ts_ms >= ?",
        (cutoff_ms,),
    ):
        if oid and oid in fills_oids:
            continue                       # fills already account for it
        if market in sett_by_market:
            continue                       # market settled; intent moot
        key = (market, side)
        cnt, cost = in_flight_buys.get(key, (0, 0.0))
        in_flight_buys[key] = (cnt + count, cost + count * (limit_cents / 100.0))

    # ---- open positions / notional ----
    open_by_market: dict[tuple[str, str], int] = {}
    open_notional_usd = 0.0
    for key, agg in market_state.items():
        market, _side = key
        if market in sett_by_market:
            continue                       # settled markets carry no live exposure
        net = agg.buy_count - agg.sell_count
        if net <= 0:
            continue
        avg_buy_price = agg.buy_cost_usd / agg.buy_count if agg.buy_count else 0.0
        open_by_market[key] = net
        open_notional_usd += net * avg_buy_price
    for key, (cnt, cost) in in_flight_buys.items():
        open_by_market[key] = open_by_market.get(key, 0) + cnt
        open_notional_usd += cost

    # ---- realized P&L today ----
    # Per-bot, never read settlement.cash_delta directly. Three components:
    #   (a) settled today and bot held position: payout - cost - fees
    #   (b) unsettled but partial-closed today: sell_revenue_today - sells × VWAP_buy - fees_today
    #   (c) settled today and bot DIDN'T hold (no fills) → 0
    realized_pnl_usd = 0.0
    for key, agg in market_state.items():
        market, side = key
        sett = sett_by_market.get(market)
        if sett is not None:
            settled_ms = _iso_to_ms(sett.get("settled_time"))
            if settled_ms is None or settled_ms < midnight_ms:
                continue                   # settled before today
            settled_yes = (sett.get("market_result") == "yes")
            net_at_settle = max(0, agg.buy_count - agg.sell_count)
            wins = (
                (side == "yes" and settled_yes) or (side == "no" and not settled_yes)
            )
            payout = (net_at_settle * 1.0) if wins else 0.0
            pnl = (
                agg.sell_revenue_usd + payout - agg.buy_cost_usd - agg.fees_usd
            )
            realized_pnl_usd += pnl
        else:
            if agg.sell_count_today <= 0 or agg.buy_count <= 0:
                continue
            avg_buy_price = agg.buy_cost_usd / agg.buy_count
            cost_basis_of_sells = agg.sell_count_today * avg_buy_price
            pnl_today = (
                agg.sell_revenue_today_usd
                - cost_basis_of_sells
                - agg.fees_today_usd
            )
            realized_pnl_usd += pnl_today

    return PositionSnapshot(
        open_notional_usd=open_notional_usd,
        realized_pnl_today_usd=realized_pnl_usd,
        open_contracts_by_market=open_by_market,
    )


def kalshi_fee_cents(price_cents: int, count: int) -> int:
    """Kalshi taker fee schedule (approximation, ceil per contract).

    Fee per contract = ceil(0.07 * price * (1 - price) * 100) cents,
    where price is in dollars (0..1). Rounded up at the per-contract level.
    """
    p = price_cents / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0
    return math.ceil(per_contract) * count
