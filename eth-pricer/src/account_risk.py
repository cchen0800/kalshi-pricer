"""Account-level risk controls derived from bot-attributed local fills."""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger("account_risk")

_BOT_DBS = (
    "kalshi-pricer/pricer.db",
    "kalshi-pricer/pricer.aggressive.db",
    "eth-pricer/pricer.db",
    "eth-pricer/pricer.aggressive.db",
    "sol-pricer/pricer.db",
    "sol-pricer/pricer.aggressive.db",
)


def _midnight_et_ms() -> int:
    now_et = datetime.now(ET)
    midnight_et = datetime.combine(now_et.date(), dtime.min, tzinfo=ET)
    return int(midnight_et.astimezone(timezone.utc).timestamp() * 1000)


def _db_root(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        return None
    path = Path(row[2]).resolve()
    if path.parent.name in {"kalshi-pricer", "eth-pricer", "sol-pricer"}:
        return path.parent.parent
    return path.parent


def _mark_value(conn: sqlite3.Connection, market_ticker: str, side: str) -> float | None:
    settled = conn.execute(
        """
        SELECT settled_yes
        FROM market_settlements
        WHERE market_ticker = ?
        ORDER BY recorded_ts_ms DESC
        LIMIT 1
        """,
        (market_ticker,),
    ).fetchone()
    if settled is not None:
        settled_yes = bool(settled[0])
        won = (side == "yes" and settled_yes) or (side == "no" and not settled_yes)
        return 1.0 if won else 0.0

    poll = conn.execute(
        """
        SELECT yes_bid, no_bid
        FROM polls
        WHERE market_ticker = ?
        ORDER BY ts_ms DESC
        LIMIT 1
        """,
        (market_ticker,),
    ).fetchone()
    if poll is None:
        return None
    return poll[0] if side == "yes" else poll[1]


def _marked_pnl_since(conn: sqlite3.Connection, start_ms: int) -> float:
    rows = conn.execute(
        """
        SELECT
            f.market_ticker,
            f.side AS fill_side,
            f.count,
            f.cash_delta_usd,
            o.side AS intent_side,
            o.action AS intent_action
        FROM fills f
        JOIN intended_orders o ON o.id = f.intended_order_id
        WHERE f.ts_ms >= ?
        """,
        (start_ms,),
    ).fetchall()
    cash = sum(float(r[3] or 0.0) for r in rows)
    pos: dict[tuple[str, str], int] = defaultdict(int)
    for market_ticker, _fill_side, count, _cash, side, action in rows:
        signed = int(count or 0) if action == "buy" else -int(count or 0)
        pos[(market_ticker, side)] += signed

    marked = 0.0
    for (market_ticker, side), count in pos.items():
        if count <= 0:
            continue
        value = _mark_value(conn, market_ticker, side)
        if value is not None:
            marked += count * value
    return cash + marked


def account_loss_today_usd(conn: sqlite3.Connection) -> float | None:
    """Positive account-level bot loss for today, or None if unavailable.

    Uses only fills joined to local intended_orders, so unmatched account-level
    fills from sibling bots or manual trades do not contaminate this stop.
    """
    root = _db_root(conn)
    if root is None:
        return 0.0

    start_ms = _midnight_et_ms()
    total_pnl = 0.0
    seen = set()
    for rel in _BOT_DBS:
        path = (root / rel).resolve()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                total_pnl += _marked_pnl_since(db, start_ms)
        except Exception as exc:
            log.warning("account loss read failed for %s: %s", path, exc)
            return None
    return max(0.0, -total_pnl)
