"""FastAPI dashboard. Runs the engine in a background thread; serves a live
HTML page and JSON endpoints reading from the same SQLite file the engine
writes to.

Launch:
    python dashboard.py        # http://localhost:8000

Read-only. The engine inside is the same read-only one as `python main.py`.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from main import load_config
from src.btc_feed import CoinbaseFeed
from src.engine import EngineConfig, event_close_utc, run as run_engine

NY_TZ = ZoneInfo("America/New_York")


def _event_title(event_ticker: str) -> str:
    """Render a Kalshi-style human title from a KXBTCD ticker.

    `KXBTCD-26APR2802` → `Bitcoin · Apr 28, 2:00 AM EDT`.
    Falls back to the raw ticker if it can't be parsed.
    """
    close_dt = event_close_utc(event_ticker)
    if close_dt is None:
        return event_ticker
    ny = close_dt.astimezone(NY_TZ)
    hour12 = ny.hour % 12 or 12
    ampm = "AM" if ny.hour < 12 else "PM"
    return f"Bitcoin · {ny.strftime('%b')} {ny.day}, {hour12}:00 {ampm} {ny.strftime('%Z')}"

ROOT = Path(__file__).parent
TEMPLATE = ROOT / "templates" / "dashboard.html"

_state: dict = {"thread": None, "stop": None, "cfg": None}


def _engine_thread_target(cfg: EngineConfig, stop: threading.Event) -> None:
    try:
        run_engine(cfg, stop_event=stop)
    except Exception:
        import logging
        logging.getLogger("dashboard").exception("engine thread crashed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    stop = threading.Event()
    th = threading.Thread(target=_engine_thread_target, args=(cfg, stop), daemon=True)
    th.start()
    feed = CoinbaseFeed()
    _state["thread"], _state["stop"], _state["cfg"], _state["feed"] = th, stop, cfg, feed
    try:
        yield
    finally:
        stop.set()
        th.join(timeout=10)
        feed.close()


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
    uvicorn.run("dashboard:app", host="127.0.0.1", port=8000, log_level="info")
