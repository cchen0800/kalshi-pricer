# How to use kalshi-pricer

A walk-through of the workflow this tool is built for. The tool is a logger
and dashboard — it tells you where the model and the Kalshi book disagree.
**It does not place trades.** If you decide to act, you do it by hand on the
Kalshi web app.

---

## 1. One-time setup

```bash
cd kalshi-pricer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# .env should already have your KALSHI_KEY_ID and the path to your .pem.
# If not, copy .env.example → .env and fill it in.
```

Sanity check before you ever start the dashboard:

```bash
python scripts/verify_kalshi.py
```

You should see a nearest-open hourly event ticker (e.g.
`KXBTCD-26APR2623`), a strike ladder, and the settlement clause that mentions
CF Benchmarks BRTI. If you see a 4xx error, your auth is wrong — fix that
first.

## 2. Start a session

```bash
python dashboard.py
```

Then open **http://127.0.0.1:8000** in any browser.

What's happening under the hood: this single command boots a FastAPI server
*and* spawns the polling engine in a background thread. The engine writes to
`pricer.db` every 30 seconds; the dashboard reads from the same file and
auto-refreshes every 2 seconds.

You'll see a "waiting for first poll…" message for up to 30 seconds, then
the live grid takes over.

## 3. Reading the dashboard

### Header strip

| field | meaning | use it to… |
|---|---|---|
| `Event` | The Kalshi event ticker. Format: `KXBTCD-YYMMMDDHH`. | Confirm you're looking at the right hour. |
| `BTC spot` | Coinbase BTC-USD last trade. | Eyeball reasonableness vs. the strike ladder. |
| `Realized σ` | Annualized vol from the last 60 1m closes. | Big jumps = regime change; trust the model less right after. |
| `Time to close` | Minutes until trading halts (BRTI averaging starts then). | Pricing has different character at T-1h vs T-3min. |
| `Edge threshold` | What counts as "flagged" (default ±5¢). | Configure in `config.yaml`. |
| `Last poll` | Age of the data on screen. Turns yellow if >90 s. | Quick check the engine isn't dead. |

The pulse dot is green when fresh, yellow when stale.

### Strike grid

One row per strike, sorted ascending. Columns:

- **Strike** — BRTI must close *above* this number for YES to resolve YES.
- **Model %** — `P(BTC > strike at close)` under our zero-drift lognormal,
  using the current spot, σ, and minutes-left.
- **Bid / Ask / Mid** — Kalshi YES quotes, in dollars (so 0.42 = 42¢).
- **Edge ¢** — `model_prob × 100 − mid × 100`, signed.
  - Positive: model thinks YES is underpriced vs. mid.
  - Negative: model thinks YES is overpriced vs. mid.
- **Action** — what the *lift-the-market* edge would be:
  - `BUY_YES +X¢`: you'd pay the ask, model says you still net +X¢.
  - `SELL_YES +X¢`: you'd hit the bid, model says you still net +X¢.
  - Only shown when the lift edge exceeds threshold.
- **Liquidity** — bid_size + ask_size (top of book).
- **Ticker** — the full market ticker, ready to paste into Kalshi search.

Row coloring:

- **Green-tinted** = `BUY_YES` flagged.
- **Red-tinted** = `SELL_YES` flagged.
- **Bold** = within $250 of spot (the ATM band where prices are tight and
  most of the action happens).
- **Dim grey** = no quotes at all (stale or untraded strike).

## 4. The intended workflow

This is a **research-and-monitoring** tool. The high-level loop:

```
   ┌──────────────┐    ┌────────────────┐    ┌──────────────────┐
   │ Watch hour   │ →  │ See flagged    │ →  │ Decide: real     │
   │ approach     │    │ strike(s)      │    │ edge or noise?   │
   └──────────────┘    └────────────────┘    └──────────────────┘
          ↑                                            │
          │                                            ↓
   ┌──────────────┐                          ┌──────────────────┐
   │ DB grows;    │ ←────────────────────────│ Manual decision: │
   │ backtest     │                          │ trade on Kalshi  │
   │ later        │                          │ web app — or not │
   └──────────────┘                          └──────────────────┘
```

### When to actually pay attention

**Best window: T-15min to T-3min before close.**

- Earlier than 15 min: the lognormal assumption is loose, prices haven't
  converged on the right strike, both sides are just guessing.
- Later than 3 min: the BRTI 60-second average dominates, our spot-based
  pricing is wrong (you'd need to model the average-of-the-last-60-seconds,
  not just terminal price).
- The sweet spot is when:
  - Quotes are tight (1¢–3¢ wide)
  - The ATM band has clear gradients (98¢ → 2¢ across 5 strikes)
  - Realized σ has stabilized (not the first 5 min after a CPI print)

### When you see a flagged strike

