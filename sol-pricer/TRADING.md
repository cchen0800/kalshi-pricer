# Trading layer

This doc explains the live-trading add-on (`trade.py` + `src/executor.py` +
`src/kalshi_trader.py`) layered on top of the read-only pricer. If you only
want to log signals, use `dashboard.py` / `main.py` instead.

> **Read this whole doc before running `--live`.** Every guard in this system
> exists because something would otherwise go wrong with real money.

---

## TL;DR

```bash
# 1. One-time Telegram setup (optional but recommended)
#    a. Create a bot via @BotFather, paste token into .env
#    b. Send the bot any message in Telegram
#    c. Run:
python scripts/telegram_setup.py

# 2. Default mode is dry-run. Same code path, no POSTs:
python trade.py --dry-run

# 3. When ready, flip to live (you must type a confirmation phrase):
python trade.py --live

# Kill switch (works in both modes — checked before each order):
touch .kill
```

---

## How the trade decision is made

### Step 1: model price for each strike

Every 30 seconds the engine fetches:

- The full strike ladder for the nearest open `KXSOLD` hourly event from Kalshi
  (typically 100–200 strikes spanning roughly ±2% around spot)
- Coinbase SOL-USD spot (proxy for the SRTI settlement index — see caveat below)
- The last 60 one-minute Coinbase candles, used to compute realized vol

For each strike `K`, the model computes

```
P(BTC > K at close) = Φ( (ln(spot/K) - 0.5·σ²·T) / (σ·√T) )
```

This is a **zero-drift lognormal** — appropriate for short-dated BTC where
expected drift over an hour is tiny relative to vol. Implemented in
`src/pricer.py:25`.

### Step 2: actionable edge per strike

For each strike, we compute two edges in cents:

```
buy_edge  = model_prob × 100  −  yes_ask_cents     # YES looks too cheap
sell_edge = yes_bid_cents     −  model_prob × 100  # YES looks too expensive
```

`actionable_edge()` (`src/engine.py:125`) picks whichever is larger and
positive, returning `('BUY_YES', edge)`, `('SELL_YES', edge)`, or
`('NONE', 0)`.

**Why `bid`/`ask` and not the mid?** Because we have to actually trade against
the book. A 5¢ mid edge with a 4¢ spread is 1¢ of real edge to a taker, not 5¢.
Using the relevant top-of-book quote prices the decision against what we'd
actually fill at.

### Step 3: pick the best candidate

The executor sorts all flagged rows by descending edge and tries each one
through the guards below. The first row that passes every guard becomes the
order.

- **At most one order per 30-second poll cycle.** Even if six strikes look
  attractive, we place one.
- **Limit price = the opposite side of the book.** Buys cross the ask, sells
  cross the bid. Either we fill immediately (taker) or Kalshi rejects /
  rests — we never sit at fair value hoping for a fill.

---

## The guards (hardcoded in `src/executor.py:32`)

These are not in `config.yaml` on purpose. A typo in YAML must not be able to
unlock more risk. To raise a limit, you have to edit Python.

| Guard | Value | Why |
|---|---|---|
| `MAX_NOTIONAL_USD` | `30.0` | Total $ tied up in open positions. Hits → stop opening new buys. |
| `MAX_DAILY_LOSS_USD` | `30.0` | Realized loss kill switch (today, ET). Hits → halt all orders. |
| `MAX_CONTRACTS_PER_ORDER` | `5` | Per-ticket size cap. Bounds blow-up from a single order. |
| `MAX_CONTRACTS_PER_STRIKE` | `10` | Per-strike concentration cap. Avoids piling everything into one strike. |
| `MIN_EDGE_CENTS` | `15.0` | Minimum actionable edge to act. Must clear: Kalshi taker fee (~2¢), spread (1–2¢), and SRTI/Coinbase basis noise (~5–10¢ in BTC terms). |
| `MAX_ORDERS_PER_MINUTE` | `4` | Rate limit on ourselves. Catches runaway loops; avoids tripping Kalshi's own rate limits. |
| `MIN_MINUTES_TO_CLOSE` | `5.0` | Stop trading 5 min before settle. Reason: the SRTI averages over the final 60s, and Coinbase can drift several dollars from SRTI in that window — the model is least reliable then. |
| `KILL_FILE = .kill` | n/a | Touch the file → executor refuses next order. Quick manual halt. |

### Per-order guards (within `_build_ticket`)

- **`SELL_YES` requires an existing long.** We don't trade NO contracts and
  don't go short. If we don't already own YES on a market, sell signals are
  ignored.
- **Limit price must be 1–99¢.** Strikes with one-sided books (bid=0 or ask=0)
  are skipped.
- **Per-strike concentration** is enforced against current holdings, not just
  the new order — so 8 contracts already held + 5 new = 13 → capped to room=2.
- **Notional cap** (buys only) further reduces the count if the budget is
  tight: `count = floor(remaining_budget / price)`.
- **Post-fee edge sanity check.** After computing the Kalshi taker fee for the
  exact price, if `edge − fee < MIN_EDGE_CENTS / 2`, we skip. Belt-and-braces
  against the ~2¢ fee on a 15¢ edge.

---

## What happens when an order is placed

1. Executor builds an `OrderTicket` with strike, side, count, limit price.
2. A unique `client_order_id` is generated (UUID prefix `btcp-`) for
   idempotency.
