"""Read-side helpers for trade history and realized P&L.

Joins three sources of truth:
- DB `intended_orders` (what we tried to do — local, always present)
- Kalshi `/portfolio/fills` (what actually executed — authoritative)
- Kalshi `/portfolio/settlements` (cash-settled markets — authoritative)

Used by dashboard.py (`/api/trades`) and by trade.py (Telegram /pnl, /trades).
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger("trade_history")

# Cache the Kalshi side of the join for a few seconds. Both the dashboard's
# auto-refresh and rapid-fire Telegram commands hammer this; per-request fetches
# would burn rate limit and add latency. Kalshi state changes slowly relative
# to the dashboard's 2s tick.
_CACHE_TTL_S = 20.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def _f(s: Any) -> float:
    """Coerce '0.2100' / '0' / 0.21 / None → float."""
    if s is None or s == "":
        return 0.0
    return float(s)


def _settlement_pnl_usd(s: dict) -> float:
    """Realized $ P&L for a settled market record.

    revenue is in cents; total_cost and fee_cost are dollar strings.
    """
    revenue_usd = _f(s.get("revenue", 0)) / 100.0
    cost_usd = _f(s.get("yes_total_cost_dollars")) + _f(s.get("no_total_cost_dollars"))
    fee_usd = _f(s.get("fee_cost"))
    return revenue_usd - cost_usd - fee_usd


def _fetch_fills(trader, ticker: str | None = None, limit: int = 200) -> list[dict]:
    """All fills, optionally filtered to one market. Best-effort, swallows errors."""
    if trader is None:
        return []
    key = f"fills:{ticker or '*'}:{limit}"
    def _go():
        try:
            data = trader.get_fills(ticker=ticker, limit=limit)
            return data.get("fills", []) or []
        except Exception as e:
            log.warning("get_fills(%s) failed: %s", ticker, e)
            return []
    return _cached(key, _go)


def _fetch_settlements(trader, limit: int = 200) -> list[dict]:
    if trader is None:
        return []
    def _go():
        try:
            # KalshiTrader doesn't expose this as a method on older builds, so call _request.
            data = trader._request("GET", "/portfolio/settlements", params={"limit": limit})
            return data.get("settlements", []) or []
        except Exception as e:
            log.warning("get_settlements failed: %s", e)
            return []
    return _cached(f"settlements:{limit}", _go)


def _fetch_balance(trader) -> dict:
    if trader is None:
        return {}
    def _go():
        try:
            return trader.get_balance()
        except Exception as e:
            log.warning("get_balance failed: %s", e)
            return {}
    return _cached("balance", _go)


def list_trades(
    db: sqlite3.Connection,
    trader: Any,
    *,
    mode: str = "live",
    limit: int = 50,
) -> list[dict]:
    """Return a list of trades (newest first), enriched with fill + settlement data."""
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        SELECT id, ts_ms, mode, event_ticker, market_ticker, side, action,
               limit_price_cents, count, notional_usd, model_prob, edge_cents,
               minutes_left, spot, status, reject_reason, kalshi_order_id
        FROM intended_orders
        WHERE mode = ?
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (mode, limit),
    ).fetchall()
    if not rows:
        return []

    settlements = _fetch_settlements(trader)
    settlement_by_market: dict[str, dict] = {}
    for s in settlements:
        mt = s.get("ticker") or s.get("market_ticker")
        if mt:
            settlement_by_market[mt] = s

    # Pull fills once, broadly; index by order_id for direct join.
    fills = _fetch_fills(trader, ticker=None, limit=200)
    fills_by_order_id: dict[str, list[dict]] = {}
    for f in fills:
        oid = f.get("order_id")
        if oid:
            fills_by_order_id.setdefault(oid, []).append(f)

    out: list[dict] = []
    for r in rows:
        oid = r["kalshi_order_id"]
        order_fills = fills_by_order_id.get(oid, []) if oid else []

        # Volume-weighted average fill price (cents) across all fills for this order.
        fill_price_cents: int | None = None
        filled_count = 0.0
        fill_fees_usd = 0.0
        is_taker_any = False
        if order_fills:
            total_count = 0.0
            total_cost = 0.0
            for f in order_fills:
                cnt = _f(f.get("count_fp") or f.get("count"))
                # Pick the price for the side we traded.
                price = _f(
                    f.get("yes_price_dollars") if r["side"] == "yes"
                    else f.get("no_price_dollars")
                )
                total_count += cnt
                total_cost += cnt * price
                fill_fees_usd += _f(f.get("fee_cost"))
                if f.get("is_taker"):
                    is_taker_any = True
            if total_count > 0:
                fill_price_cents = round((total_cost / total_count) * 100)
                filled_count = total_count

        sett = settlement_by_market.get(r["market_ticker"])
        settled = sett is not None
        market_result = sett.get("market_result") if sett else None
        # Per-market P&L. If multiple of our orders touched the same market,
        # they share this number (we surface that note in the UI).
        market_pnl_usd = _settlement_pnl_usd(sett) if sett else None

        out.append({
            "id": r["id"],
            "ts_ms": r["ts_ms"],
            "ts_iso": datetime.datetime.fromtimestamp(
                r["ts_ms"] / 1000, datetime.timezone.utc
            ).isoformat(),
            "mode": r["mode"],
            "event_ticker": r["event_ticker"],
            "market_ticker": r["market_ticker"],
            "side": r["side"],
            "action": r["action"],
            "count": r["count"],
            "limit_price_cents": r["limit_price_cents"],
            "notional_usd": r["notional_usd"],
            "model_prob": r["model_prob"],
            "edge_cents": r["edge_cents"],
            "minutes_left": r["minutes_left"],
            "spot": r["spot"],
            "status": r["status"],
            "reject_reason": r["reject_reason"],
            "kalshi_order_id": r["kalshi_order_id"],
            # Enriched
            "fill_price_cents": fill_price_cents,
            "filled_count": filled_count,
            "fill_fees_usd": fill_fees_usd,
            "is_taker": is_taker_any if order_fills else None,
            "settled": settled,
            "market_result": market_result,    # 'yes' | 'no' | None
            "market_pnl_usd": market_pnl_usd,  # per-market, shared across orders
        })
    return out


def summarize(
    db: sqlite3.Connection,
    trader: Any,
    *,
    mode: str = "live",
) -> dict:
    """Aggregate stats. Counts are per-order; realized P&L is per-market summed."""
    trades = list_trades(db, trader, mode=mode, limit=500)
    total = len(trades)
    submitted = sum(1 for t in trades if t["status"] == "submitted")
    filled = sum(1 for t in trades if t["fill_price_cents"] is not None)

    # Realized P&L: sum settlements once per market, not once per order
    seen_markets: set[str] = set()
    realized_pnl_usd = 0.0
    won = 0
    lost = 0
    for t in trades:
        if not t["settled"]:
            continue
        mt = t["market_ticker"]
        if mt in seen_markets:
            continue
        seen_markets.add(mt)
        pnl = t["market_pnl_usd"] or 0.0
        realized_pnl_usd += pnl
        # Did our side match the result?
        if t["market_result"] == t["side"]:
            won += 1
        else:
            lost += 1

    open_count = total - len(seen_markets)
    open_notional_usd = sum(
        t["notional_usd"] for t in trades
        if not t["settled"] and t["status"] == "submitted"
    )
    fees_usd = sum(t["fill_fees_usd"] for t in trades)

    bal = _fetch_balance(trader)
    return {
        "total_orders": total,
        "submitted": submitted,
        "filled": filled,
        "settled_markets": len(seen_markets),
        "open_orders": open_count,
        "won": won,
        "lost": lost,
        "win_rate": (won / (won + lost)) if (won + lost) else None,
        "realized_pnl_usd": round(realized_pnl_usd, 4),
        "open_notional_usd": round(open_notional_usd, 4),
        "fees_paid_usd": round(fees_usd, 4),
        "cash_balance_cents": bal.get("balance"),
        "portfolio_value_cents": bal.get("portfolio_value"),
    }


# ---- Telegram formatters ----

def _fmt_usd(n: float | None) -> str:
    if n is None:
        return "—"
    sign = "+" if n >= 0 else "−"
    return f"{sign}${abs(n):.2f}"


def format_pnl_telegram(db: sqlite3.Connection, trader: Any) -> str:
    s = summarize(db, trader)
    cash = s["cash_balance_cents"]
    cash_str = f"${cash/100:,.2f}" if cash is not None else "—"
    win_rate_str = (
        f"{s['win_rate']*100:.0f}%  ({s['won']}W / {s['lost']}L)"
        if s["win_rate"] is not None
        else f"— ({s['won']}W / {s['lost']}L)"
    )
    return (
        "*P&L summary (live)*\n"
        f"orders placed: {s['total_orders']}  ({s['filled']} filled)\n"
        f"settled markets: {s['settled_markets']} → {win_rate_str}\n"
        f"open: {s['open_orders']}  (${s['open_notional_usd']:.2f} notional)\n"
        f"realized P&L: {_fmt_usd(s['realized_pnl_usd'])}\n"
        f"fees paid: ${s['fees_paid_usd']:.2f}\n"
        f"cash: {cash_str}"
    )


def format_trades_telegram(
    db: sqlite3.Connection,
    trader: Any,
    *,
    limit: int = 5,
) -> str:
    trades = list_trades(db, trader, limit=limit)
    if not trades:
        return "*Recent trades* — none yet."
    lines = ["*Recent trades*"]
    for t in trades:
        ts = datetime.datetime.fromtimestamp(
            t["ts_ms"] / 1000, datetime.timezone.utc
        ).astimezone(datetime.timezone(datetime.timedelta(hours=-4)))
        when = ts.strftime("%m/%d %H:%M")
        action = "BUY" if t["action"] == "buy" else "SELL"
        side = t["side"].upper()
        # Strike from market ticker e.g. KXBTCD-26MAY0222-T78299.99
        strike_part = t["market_ticker"].split("-")[-1].replace("T", "$")
        price = t["fill_price_cents"] if t["fill_price_cents"] is not None else t["limit_price_cents"]
        price_label = "fill" if t["fill_price_cents"] is not None else "limit"
        if t["settled"]:
            outcome = "WON" if t["market_result"] == t["side"] else "LOST"
            pnl = _fmt_usd(t["market_pnl_usd"])
            tail = f"→ {outcome} {pnl}"
        elif t["status"] == "submitted" and t["fill_price_cents"] is None:
            tail = "→ resting"
        elif t["fill_price_cents"] is not None:
            tail = "→ open"
        else:
            tail = f"→ {t['status']}"
        lines.append(
            f"`{when}`  {action} {side} ×{t['count']} @ {price}¢ ({price_label})  "
            f"{strike_part}  edge {t['edge_cents']:+.1f}¢  {tail}"
        )
    return "\n".join(lines)
