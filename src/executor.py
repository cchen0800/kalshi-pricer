"""Trade executor.

Hardcoded risk controls. None of these are config, by design — a typo in YAML
shouldn't be able to unlock more risk. To raise a limit you have to change
this file.

Strategy (v0):
  - On each poll, look at flagged rows from the engine.
  - For each row above MIN_EDGE_CENTS, generate a YES buy or sell limit
    crossing the spread (taker). Limit price = the opposite side of the book,
    so we either fill immediately or get rejected by Kalshi.
  - Apply guards in order: kill flag, time-to-close, daily loss, open notional,
    per-strike concentration, rate limit, edge floor.
  - Place at most ONE order per poll cycle (the highest-edge one that passes).
  - Dry-run mode: log the order ticket to `intended_orders` with status
    'dry_run' and skip the POST.
"""

from __future__ import annotations

import collections
import logging
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from src.db import PollRow
from src.engine import actionable_edge
from src.kalshi_trader import KalshiTrader
from src.notify import TelegramNotifier
from src.positions import snapshot

log = logging.getLogger("executor")


def _md_escape(s: str) -> str:
    """Escape Markdown specials for Telegram's 'Markdown' parse mode.

    A bare _, *, `, or [ in a body anywhere would either start a never-closed
    entity (rejecting the whole message with a 400) or silently consume text
    until the next matching char. Escape all four defensively.
    """
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s

# ---- HARDCODED GUARDS — DO NOT MOVE TO CONFIG ----
MAX_NOTIONAL_USD = 30.0           # max $ tied up in open positions
MAX_DAILY_LOSS_USD = 30.0         # realized loss kill-switch (today, ET)
MAX_CONTRACTS_PER_ORDER = 5
MAX_CONTRACTS_PER_STRIKE = 10
MIN_EDGE_CENTS = 8.0              # NET-of-fee edge (see engine.actionable_edge).
                                  # Backtest sweep on 6d/27 events shows SELL t-stat peaks here;
                                  # raising past ~9¢ kills SELL signal entirely. BUY is saturated.
MAX_ORDERS_PER_MINUTE = 4
MIN_MINUTES_TO_CLOSE = 2.0        # path-dependent pricer is now correct in the
                                  # final minutes (averaging-window variance
                                  # collapse + realized-portion lock-in). The 5-min
                                  # cushion was tuned to the old endpoint pricer's
                                  # near-expiry bias and is no longer warranted.
KILL_FILE = Path(".kill")
# --------------------------------------------------


@dataclass
class OrderTicket:
    market_ticker: str
    event_ticker: str
    side: str                  # 'yes' (we don't trade NO in v0; sell YES instead)
    action: str                # 'buy' | 'sell'
    limit_price_cents: int     # 1..99
    count: int
    model_prob: float
    edge_cents: float
    minutes_left: float
    spot: float

    @property
    def notional_usd(self) -> float:
        return (self.limit_price_cents / 100.0) * self.count

    @property
    def client_order_id(self) -> str:
        return f"btcp-{uuid.uuid4().hex[:24]}"


