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
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.db import PollRow
from src.engine import LEGACY_POLICY, SidePolicy, actionable_edge
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


_EVENT_RE = re.compile(r"^KX(?:ETH|BTC)D-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")


def _format_strike(market_ticker: str) -> str:
    """KXETHD-26MAY0610-T2409.99 → 'above $2,410'."""
    last = market_ticker.rsplit("-", 1)[-1]
    if not last.startswith("T"):
        return last
    try:
        n = float(last[1:])
    except ValueError:
        return last
    return f"above ${math.ceil(n):,d}"


def _format_event(event_ticker: str) -> str:
    """KXETHD-26MAY0610 → 'MAY 06 10 AM ET'."""
    m = _EVENT_RE.match(event_ticker or "")
    if not m:
        return event_ticker or "—"
    h = int(m.group(4))
    h12 = (h % 12) or 12
    ampm = "AM" if h < 12 else "PM"
    return f"{m.group(2)} {m.group(3)} {h12} {ampm} ET"

# ---- HARDCODED GUARDS — DO NOT MOVE TO CONFIG ----
# Per-bot risk knobs live in BOT_PROFILES below. Anything that varies between
# bots (notional cap, edge floor, kill-file, COID prefix) is profile-scoped.
# Anything that should never differ between bots stays module-level here.
MAX_CONTRACTS_PER_ORDER = 20          # safety backstop; edge scaling + notional cap bind first
MAX_CONTRACTS_PER_STRIKE = 10
MAX_CONTRACTS_PER_EVENT = 20          # same-side cap across the strike ladder of one event.
                                      # Strikes within an event are 100% correlated to the same
                                      # final spot — a directional thesis stacked across N strikes
                                      # is one bet, not N.
MAX_ORDERS_PER_MINUTE = 4
MAX_ORDERS_PER_POLL = 3           # PR #6: place up to K candidates per poll cycle
                                  # (single-poll cap; rate limit and notional cap
                                  # still bind across polls). At K>1 we mutate a
                                  # working copy of the snapshot between tickets
                                  # so subsequent candidates see post-prior-fill
                                  # notional + per-strike held counts.
MIN_MINUTES_TO_CLOSE = 0.25       # 15s execution buffer; the path-dependent
                                  # pricer specifically models T<W (averaging-window
                                  # variance collapse + realized-portion lock-in),
                                  # so the cushion only needs to cover order-placement
                                  # latency, not pricer bias.
ORDER_TTL_SECONDS = 3.0           # Native IOC: every BUY is priced AT the ask
                                  # to take, but if the ask moved during placement
                                  # latency the order ends up resting and gets
                                  # adverse-selected (48h data: maker fills lost
                                  # money, taker fills made +83% ROI). After this
                                  # delay we cancel anything still resting.
FULL_CONVICTION_EDGE_CENTS = 10.0 # log scaling saturates here; edges above this
                                  # deploy 100% of available notional budget.

# Hard ceilings. Profile values are validated against these at import time;
# a typo can never unlock more risk than the ceiling allows.
_MAX_NOTIONAL_CEILING_PCT = 1.0       # no profile can exceed 100% of portfolio
_MAX_DAILY_LOSS_CEILING_PCT = 0.50    # no profile can lose >50% of portfolio/day
_HARD_NOTIONAL_CEILING_USD = 100.0    # backstop: balance-fetch bug can't unlock infinite
_HARD_DAILY_LOSS_CEILING_USD = 50.0
_MIN_EDGE_FLOOR_CENTS = 2.0           # below this, fee + spread will eat the edge
_BALANCE_CACHE_TTL_S = 30.0           # don't spam Kalshi in fast-poll (3s)
_DRY_RUN_PORTFOLIO_USD = 1000.0       # synthetic balance for dry-run / tests


@dataclass(frozen=True)
class BotProfile:
    bot_id: str                   # tag written to intended_orders.bot_id
    coid_prefix: str              # 5-char prefix on client_order_id
    max_notional_pct: float       # fraction of portfolio balance
    max_daily_loss_pct: float     # fraction of portfolio balance
    min_edge_cents: float         # NET-of-fee edge floor (see engine.actionable_edge)
    kill_file: Path
    policy: SidePolicy = LEGACY_POLICY  # which sides may be entered/closed