3. **Dry-run mode:** insert a row into `intended_orders` with `status='dry_run'`,
   log to stdout, send Telegram notification (if configured), and return.
   No HTTP request to Kalshi.
4. **Live mode:** call `KalshiTrader.place_order` (POST `/portfolio/orders`),
   record the response in `intended_orders` with `status='submitted'` (or
   `'error'` on exception), and send Telegram notification.
5. The Kalshi response includes the actual `order_id`. Fills are written to
   the `fills` table when reconciled (see "Reconciliation" below).

---

## Telegram notifications

Notifications are sent on every order intent — **dry-run included** — so you
can verify the channel is working without risking money.

Format:

```
[DRY-RUN] BUY YES 5 @ 62¢
KXSOLD-26MAY0213-T78499.99
strike: $78,500 spot ; T-46.9min
model: 66.9¢  edge: +4.9¢
notional: $3.10
status: logged
```

Setup:

```bash
# 1. Talk to @BotFather → /newbot → save the token in .env
# 2. Open the bot in Telegram, send any message (e.g. /start)
# 3. Run:
python scripts/telegram_setup.py
# This auto-detects your chat ID from the bot's getUpdates feed and writes
# TELEGRAM_CHAT_ID to .env. It also sends a "setup OK" confirmation.
```

If Telegram is unreachable or the env vars are missing, notifications are a
silent no-op — they will **never** block trading.

### Remote kill switch from Telegram

When `trade.py` starts, it also launches a background thread that listens for
commands from your configured chat:

| Command | Effect |
|---|---|
| `/kill` (or `kill`, `/stop`, `halt`) | Touches `.kill` — executor refuses next order. Bot replies with confirmation. |
| `/status` | Reports whether the kill switch is currently armed. |
| `/help` | Lists the available commands. |

Messages from any other chat are ignored (only the chat ID in `.env` can
control the trader). To resume after `/kill`, you must SSH in and
`rm .kill` — by design, the asymmetry is "easy to halt, hard to resume."

---

## Mode reference

| Mode | Flag | Effect |
|---|---|---|
| Dry-run | `--dry-run` | All guards run; tickets logged to `intended_orders`; no POST. Safe to leave running. |
| Live | `--live` | Same code path; tickets posted to Kalshi. Requires confirmation phrase at start (or `--yes-i-know` to skip prompt — useful for restart scripts but not recommended). |

---

## Reconciliation (and what's not in v0)

Right now the executor tracks order *intents* and a local mirror of fills, but
**does not yet poll Kalshi for fill status or settlement** — those tables
(`fills`, `portfolio_settlements`) exist but are populated only by manual
SQL or by a reconciliation script (TBD).

What this means in practice for the $30 budget run:

- Open notional is computed from the local `fills` mirror. Until we add a
  reconciliation pass, you should manually verify positions against Kalshi's
  web UI after each order.
- The daily-loss kill switch sees only realized cash deltas in the local DB.
  If reconciliation lags, the switch can be loose.
- For a $30 ceiling this is acceptable — the per-order, per-strike, and rate
  caps ensure we can't blow up between reconciliations. A future PR should
  add `scripts/reconcile.py` that pulls fills + settlements from Kalshi and
  writes them to the local mirror.

---

## Settlement source caveat (still applies)

`KXSOLD` settles against **CF Benchmarks SRTI** averaged over the final 60s.
The SRTI is a basket of Coinbase, Bitstamp, Kraken, LMAX, Gemini, itBit. We
proxy with **Coinbase SOL-USD** because real-time SRTI requires a paid
subscription. Coinbase tracks SRTI tightly under normal conditions but can
diverge several dollars during fast moves or exchange-specific events.

That ~$5–10 BTC-terms basis is the main reason `MIN_EDGE_CENTS = 15`. A 5¢
edge in our model could be 0¢ in reality — or negative.

---

## Risk model recap

The hardcoded guards form a defense in depth:

```
input:    flagged row (any edge)
  ↓
guard:    kill flag        → halts everything until removed
guard:    rate limit       → max 4 orders/min
guard:    daily loss       → halts on $30 realized loss
filter:   edge ≥ 15¢       → skips noise
guard:    T-5min cutoff    → no new orders near settle
build:    side check       → SELL needs existing long
build:    price 1–99¢      → skips degenerate books
build:    per-strike cap   → ≤10 contracts/strike
build:    per-order cap    → ≤5 contracts/order
build:    notional cap     → ≤$30 outstanding total
build:    post-fee check   → skip if fee eats the edge
  ↓
output:   one OrderTicket, posted (live) or logged (dry-run)
```

If any single check fails, the order is skipped and the next-best candidate
is tried. If no candidate passes, the poll cycle ends with no order.

---

## Files

```
src/
  pricer.py          # zero-drift lognormal P(S_T > K)  (existing)
  engine.py          # 30s poll loop + actionable_edge  (existing)
  kalshi_trader.py   # POST-allowed Kalshi client       (NEW)
  positions.py       # local position/PnL accounting    (NEW)
  executor.py        # guards + decision + order logic  (NEW)
  notify.py          # Telegram wrapper                 (NEW)
  db.py              # schema (extended)                (modified)
  …
scripts/
  telegram_setup.py  # one-shot chat-ID detector        (NEW)
trade.py             # CLI entrypoint (--dry-run/--live)(NEW)
tests/
  test_executor.py   # guard tests, 10 cases            (NEW)
```