@dataclass
class Decision:
    placed: bool
    reason: str
    ticket: OrderTicket | None = None


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        trader: KalshiTrader | None,
        *,
        live: bool,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.conn = conn
        self.trader = trader
        self.live = live
        self.notifier = notifier
        self._order_times: collections.deque[float] = collections.deque(maxlen=MAX_ORDERS_PER_MINUTE)

    def handle_poll(self, rows: list[PollRow]) -> Decision:
        # Guard: kill flag
        if KILL_FILE.exists():
            return Decision(False, f"kill file present: {KILL_FILE}")

        # Guard: rate limit (across all polls, not just this one)
        now = time.time()
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) >= MAX_ORDERS_PER_MINUTE:
            return Decision(False, "rate limit: >=4 orders in last 60s")

        # Snapshot first — this is the source for every per-order risk check
        # below. Fail-closed if we can't read it: in live mode we'd rather
        # miss a trade than open uncapped exposure on stale state.
        snap = snapshot(self.conn, self.trader)
        if snap is None:
            return Decision(False, "cannot read positions from Kalshi (fail-closed)")

        # Guard: daily loss
        if snap.total_loss_today_usd() >= MAX_DAILY_LOSS_USD:
            return Decision(
                False,
                f"daily loss limit hit: ${snap.total_loss_today_usd():.2f} >= ${MAX_DAILY_LOSS_USD:.2f}",
            )

        # Find best candidate.
        candidates: list[tuple[float, PollRow, str, float]] = []
        for r in rows:
            side, edge = actionable_edge(r)
            if side == "NONE" or edge < MIN_EDGE_CENTS:
                continue
            candidates.append((edge, r, side, edge))
        if not candidates:
            return Decision(False, "no rows above MIN_EDGE_CENTS")
        candidates.sort(key=lambda x: -x[0])

        # Guard: time to close (use the freshest row — they all share minutes_left).
        if rows and rows[0].minutes_left < MIN_MINUTES_TO_CLOSE:
            return Decision(
                False,
                f"too close to settle: T-{rows[0].minutes_left:.1f}min < {MIN_MINUTES_TO_CLOSE}min",
            )

        for _, row, side, edge in candidates:
            ticket = self._build_ticket(row, side, edge, snap)
            if ticket is None:
                continue
            return self._place(ticket)

        return Decision(False, "all candidates blocked by per-order guards")

    def _build_ticket(
        self,
        row: PollRow,
        side_label: str,
        edge: float,
        snap,
    ) -> OrderTicket | None:
        # side_label is from actionable_edge: 'BUY_YES' or 'SELL_YES'.
        # We always trade the YES contract; SELL_YES needs an existing long.
        if side_label == "BUY_YES":
            action = "buy"
            if row.yes_ask is None:
                return None
            limit_cents = int(round(row.yes_ask * 100))
        elif side_label == "SELL_YES":
            action = "sell"
            if row.yes_bid is None:
                return None
            limit_cents = int(round(row.yes_bid * 100))
            # Only sell what we already own.
            held = snap.open_contracts_by_market.get((row.market_ticker, "yes"), 0)
            if held <= 0:
                log.debug("skip SELL_YES on %s: no long position", row.market_ticker)
                return None
        else:
            return None

        if not (1 <= limit_cents <= 99):
            return None

        # Per-strike concentration cap.
        held = snap.open_contracts_by_market.get((row.market_ticker, "yes"), 0)
        room_in_strike = MAX_CONTRACTS_PER_STRIKE - held if action == "buy" else held
        if room_in_strike <= 0:
            return None

        # Per-order size cap.
        max_count = min(MAX_CONTRACTS_PER_ORDER, room_in_strike)

        # Notional cap (only matters for buys; sells free up notional).
        if action == "buy":
            remaining_notional = MAX_NOTIONAL_USD - snap.open_notional_usd
            cost_per_contract = limit_cents / 100.0
            if cost_per_contract <= 0:
                return None
            max_by_notional = math.floor(remaining_notional / cost_per_contract)
            max_count = min(max_count, max_by_notional)

        if max_count < 1:
            return None

        # `edge` is already net of fees (see engine.actionable_edge).
        return OrderTicket(
            market_ticker=row.market_ticker,
            event_ticker=row.event_ticker,
            side="yes",
            action=action,
            limit_price_cents=limit_cents,
            count=int(max_count),
            model_prob=row.model_prob,
            edge_cents=edge,
            minutes_left=row.minutes_left,
            spot=row.spot,
        )

    def _place(self, ticket: OrderTicket) -> Decision:
        coid = ticket.client_order_id
        ts_ms = int(time.time() * 1000)
        mode = "live" if self.live else "dry_run"

        if not self.live:
            self._record_intent(ts_ms, mode, ticket, coid, status="dry_run", response=None)
            log.info(
                "[DRY-RUN] would place: %s %s %d @ %d¢ on %s  (edge=%.1f¢, model=%.1f¢, T-%.1fmin)",
                ticket.action.upper(), ticket.side, ticket.count, ticket.limit_price_cents,
                ticket.market_ticker, ticket.edge_cents, ticket.model_prob * 100, ticket.minutes_left,
            )
            self._notify(ticket, mode="DRY-RUN", status="logged")
            self._order_times.append(time.time())
            return Decision(True, "dry_run", ticket)

        # LIVE
        assert self.trader is not None, "live=True requires trader instance"
        try:
            resp = self.trader.place_order(
                ticker=ticket.market_ticker,
                client_order_id=coid,
                side=ticket.side,
                action=ticket.action,
                count=ticket.count,
                limit_price_cents=ticket.limit_price_cents,
            )
        except Exception as e:
            self._record_intent(
                ts_ms, mode, ticket, coid, status="error", response=None, reject=str(e)[:500],
            )
            log.exception("order failed: %s", e)
            return Decision(False, f"order error: {e}")

        order_id = (resp.get("order") or {}).get("order_id") or resp.get("order_id")
        self._record_intent(
            ts_ms, mode, ticket, coid, status="submitted", response=resp, order_id=order_id,
        )
        log.info(
            "[LIVE] placed: %s %s %d @ %d¢ on %s  → order_id=%s",
            ticket.action.upper(), ticket.side, ticket.count, ticket.limit_price_cents,
            ticket.market_ticker, order_id,
        )
        self._notify(ticket, mode="LIVE", status=f"submitted (order_id={order_id})")
        self._order_times.append(time.time())
        return Decision(True, "submitted", ticket)

    def _notify(self, ticket: OrderTicket, *, mode: str, status: str) -> None:
        if self.notifier is None or not self.notifier.enabled:
            return
        # Telegram Markdown: any interpolated string that could contain _, *,
        # `, or [ must be escaped or the whole message gets rejected with a 400.
        # Previously only `market` was escaped; the `status` string contains
        # "order_id=..." whose underscore opened an unclosed italic and silently
        # killed every order alert.
        market = _md_escape(ticket.market_ticker)
        status_md = _md_escape(status)
        side_word = "BUY YES" if ticket.action == "buy" else "SELL YES"
        msg = (
            f"*[{mode}] {side_word}* {ticket.count} @ {ticket.limit_price_cents}¢\n"
            f"`{market}`\n"
            f"strike: ${ticket.spot:,.0f} spot ; T-{ticket.minutes_left:.1f}min\n"
            f"model: {ticket.model_prob*100:.1f}¢  edge: +{ticket.edge_cents:.1f}¢\n"
            f"notional: ${ticket.notional_usd:.2f}\n"
            f"status: {status_md}"
        )
        self.notifier.send(msg)

    def _record_intent(
        self,
        ts_ms: int,
        mode: str,
        ticket: OrderTicket,
        coid: str,
        *,
        status: str,
        response: dict | None,
        order_id: str | None = None,
        reject: str | None = None,
    ) -> None:
        import json
        self.conn.execute(
            """
            INSERT INTO intended_orders (
                ts_ms, mode, event_ticker, market_ticker, side, action,
                limit_price_cents, count, notional_usd, model_prob, edge_cents,
                minutes_left, spot, client_order_id, status, reject_reason,
                kalshi_order_id, raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_ms, mode, ticket.event_ticker, ticket.market_ticker, ticket.side,
                ticket.action, ticket.limit_price_cents, ticket.count, ticket.notional_usd,
                ticket.model_prob, ticket.edge_cents, ticket.minutes_left, ticket.spot,
                coid, status, reject, order_id,
                json.dumps(response) if response is not None else None,
            ),
        )
