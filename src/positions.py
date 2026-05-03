"""Bot-portfolio risk snapshot.

Source of truth for the live executor's risk caps. Joins:

  - DB `intended_orders` (mode='live', status='submitted') — bot's order ledger
  - Kalshi `/portfolio/settlements`                       — settlement state + P&L

Deliberately scoped to bot-originated trades. The user may manually trade other
tickers (or even the same KXBTCD markets) on the same Kalshi account; those
do NOT consume the bot's $30 budget.

Fail-closed: if `trader` is set but the Kalshi call fails, `snapshot()` returns
`None`. The caller (executor) MUST refuse to place new orders in that case —
we'd rather miss a trade than open uncapped exposure.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger("positions")


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


def snapshot(conn: sqlite3.Connection, trader: Any | None) -> PositionSnapshot | None:
    """Return the bot's current risk snapshot, or None to signal fail-closed.

    `trader=None` → empty snapshot (the dry-run path; nothing live to read).
    `trader` set but Kalshi unreachable → None (live caller must refuse to trade).
    """
    if trader is None:
        return PositionSnapshot.empty()

    # Direct call (not via trade_history._fetch_settlements which swallows
    # errors and returns []). Risk-critical paths must distinguish "no data"
    # from "data unavailable" — the latter has to fail closed.
    try:
        data = trader._request("GET", "/portfolio/settlements", params={"limit": 200})
        settlements = data.get("settlements", []) or []
    except Exception:
        log.exception("snapshot: settlements fetch failed; failing closed")
        return None

    sett_by_market: dict[str, dict] = {}
    for s in settlements:
        mt = s.get("ticker")
        if mt:
            sett_by_market[mt] = s

    midnight_ms = _midnight_et_ms()
    open_by_market: dict[tuple[str, str], int] = {}
    open_notional_usd = 0.0
    settled_pnl_by_market: dict[str, float] = {}

    for market, side, action, count, limit_cents in conn.execute(
        "SELECT market_ticker, side, action, count, limit_price_cents "
        "FROM intended_orders WHERE mode='live' AND status='submitted'"
    ):
        sett = sett_by_market.get(market)
        if sett is not None:
            # Settled — exclude from open positions; record today's P&L once per market.
            if market in settled_pnl_by_market:
                continue
            settled_ms = _iso_to_ms(sett.get("settled_time"))
            if settled_ms is None or settled_ms < midnight_ms:
                continue
            revenue_usd = _f(sett.get("revenue", 0)) / 100.0
            cost_usd = _f(sett.get("yes_total_cost_dollars")) + _f(sett.get("no_total_cost_dollars"))
            fee_usd = _f(sett.get("fee_cost"))
            settled_pnl_by_market[market] = revenue_usd - cost_usd - fee_usd
            continue

        key = (market, side)
        signed_count = count if action == "buy" else -count
        open_by_market[key] = open_by_market.get(key, 0) + signed_count
        open_notional_usd += (limit_cents / 100.0) * signed_count

    return PositionSnapshot(
        open_notional_usd=max(0.0, open_notional_usd),
        realized_pnl_today_usd=sum(settled_pnl_by_market.values()),
        open_contracts_by_market={k: v for k, v in open_by_market.items() if v > 0},
    )


def kalshi_fee_cents(price_cents: int, count: int) -> int:
    """Kalshi taker fee schedule (approximation, ceil per contract).

    Fee per contract = ceil(0.07 * price * (1 - price) * 100) cents,
    where price is in dollars (0..1). Rounded up at the per-contract level.
    """
    p = price_cents / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0
    return math.ceil(per_contract) * count