BOT_PROFILES: dict[str, BotProfile] = {
    "selective": BotProfile(
        bot_id="eth-selective",
        coid_prefix="ethp-",      # legacy prefix; do not change (existing live history)
        # 2026-05-09 rebalance (post-merge with PR #1 risk-tightening):
        # max_notional_pct nudged 0.15 → 0.28 (midpoint between the conservative
        # post-loss tightening at 0.15 and the analysis-driven target of 0.40).
        # ETH BUY_NO is the only bot bucket where the shadow signal cleanly
        # translates to realized P&L (+6c/contract predicted = +6c realized in
        # the 147K-poll alpha scan), so it gets the larger relative bump:
        # ETH share of bot capital rises from ~50% to ~64%. Daily-loss cap
        # stays at the tightened 0.05. Reassess after 2 weeks of fresh fills.
        max_notional_pct=0.28,
        max_daily_loss_pct=0.05,
        min_edge_cents=3.0,
        kill_file=Path(".kill"),  # legacy
        policy=SidePolicy(
            allow_buy_yes=False,
            allow_buy_no=True,
            sell_yes_to_close_only=True,
        ),
    ),
    "aggressive": BotProfile(
        bot_id="eth-aggressive",
        coid_prefix="etha-",
        max_notional_pct=0.15,
        max_daily_loss_pct=0.05,
        min_edge_cents=3.0,
        kill_file=Path(".kill.aggressive"),
        policy=LEGACY_POLICY,
    ),
}

for _name, _p in BOT_PROFILES.items():
    if _p.max_notional_pct > _MAX_NOTIONAL_CEILING_PCT:
        raise ValueError(
            f"profile {_name!r} max_notional_pct={_p.max_notional_pct} "
            f"exceeds ceiling {_MAX_NOTIONAL_CEILING_PCT}"
        )
    if _p.max_daily_loss_pct > _MAX_DAILY_LOSS_CEILING_PCT:
        raise ValueError(
            f"profile {_name!r} max_daily_loss_pct={_p.max_daily_loss_pct} "
            f"exceeds ceiling {_MAX_DAILY_LOSS_CEILING_PCT}"
        )
    if _p.min_edge_cents < _MIN_EDGE_FLOOR_CENTS:
        raise ValueError(
            f"profile {_name!r} min_edge_cents={_p.min_edge_cents} "
            f"below floor {_MIN_EDGE_FLOOR_CENTS}"
        )

MIN_EDGE_CENTS = BOT_PROFILES["selective"].min_edge_cents
KILL_FILE = BOT_PROFILES["selective"].kill_file
# --------------------------------------------------


