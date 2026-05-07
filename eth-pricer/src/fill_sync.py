"""Periodic sync of Kalshi fills + settlements into the local SQLite DB.

The dashboard already reads Kalshi live for displayed P&L (see trade_history.py),
so this is purely for offline analysis: calibration sweeps, regime
backtests, anything that needs to JOIN our intended_orders against actual
fills/settlements without hitting the API every time.

Idempotent: every insert is INSERT OR IGNORE keyed on the Kalshi-side
identifier (trade_id for fills, market_ticker for settlements), so running
the sync repeatedly is safe.

The sync is best-effort: any HTTP / parse error is logged and swallowed —
trading is the priority and stale local mirror data is acceptable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from src.kalshi_trader import KalshiTrader

log = logging.getLogger("fill_sync")


def _f(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Add columns / indexes the sync needs but the original schema lacks.

    Original schema (db.py) didn't have kalshi_order_id on fills (so we can't
    link a fill to the intended_order that produced it) and had no UNIQUE on
    kalshi_trade_id (so reruns would dupe). Both are additive changes, safe
    to apply on every startup."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fills)").fetchall()}
    if "kalshi_order_id" not in cols:
        conn.execute("ALTER TABLE fills ADD COLUMN kalshi_order_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_trade_id ON fills(kalshi_trade_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(kalshi_order_id)"
    )


def sync_fills(conn: sqlite3.Connection, trader: KalshiTrader, *, limit: int = 200) -> int:
    """Pull recent fills from Kalshi and upsert into the local fills table.

    Links each fill back to its originating intended_order via kalshi_order_id.
    Returns the number of new rows inserted."""
    try:
        data = trader.get_fills(limit=limit)
    except Exception as e:
        log.warning("get_fills failed: %s", e)
        return 0
    fills = data.get("fills") or []
    if not fills:
        return 0

    order_id_to_intent: dict[str, int] = {}
    for r in conn.execute(
        "SELECT id, kalshi_order_id FROM intended_orders WHERE kalshi_order_id IS NOT NULL"
    ).fetchall():
        order_id_to_intent[r[1]] = r[0]

    new = 0
    for f in fills:
        trade_id = f.get("trade_id") or f.get("fill_id")
        if not trade_id:
            continue
        side = f.get("side") or ""
        price = (
            _f(f.get("yes_price_dollars")) if side == "yes"
            else _f(f.get("no_price_dollars"))
        )
        fill_price_cents = int(round(price * 100))
        count = int(round(_f(f.get("count_fp") or f.get("count"))))
        fee = _f(f.get("fee_cost"))
        action = f.get("action") or ""
        cash_delta = -(price * count + fee) if action == "buy" else (price * count - fee)
        ts_ms = 0
        ct = f.get("created_time")
        if ct:
            try:
                from datetime import datetime
                ts_ms = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ts_ms = int(time.time() * 1000)
        order_id = f.get("order_id")
        intent_id = order_id_to_intent.get(order_id) if order_id else None

        cur = conn.execute(
            """
            INSERT OR IGNORE INTO fills (
                ts_ms, intended_order_id, market_ticker, side, action,
                fill_price_cents, count, fee_usd, cash_delta_usd,
                kalshi_trade_id, kalshi_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_ms, intent_id, f.get("ticker") or f.get("market_ticker") or "",
                side, action, fill_price_cents, count, fee, cash_delta,
                trade_id, order_id,
            ),
        )
        if cur.rowcount:
            new += 1
    return new


def sync_settlements(
    conn: sqlite3.Connection, trader: KalshiTrader, *, limit: int = 200
) -> int:
    """Pull recent portfolio settlements and upsert. UNIQUE on market_ticker
    means a re-sync is a no-op; a settled market never re-settles.

    cash_delta_usd is the net P&L vs cost basis as reported by Kalshi. Note
    this is contaminated by manual trades on the same market (Kalshi does not
    split account-level settlements by client_order_id). For attributing P&L
    to bot-only orders, prefer joining intended_orders → fills → settlement
    side rather than reading cash_delta_usd directly."""
    try:
        data = trader._request("GET", "/portfolio/settlements", params={"limit": limit})
    except Exception as e:
        log.warning("get_settlements failed: %s", e)
        return 0
    settlements = data.get("settlements") or []
    if not settlements:
        return 0

    new = 0
    for s in settlements:
        ticker = s.get("ticker") or s.get("market_ticker")
        if not ticker:
            continue
        market_result = s.get("market_result") or ""
        settled_yes = 1 if market_result == "yes" else 0
        revenue = _f(s.get("revenue")) / 100.0  # API returns cents
        cost = _f(s.get("yes_total_cost_dollars")) + _f(s.get("no_total_cost_dollars"))
        fee = _f(s.get("fee_cost"))
        cash_delta = revenue - cost - fee
        ts_ms = 0
        st = s.get("settled_time")
        if st:
            try:
                from datetime import datetime
                ts_ms = int(datetime.fromisoformat(st.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ts_ms = int(time.time() * 1000)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO portfolio_settlements (
                ts_ms, market_ticker, settled_yes, cash_delta_usd, raw_response
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (ts_ms, ticker, settled_yes, cash_delta, json.dumps(s)),
        )
        if cur.rowcount:
            new += 1
    return new


class FillSyncer:
    """Throttled wrapper. Call .maybe_sync() liberally — actual API calls
    happen at most once per `interval_s`."""

    def __init__(self, trader: KalshiTrader, *, interval_s: float = 60.0) -> None:
        self.trader = trader
        self.interval_s = interval_s
        self._last_run = 0.0
        self._schema_ready = False

    def maybe_sync(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_run < self.interval_s:
            return
        self._last_run = now
        try:
            if not self._schema_ready:
                _ensure_schema(conn)
                self._schema_ready = True
            nf = sync_fills(conn, self.trader)
            ns = sync_settlements(conn, self.trader)
            if nf or ns:
                log.info("fill_sync: +%d fills, +%d settlements", nf, ns)
        except Exception:
            log.exception("fill_sync failed; will retry")