Don't act on the number alone. Walk through this:

1. **Is liquidity > 100?** Sub-100 size at the touch is often a single
   resting order; lifting it leaves you alone with no fill behind.
2. **Is the strike close to spot?** Edges in the deep tails (model says
   0.001%, market says 0¢) are noise — the model is confidently saying
   "definitely no" while the market floor of 0/1¢ creates a tiny apparent
   edge that's not actionable.
3. **Is the edge persistent across polls?** A 6¢ edge on one poll that's
   gone the next poll is just bid/ask jitter. A 6¢ edge that holds for
   3+ polls (90+ seconds) is more interesting.
4. **Does the action match a story?** "Market thinks tail is fatter than
   lognormal does" is a story that holds together. "Market thinks ATM is
   higher than 50/50" might mean BTC has been trending and our zero-drift
   assumption is off.

### What to do (or not do)

This tool **never** trades. If you decide an edge is real:

- Copy the ticker from the rightmost column.
- Open Kalshi, paste, manually place a small order.
- Note the edge magnitude and ticker — you'll want to compare your fill
  to where the strike resolved later.

If you decide it's not real, do nothing. The DB still has the row, so you
can review whether your decision was right.

## 5. After a few sessions: backtesting from the DB

Once `pricer.db` has a few hours of polls, query it directly:

```bash
sqlite3 pricer.db
```

```sql
-- How many polls have I collected?
SELECT COUNT(DISTINCT ts_ms), COUNT(*) FROM polls;

-- Which events have I logged?
SELECT event_ticker, COUNT(*) AS rows,
       MIN(datetime(ts_ms/1000, 'unixepoch')) AS first_seen,
       MAX(datetime(ts_ms/1000, 'unixepoch')) AS last_seen
FROM polls GROUP BY event_ticker ORDER BY first_seen DESC;

-- All flagged opportunities I saw on a specific event:
SELECT datetime(ts_ms/1000,'unixepoch','localtime') AS t,
       strike, model_prob, yes_bid, yes_ask, edge_cents
FROM polls
WHERE event_ticker = 'KXBTCD-26APR2623'
  AND ABS(edge_cents) > 5
ORDER BY ts_ms, strike;

-- Did flagged edges revert (i.e. was the model directionally right)?
-- Compare model_prob at flag time to the last-poll mid for the same strike.
WITH flagged AS (
  SELECT market_ticker, ts_ms, model_prob, edge_cents
  FROM polls WHERE ABS(edge_cents) > 5
),
finals AS (
  SELECT market_ticker, MAX(ts_ms) AS final_ts
  FROM polls GROUP BY market_ticker
)
SELECT f.market_ticker,
       f.edge_cents AS edge_at_flag,
       p.yes_bid AS final_bid,
       p.yes_ask AS final_ask
FROM flagged f
JOIN finals fi USING (market_ticker)
JOIN polls p ON p.market_ticker = fi.market_ticker AND p.ts_ms = fi.final_ts
LIMIT 20;
```

Once you have a few days of data, graduate to a Jupyter notebook reading the
same DB — `pandas.read_sql_query` and you're off.

## 6. Pitfalls that have already burned this tool

- **First 5 minutes after launch**: realized σ uses the trailing 60 minutes,
  but the most recent minute is partial. Don't trust σ until you've been
  running for ~2 minutes.
- **The 60-second BRTI average**: at T < 3 min, the right model is "average
  of 60 correlated 1-second BRTI samples," not "terminal price." The current
  pricer overstates volatility this close to settle.
- **Coinbase ≠ BRTI**: when funding rates blow out or one exchange has an
  outage, Coinbase can drift several dollars from BRTI. A "$10" edge at
  $79k spot is 0.013% — well within typical basis noise.
- **Apparent edges in the tails**: model says 0.0001% on a $90k strike with
  spot $79k → market shows 0/1¢ → "apparent" 0.5¢ edge. Worthless.
- **Kalshi tick size at the touch**: many strikes are quoted 0.99/1.00 or
  0.00/0.01. The 1¢ floor means you can never see "true" agreement; pay
  attention to mid, not edge, in those cases.

## 7. What's intentionally not in scope (yet)

- No order placement. Read-only client.
- No CF Benchmarks subscription. Using Coinbase as BRTI proxy.
- No drift / skew / IV-blend. Pure zero-drift lognormal.
- No alerts (Slack, email). The dashboard is the alert.
- No multi-user auth. Run it on your laptop.

## 8. Stopping the app

`Ctrl-C` in the terminal running `dashboard.py` cleanly shuts down both the
HTTP server and the engine thread. The DB is closed safely; nothing in flight
gets corrupted.

If you only want headless engine logging without the UI:

```bash
python main.py
```

Same engine, no FastAPI, no port open.
