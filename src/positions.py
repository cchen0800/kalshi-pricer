"""Position + PnL accounting from the local DB.

Source of truth for *real* positions is Kalshi's portfolio API; we re-sync from
there on demand. This module reads/writes the local mirror in pricer.db
(`fills`, `settlements`, `intended_orders`) and computes:

  - open notional: sum of |cash spent| on positions not yet settled
  - realized PnL (today): sum of cash_delta over fills + settlements since
    midnight ET
  - count of open contracts per (market_ticker, side)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass
class PositionSnapshot:
    open_notional_usd: float
    realized_pnl_today_usd: float
    open_contracts_by_market: dict[tuple[str, str], int]  # (market, side) -> net count

    def total_loss_today_usd(self) -> float:
        """Positive number = how much we've lost today (realized only)."""
        return max(0.0, -self.realized_pnl_today_usd)


def _midnight_et_ms() -> int:
    now_et = datetime.now(ET)
    midnight_et = datetime.combine(now_et.date(), dtime.min, tzinfo=ET)
    return int(midnight_et.astimezone(timezone.utc).timestamp() * 1000)


def snapshot(conn: sqlite3.Connection) -> PositionSnapshot:
    midnight_ms = _midnight_et_ms()

    # Net contracts per (market, side) from fills.
    # buy adds to position, sell removes from it.
    contracts: dict[tuple[str, str], int] = {}
    open_notional = 0.0
    for market, side, action, price_cents, count in conn.execute(
        "SELECT market_ticker, side, action, fill_price_cents, count FROM fills"
    ):
        key = (market, side)
        delta = count if action == "buy" else -count
        contracts[key] = contracts.get(key, 0) + delta

    # Open notional = sum over open positions of (|net contracts| * avg cost).
    # Approximation: use most recent fill price as the cost basis. For our
    # $30-budget purposes this is close enough — we'll cross-check against
    # Kalshi's portfolio API in the executor.
    last_price: dict[tuple[str, str], int] = {}
    for market, side, price_cents in conn.execute(
        "SELECT market_ticker, side, fill_price_cents FROM fills ORDER BY ts_ms"
    ):
        last_price[(market, side)] = price_cents
    for key, net in contracts.items():
        if net > 0 and key in last_price:
            open_notional += (last_price[key] / 100.0) * net

    # Settled markets no longer count as open notional, even if fills exist.
    settled = {row[0] for row in conn.execute("SELECT market_ticker FROM portfolio_settlements")}
    open_by_market: dict[tuple[str, str], int] = {}
    open_notional = 0.0
    for key, net in contracts.items():
        market, _ = key
        if market in settled or net <= 0:
            continue
        open_by_market[key] = net
        if key in last_price:
            open_notional += (last_price[key] / 100.0) * net

    # Realized PnL today: sum of cash_delta from fills + settlements since midnight.
    realized = 0.0
    for (delta,) in conn.execute(
        "SELECT cash_delta_usd FROM fills WHERE ts_ms >= ?", (midnight_ms,)
    ):
        realized += delta
    for (delta,) in conn.execute(
        "SELECT cash_delta_usd FROM portfolio_settlements WHERE ts_ms >= ?", (midnight_ms,)
    ):
        realized += delta

    return PositionSnapshot(
        open_notional_usd=open_notional,
        realized_pnl_today_usd=realized,
        open_contracts_by_market=open_by_market,
    )


def kalshi_fee_cents(price_cents: int, count: int) -> int:
    """Kalshi taker fee schedule (approximation, ceil per contract).

    Fee per contract = ceil(0.07 * price * (1 - price) * 100) cents,
    where price is in dollars (0..1). Rounded up at the per-contract level.
    """
    p = price_cents / 100.0
    per_contract = 0.07 * p * (1.0 - p) * 100.0  # in cents
    import math
    return math.ceil(per_contract) * count