@dataclass
class OrderTicket:
    market_ticker: str
    event_ticker: str
    side: str                  # 'yes' | 'no'  (BUY_NO is the SELL-edge expression — see PR #4)
    action: str                # 'buy' | 'sell'
    limit_price_cents: int     # 1..99
    count: int
    model_prob: float
    edge_cents: float
    minutes_left: float
    spot: float
    coid_prefix: str = "ethp-"

    @property
    def notional_usd(self) -> float:
        return (self.limit_price_cents / 100.0) * self.count

    @property
    def client_order_id(self) -> str:
        return f"{self.coid_prefix}{uuid.uuid4().hex[:24]}"


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
        profile: BotProfile | None = None,
        portfolio_usd: float | None = None,
    ) -> None:
        self.conn = conn
        self.trader = trader
        self.live = live
        self.notifier = notifier
        self.profile = profile or BOT_PROFILES["selective"]
        self._order_times: collections.deque[float] = collections.deque(maxlen=MAX_ORDERS_PER_MINUTE)
        self._override_portfolio_usd = portfolio_usd
        self._cached_portfolio_usd: float | None = None
        self._cached_portfolio_ts: float = 0.0

    def _get_portfolio_usd(self) -> float:
        if self._override_portfolio_usd is not None:
            return self._override_portfolio_usd
        if self.trader is None:
            return _DRY_RUN_PORTFOLIO_USD
        now = time.time()
        if (
            self._cached_portfolio_usd is not None
            and now - self._cached_portfolio_ts < _BALANCE_CACHE_TTL_S
        ):
            return self._cached_portfolio_usd
        try:
            bal = self.trader.get_balance()
            total = (bal.get("balance", 0) + bal.get("portfolio_value", 0)) / 100.0
            self._cached_portfolio_usd = total
            self._cached_portfolio_ts = now
            log.debug("portfolio balance: $%.2f", total)
            return total
        except Exception:
            log.warning("balance fetch failed, using cached value")
            if self._cached_portfolio_usd is not None:
                return self._cached_portfolio_usd
            return _DRY_RUN_PORTFOLIO_USD

    def handle_poll(self, rows: list[PollRow]) -> Decision:
        # Guard: kill flag
        if self.profile.kill_file.exists():
            return Decision(False, f"kill file present: {self.profile.kill_file}")

        # Guard: rate limit (across all polls, not just this one)
        now = time.time()
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) >= MAX_ORDERS_PER_MINUTE:
            return Decision(False, "rate limit: >=4 orders in last 60s")

        # Snapshot first — this is the source for every per-order risk check
        # below. Fail-closed if we can't read it: in live mode we'd rather
        # miss a trade than open uncapped exposure on stale state.
        snap = snapshot(self.conn, self.trader, bot_coid_prefix=self.profile.coid_prefix)
        if snap is None:
            return Decision(False, "cannot read positions from Kalshi (fail-closed)")

        # Compute effective dollar caps from portfolio balance × profile pct.
        portfolio_usd = self._get_portfolio_usd()
        max_notional = min(
            self.profile.max_notional_pct * portfolio_usd,
            _HARD_NOTIONAL_CEILING_USD,
        )
        max_daily_loss = min(
            self.profile.max_daily_loss_pct * portfolio_usd,
            _HARD_DAILY_LOSS_CEILING_USD,
        )

        # Guard: daily loss
        if snap.total_loss_today_usd() >= max_daily_loss:
            return Decision(
                False,
                f"daily loss limit hit: ${snap.total_loss_today_usd():.2f} "
                f">= ${max_daily_loss:.2f} ({self.profile.max_daily_loss_pct:.0%} of ${portfolio_usd:.0f})",
            )

        # Guard: time to close. Runs before the candidate scan so we don't
        # waste cycles on a poll we won't act on, and so the message doesn't
        # get masked by the engine's stricter BUY gate (which would otherwise
        # return "no rows above min_edge_cents" for every too-close-to-settle
        # row, hiding the time-based reason).
        if rows and rows[0].minutes_left < MIN_MINUTES_TO_CLOSE:
            return Decision(
                False,
                f"too close to settle: T-{rows[0].minutes_left:.1f}min < {MIN_MINUTES_TO_CLOSE}min",
            )

        # Find best candidate. Policy (per-bot) decides which sides are even
        # eligible — engine display + shadow logging still see the raw legacy
        # view via actionable_edge()'s default arg, so dashboards aren't
        # distorted by per-bot config.
        candidates: list[tuple[float, PollRow, str, float]] = []
        for r in rows:
            side, edge = actionable_edge(r, self.profile.policy)
            if side == "NONE" or edge < self.profile.min_edge_cents:
                continue
            candidates.append((edge, r, side, edge))
        if not candidates:
            return Decision(False, f"no rows above min_edge_cents={self.profile.min_edge_cents}")
        candidates.sort(key=lambda x: -x[0])

        # Top-K loop: place up to MAX_ORDERS_PER_POLL tickets, mutating a working
        # snap copy so each subsequent _build_ticket sees the running notional /
        # per-strike held counts including any tickets we already placed this
        # poll. The deepcopy isolates us from snap's caller (don't mutate the
        # real positions snapshot — it's reused by callers further up).
        import copy
        working_snap = copy.deepcopy(snap)
        placed_tickets: list[OrderTicket] = []
        last_decision: Decision | None = None
        for _, row, side, edge in candidates:
            if len(placed_tickets) >= MAX_ORDERS_PER_POLL:
                break
            # Re-check rate limit between placements — _place appends to
            # _order_times each time so the same MAX_ORDERS_PER_MINUTE that
            # gates across polls also gates within a single high-K poll.
            now = time.time()
            while self._order_times and now - self._order_times[0] > 60:
                self._order_times.popleft()
            if len(self._order_times) >= MAX_ORDERS_PER_MINUTE:
                break

            ticket = self._build_ticket(row, side, edge, working_snap, max_notional)
            if ticket is None:
                continue
            decision = self._place(ticket)
            last_decision = decision
            if not decision.placed:
                continue
            placed_tickets.append(ticket)

            # Update working snap so the NEXT candidate's caps reflect this
            # placement. Buys add to notional + held; sells reduce held only
            # (notional was committed when the position was opened).
            key = (ticket.market_ticker, ticket.side)
            cur_held = working_snap.open_contracts_by_market.get(key, 0)
            if ticket.action == "buy":
                working_snap.open_notional_usd += ticket.notional_usd
                working_snap.open_contracts_by_market[key] = cur_held + ticket.count
            else:  # sell
                working_snap.open_contracts_by_market[key] = max(
                    0, cur_held - ticket.count
                )

        if placed_tickets:
            n = len(placed_tickets)
            return Decision(
                True,
                "dry_run" if not self.live else f"placed {n} ticket(s)",
                placed_tickets[-1],
            )
        if last_decision is not None:
            return last_decision
        return Decision(False, "all candidates blocked by per-order guards")

    def _build_ticket(
        self,
        row: PollRow,
        side_label: str,
        edge: float,
        snap,
        max_notional_usd: float,
    ) -> OrderTicket | None:
        # side_label is from actionable_edge. SELL_YES needs an existing long
        # (closing only). BUY_NO is the canonical fresh-short entry — Kalshi's
        # API books NO natively (kalshi_trader.place_order side='no'), so we
        # don't need to synthesize anything from yes_bid.
        if side_label == "BUY_YES":
            action = "buy"
            ticket_side = "yes"
            if row.yes_ask is None:
                return None
            limit_cents = int(round(row.yes_ask * 100))
        elif side_label == "BUY_NO":
            action = "buy"
            ticket_side = "no"
            if row.no_ask is None:
                return None
            limit_cents = int(round(row.no_ask * 100))
        elif side_label == "SELL_YES":
            action = "sell"
            ticket_side = "yes"
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

        # Per-strike concentration cap. Tracked per (market, side) — a YES
        # holding and a NO holding in the same market each consume their own
        # slot. They're economically opposite, but they each take capital and
        # we don't want to silently double-stack on either side.
        held_same_side = snap.open_contracts_by_market.get(
            (row.market_ticker, ticket_side), 0
        )
        room_in_strike = (
            MAX_CONTRACTS_PER_STRIKE - held_same_side if action == "buy" else held_same_side
        )
        if room_in_strike <= 0:
            return None

        # Per-event concentration cap.
        held_in_event_same_side = sum(
            n for (mt, sd), n in snap.open_contracts_by_market.items()
            if sd == ticket_side and mt.startswith(row.event_ticker + "-")
        )
        room_in_event = MAX_CONTRACTS_PER_EVENT - held_in_event_same_side
        if action == "sell":
            room_in_event = held_same_side
        if room_in_event <= 0:
            return None

        # Per-order size cap.
        max_count = min(MAX_CONTRACTS_PER_ORDER, room_in_strike, room_in_event)

        # Notional cap + log-proportional edge sizing (buys only).
        if action == "buy":
            remaining_notional = max_notional_usd - snap.open_notional_usd
            cost_per_contract = limit_cents / 100.0
            if cost_per_contract <= 0:
                return None
            max_by_notional = math.floor(remaining_notional / cost_per_contract)
            edge_ratio = min(
                math.log(1 + edge) / math.log(1 + FULL_CONVICTION_EDGE_CENTS),
                1.0,
            )
            edge_scaled = math.floor(edge_ratio * max_by_notional)
            if max_by_notional >= 1:
                edge_scaled = max(1, edge_scaled)
            max_count = min(max_count, edge_scaled)

        if max_count < 1:
            return None

        # `edge` is already net of fees (see engine.actionable_edge).
        return OrderTicket(
            market_ticker=row.market_ticker,
            event_ticker=row.event_ticker,
            side=ticket_side,
            action=action,
            limit_price_cents=limit_cents,
            count=int(max_count),
            model_prob=row.model_prob,
            edge_cents=edge,
            minutes_left=row.minutes_left,
            spot=row.spot,
            coid_prefix=self.profile.coid_prefix,
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

        # Write the intent BEFORE the POST so a Kalshi-accepted order can never
        # exist without a local row backing it. The snapshot's in-flight overlay
        # treats status='pending' as live exposure (BUY only), so any new BUY
        # decided in this same instant cannot exceed the cap. After the POST
        # returns we promote the row to 'submitted' (or 'error').
        intent_id = self._record_intent(
            ts_ms, mode, ticket, coid, status="pending", response=None,
        )
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
            self._update_intent(
                intent_id, status="error", response=None, reject=str(e)[:500],
            )
            log.exception("order failed: %s", e)
            return Decision(False, f"order error: {e}")

        order_id = (resp.get("order") or {}).get("order_id") or resp.get("order_id")
        self._update_intent(
            intent_id, status="submitted", response=resp, order_id=order_id,
        )
        log.info(
            "[LIVE] placed: %s %s %d @ %d¢ on %s  → order_id=%s",
            ticket.action.upper(), ticket.side, ticket.count, ticket.limit_price_cents,
            ticket.market_ticker, order_id,
        )
        self._notify(ticket, mode="LIVE", status=f"submitted (order_id={order_id})")
        self._order_times.append(time.time())
        if order_id:
            self._schedule_cancel(order_id, delay_s=ORDER_TTL_SECONDS)
        return Decision(True, "submitted", ticket)

    def _schedule_cancel(self, order_id: str, *, delay_s: float) -> None:
        """Cancel a (potentially resting) order after `delay_s` seconds.

        Every BUY is priced at the current ask intending to take. If the ask
        moved up during placement latency the order ends up resting at our
        original price — and the next person to hit it is the one with newer
        information than us. The 48h sample showed maker fills lost money
        net (-$0.85) while taker fills made +$27. Killing anything still
        resting after a few seconds collapses the adverse-selection window.

        404 from Kalshi means the order is already terminal (filled, expired,
        or already cancelled). That's the desired outcome — no-op."""
        if not self.live or self.trader is None:
            return
        def _go() -> None:
            time.sleep(delay_s)
            try:
                self.trader.cancel_order(order_id)
                log.info("[CANCEL] post-place TTL cancel: %s after %.1fs", order_id, delay_s)
            except httpx.HTTPStatusError as e:
                if e.response is not None and e.response.status_code == 404:
                    log.debug("[CANCEL] %s already terminal (404)", order_id)
                else:
                    log.warning("[CANCEL] %s failed: %s", order_id, e)
            except Exception as e:
                log.warning("[CANCEL] %s exception: %s", order_id, e)
        threading.Thread(target=_go, daemon=True, name=f"cancel-{order_id[:8]}").start()

    def _notify(self, ticket: OrderTicket, *, mode: str, status: str) -> None:
        if self.notifier is None or not self.notifier.enabled:
            return
        # Telegram Markdown: any interpolated string that could contain _, *,
        # `, or [ must be escaped or the whole message gets rejected with a 400.
        # The order_id (uuid with underscores) used to silently open an
        # unclosed italic and break every alert — it now lives in the log,
        # not the user-facing message.
        bot_md = _md_escape(self.profile.bot_id)
        # Drop "(order_id=...)" suffix from status for display; it's noise.
        status_clean = status.split(" (order_id=", 1)[0]
        status_md = _md_escape(status_clean)
        strike_md = _md_escape(_format_strike(ticket.market_ticker))
        event_md = _md_escape(_format_event(ticket.event_ticker))
        if ticket.action == "buy":
            side_word = "BUY NO" if ticket.side == "no" else "BUY YES"
        else:
            side_word = "SELL NO" if ticket.side == "no" else "SELL YES"
        msg = (
            f"*[{mode} {bot_md}] {side_word}*  "
            f"{ticket.count} × {ticket.limit_price_cents}¢  (${ticket.notional_usd:.2f})\n"
            f"{strike_md} · {event_md}\n"
            f"spot ${ticket.spot:,.0f} · T-{ticket.minutes_left:.0f}min · "
            f"model {ticket.model_prob*100:.1f}¢ · edge +{ticket.edge_cents:.1f}¢\n"
            f"_{status_md}_"
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
    ) -> int:
        """Insert a fresh intent row. Returns the row id for later updates."""
        import json
        cur = self.conn.execute(
            """
            INSERT INTO intended_orders (
                ts_ms, mode, event_ticker, market_ticker, side, action,
                limit_price_cents, count, notional_usd, model_prob, edge_cents,
                minutes_left, spot, client_order_id, status, reject_reason,
                kalshi_order_id, raw_response, bot_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_ms, mode, ticket.event_ticker, ticket.market_ticker, ticket.side,
                ticket.action, ticket.limit_price_cents, ticket.count, ticket.notional_usd,
                ticket.model_prob, ticket.edge_cents, ticket.minutes_left, ticket.spot,
                coid, status, reject, order_id,
                json.dumps(response) if response is not None else None,
                self.profile.bot_id,
            ),
        )
        return cur.lastrowid

    def _update_intent(
        self,
        intent_id: int,
        *,
        status: str,
        response: dict | None,
        order_id: str | None = None,
        reject: str | None = None,
    ) -> None:
        """Promote a 'pending' intent row in place after the POST returns."""
        import json
        self.conn.execute(
            """
            UPDATE intended_orders
            SET status = ?, kalshi_order_id = ?, raw_response = ?, reject_reason = ?
            WHERE id = ?
            """,
            (
                status,
                order_id,
                json.dumps(response) if response is not None else None,
                reject,
                intent_id,
            ),
        )
