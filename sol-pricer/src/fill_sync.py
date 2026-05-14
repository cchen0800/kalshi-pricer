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
import math
import re
import sqlite3
import time
from typing import Any, Callable

from src.kalshi_trader import KalshiTrader
from src.notify import TelegramNotifier

log = logging.getLogger("fill_sync")

_EVENT_RE = re.compile(r"^KX(?:SOL|ETH|BTC)D-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")


def _f(v: Any) -> float:
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _md_escape(s: str) -> str:
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def _format_strike(market_ticker: str) -> str:
    last = market_ticker.rsplit("-", 1)[-1]
    if not last.startswith("T"):
        return last
    try:
        n = float(last[1:])
    except ValueError:
        return last
    return f"above ${math.ceil(n):,d}"


def _format_event(event_ticker: str) -> str:
    m = _EVENT_RE.match(event_ticker or "")
    if not m:
        return event_ticker or "-"
    h = int(m.group(4))
    h12 = (h % 12) or 12
    ampm = "AM" if h < 12 else "PM"
    return f"{m.group(2)} {m.group(3)} {h12} {ampm} ET"


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


def sync_fills(
    conn: sqlite3.Connection,
    trader: KalshiTrader,
    *,
    limit: int = 200,
    on_new_fill: Callable[[dict[str, Any]], None] | None = None,
) -> int:
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

    order_id_to_intent: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        """
        SELECT id, mode, event_ticker, market_ticker, side, action,
               limit_price_cents, count, notional_usd, model_prob,
               model_prob_calibrated, edge_cents, minutes_left, spot,
               kalshi_order_id, bot_id
        FROM intended_orders
        WHERE kalshi_order_id IS NOT NULL
        """
    ).fetchall():
        order_id_to_intent[r[14]] = {
            "id": r[0],
            "mode": r[1],
            "event_ticker": r[2],
            "market_ticker": r[3],
            "side": r[4],
            "action": r[5],
            "limit_price_cents": r[6],
            "count": r[7],
            "notional_usd": r[8],
            "model_prob": r[9],
            "model_prob_calibrated": r[10],
            "edge_cents": r[11],
            "minutes_left": r[12],
            "spot": r[13],
            "kalshi_order_id": r[14],
            "bot_id": r[15],
        }

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
        ts_ms = 0
        ct = f.get("created_time")
        if ct:
            try:
                from datetime import datetime
                ts_ms = int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ts_ms = int(time.time() * 1000)
        order_id = f.get("order_id")
        intent = order_id_to_intent.get(order_id) if order_id else None
        if intent is None:
            continue
        intent_id = intent["id"] if intent else None
        effective_side = intent["side"] if intent else side
        effective_action = intent["action"] if intent else action
        effective_price = (1.0 - price) if effective_side != side else price
        effective_price_cents = int(round(effective_price * 100))
        cash_delta = (
            -(effective_price * count + fee)
            if effective_action == "buy"
            else (effective_price * count - fee)
        )

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
            if on_new_fill is not None and intent is not None:
                on_new_fill({
                    **intent,
                    "ts_ms": ts_ms,
                    "fill_side": effective_side,
                    "fill_action": effective_action,
                    "fill_price_cents": effective_price_cents,
                    "fill_count": count,
                    "fill_fee_usd": fee,
                    "fill_cash_delta_usd": cash_delta,
                    "kalshi_trade_id": trade_id,
                })
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

    def __init__(
        self,
        trader: KalshiTrader,
        *,
        interval_s: float = 60.0,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.trader = trader
        self.interval_s = interval_s
        self.notifier = notifier
        self._notify_after_ts_ms = int((time.time() - 5.0) * 1000)
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
            nf = sync_fills(
                conn,
                self.trader,
                on_new_fill=self._notify_fill if self._notifications_enabled() else None,
            )
            ns = sync_settlements(conn, self.trader)
            if nf or ns:
                log.info("fill_sync: +%d fills, +%d settlements", nf, ns)
        except Exception:
            log.exception("fill_sync failed; will retry")

    def _notifications_enabled(self) -> bool:
        return self.notifier is not None and self.notifier.enabled

    def _notify_fill(self, fill: dict[str, Any]) -> None:
        if not self._notifications_enabled():
            return
        if int(fill.get("ts_ms") or 0) < self._notify_after_ts_ms:
            return
        action = str(fill.get("fill_action") or fill.get("action") or "").lower()
        side = str(fill.get("fill_side") or fill.get("side") or "").lower()
        if action == "buy":
            side_word = "BUY NO" if side == "no" else "BUY YES"
        else:
            side_word = "SELL NO" if side == "no" else "SELL YES"
        count = int(fill.get("fill_count") or 0)
        price_cents = int(fill.get("fill_price_cents") or 0)
        notional_usd = (price_cents / 100.0) * count
        model_prob = fill.get("model_prob_calibrated")
        if model_prob is None:
            model_prob = fill.get("model_prob")
        bot_md = _md_escape(str(fill.get("bot_id") or "bot"))
        mode_md = _md_escape(str(fill.get("mode") or "live").upper())
        strike_md = _md_escape(_format_strike(str(fill.get("market_ticker") or "")))
        event_md = _md_escape(_format_event(str(fill.get("event_ticker") or "")))
        msg = (
            f"*[{mode_md} {bot_md}] {side_word} FILLED*  "
            f"{count} x {price_cents}¢  (${notional_usd:.2f})\n"
            f"{strike_md} · {event_md}\n"
            f"spot ${float(fill.get('spot') or 0):,.0f} · "
            f"T-{float(fill.get('minutes_left') or 0):.0f}min · "
            f"model {float(model_prob or 0)*100:.1f}¢ · "
            f"edge +{float(fill.get('edge_cents') or 0):.1f}¢\n"
            f"_filled_"
        )
        self.notifier.send(msg)
