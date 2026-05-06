"""FastAPI dashboard. Runs the engine in a background thread; serves a live
HTML page and JSON endpoints reading from the same SQLite file the engine
writes to.

Launch:
    python dashboard.py                    # http://localhost:8000, owns its own engine
    python dashboard.py --no-engine        # HTTP only — read DB written by trade.py
    python dashboard.py --port 5051        # bind a different port (e.g. behind Caddy)

Read-only. The engine inside is the same read-only one as `python main.py`.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from main import load_config
from src.btc_feed import CoinbaseFeed
from src.db import open_db
from src.engine import EngineConfig, event_close_utc, run as run_engine
from src.executor import BOT_PROFILES
from src.trade_history import list_trades, summarize

NY_TZ = ZoneInfo("America/New_York")


def _event_title(event_ticker: str) -> str:
    """Render the title in Kalshi's own phrasing.

    `KXBTCD-26APR2802` → `Bitcoin price tomorrow at 2am EDT`.
    Falls back to the raw ticker if it can't be parsed.
    """
    close_dt = event_close_utc(event_ticker)
    if close_dt is None:
        return event_ticker
    ny = close_dt.astimezone(NY_TZ)
    today = datetime.now(NY_TZ).date()
    if ny.date() == today:
        when = "today"
    elif ny.date() == today + timedelta(days=1):
        when = "tomorrow"
    else:
        when = f"on {ny.strftime('%b')} {ny.day}"

    hour12 = ny.hour % 12 or 12
    ampm = "am" if ny.hour < 12 else "pm"
    time_str = f"{hour12}{ampm}" if ny.minute == 0 else f"{hour12}:{ny.minute:02d}{ampm}"
    return f"Bitcoin price {when} at {time_str} {ny.strftime('%Z')}"

ROOT = Path(__file__).parent
TEMPLATE = ROOT / "templates" / "dashboard.html"

_state: dict = {"thread": None, "stop": None, "cfg": None}


def _engine_thread_target(cfg: EngineConfig, stop: threading.Event) -> None:
    try:
        run_engine(cfg, stop_event=stop)
    except Exception:
        import logging
        logging.getLogger("dashboard").exception("engine thread crashed")


def _try_init_trader():
    """Best-effort KalshiTrader for the read-only Trades tab.

    The dashboard never calls write methods. If creds aren't available
    (e.g. running locally without keys), we just degrade — the tab will
    show order intents from the DB but no fill/settlement enrichment.
    """
    try:
        from src.kalshi_trader import KalshiTrader
        return KalshiTrader()
    except Exception as e:
        import logging
        logging.getLogger("dashboard").warning(
            "KalshiTrader init failed; Trades tab will be DB-only: %s", e
        )
        return None


def _aggressive_db_path(selective_path: str) -> str:
    """Sibling DB for the aggressive bot.

    By convention `trade.py --profile aggressive` writes to `pricer.aggressive.db`
    in the same directory as the selective DB. Derive the path by inserting
    `.aggressive` before the `.db` suffix so the dashboard can read both.
    """
    p = Path(selective_path)
    if p.suffix == ".db":
        return str(p.with_suffix("")) + ".aggressive.db"
    return selective_path + ".aggressive.db"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    feed = CoinbaseFeed()
    trader = _try_init_trader()
    no_engine = os.environ.get("DASHBOARD_NO_ENGINE") == "1"
    aggressive_db = _aggressive_db_path(cfg.db_path)
    if no_engine:
        # Pure HTTP frontend. Some other process (typically trade.py) is
        # writing to the same DB; we just read.
        _state.update(
            thread=None, stop=None, cfg=cfg, feed=feed, trader=trader,
            aggressive_db=aggressive_db,
        )
        try:
            yield
        finally:
            feed.close()
            if trader is not None:
                try: trader.close()
                except Exception: pass
        return

    stop = threading.Event()
    th = threading.Thread(target=_engine_thread_target, args=(cfg, stop), daemon=True)
    th.start()
    _state.update(
        thread=th, stop=stop, cfg=cfg, feed=feed, trader=trader,
        aggressive_db=aggressive_db,
    )
    try:
        yield
    finally:
        stop.set()
        th.join(timeout=10)
        feed.close()
        if trader is not None:
            try: trader.close()
            except Exception: pass


app = FastAPI(lifespan=lifespan, title="kalshi-pricer dashboard")


def _conn() -> sqlite3.Connection:
    cfg: EngineConfig = _state["cfg"]
    if cfg is None:
        raise HTTPException(503, "engine config not loaded yet")
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    return c


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(TEMPLATE.read_text())


@app.get("/api/state")
def api_state() -> JSONResponse:
    cfg: EngineConfig = _state["cfg"]
    with _conn() as c:
        latest = c.execute("SELECT MAX(ts_ms) FROM polls").fetchone()[0]
        if latest is None:
            return JSONResponse({"ready": False, "threshold_cents": cfg.edge_threshold_cents})
        rows = c.execute(
            """
            SELECT * FROM polls WHERE ts_ms = ? ORDER BY strike ASC
            """,
            (latest,),
        ).fetchall()
        if not rows:
            return JSONResponse({"ready": False, "threshold_cents": cfg.edge_threshold_cents})
        head = rows[0]
        payload = {
            "ready": True,
            "ts_ms": head["ts_ms"],
            "event_ticker": head["event_ticker"],
            "event_title": _event_title(head["event_ticker"]),
            "spot": head["spot"],
            "sigma": head["sigma"],
            "minutes_left": head["minutes_left"],
            "proxy_source": head["proxy_source"],
            "threshold_cents": cfg.edge_threshold_cents,
            "strikes": [
                {
                    "market_ticker": r["market_ticker"],
                    "strike": r["strike"],
                    "model_prob": r["model_prob"],
                    "yes_bid": r["yes_bid"],
                    "yes_ask": r["yes_ask"],
                    "yes_bid_size": r["yes_bid_size"],
                    "yes_ask_size": r["yes_ask_size"],
                    "volume": r["volume"],
                    "edge_cents": r["edge_cents"],
                }
                for r in rows
            ],
        }
        return JSONResponse(payload)


@app.get("/api/history/{market_ticker}")
def api_history(market_ticker: str, limit: int = 120) -> JSONResponse:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT ts_ms, model_prob, yes_bid, yes_ask, edge_cents, spot, sigma, minutes_left
            FROM polls WHERE market_ticker = ?
            ORDER BY ts_ms DESC LIMIT ?
            """,
            (market_ticker, limit),
        ).fetchall()
    return JSONResponse({"market_ticker": market_ticker, "rows": [dict(r) for r in rows[::-1]]})


@app.get("/api/spot")
def api_spot() -> JSONResponse:
    """Live BTC spot from Coinbase, fetched fresh on every request.

    Used by the dashboard header to tick faster than the 30s engine cadence.
    """
    feed: CoinbaseFeed | None = _state.get("feed")
    if feed is None:
        raise HTTPException(503, "feed not ready")
    try:
        s = feed.get_spot()
    except Exception as e:
        raise HTTPException(502, f"coinbase fetch failed: {e}")
    return JSONResponse({"price": s.price, "bid": s.bid, "ask": s.ask, "ts_ms": s.epoch_ms})


@app.get("/api/trades")
def api_trades(limit: int = 50, bot: str = "selective") -> JSONResponse:
    """Trade history + aggregate P&L summary for one of the asset's bots.

    `bot` is 'selective' (default) or 'aggressive'. Picks which DB to read.
    Both bots write mode='live' to their own DB, so we always filter mode='live'.
    """
    cfg: EngineConfig = _state["cfg"]
    trader = _state.get("trader")
    if bot == "aggressive":
        db_path = _state.get("aggressive_db") or cfg.db_path
    else:
        db_path = cfg.db_path
    with open_db(db_path) as db:
        trades = list_trades(db, trader, mode="live", limit=limit)
        summary = summarize(db, trader, mode="live")
    profile = BOT_PROFILES.get(bot)
    if profile is not None:
        summary["allocated_capital_usd"] = profile.max_notional_usd
    return JSONResponse({"trades": trades, "summary": summary, "bot": bot})


@app.get("/api/events")
def api_events() -> JSONResponse:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT event_ticker, COUNT(*) AS n_rows,
                   MIN(ts_ms) AS first_ts, MAX(ts_ms) AS last_ts,
                   COUNT(DISTINCT market_ticker) AS n_strikes
            FROM polls GROUP BY event_ticker ORDER BY MIN(ts_ms) DESC
            """
        ).fetchall()
    return JSONResponse({"events": [dict(r) for r in rows]})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8000")))
    p.add_argument(
        "--no-engine",
        action="store_true",
        help="Don't spawn the polling engine; act as pure HTTP over the DB. "
             "Use when trade.py is already running and writing polls.",
    )
    args = p.parse_args()
    if args.no_engine:
        os.environ["DASHBOARD_NO_ENGINE"] = "1"
    uvicorn.run("dashboard:app", host=args.host, port=args.port, log_level="info")
