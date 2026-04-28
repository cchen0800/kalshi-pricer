# kalshi-pricer

Logging-only auto-pricer + live dashboard for Kalshi BTC hourly markets
(`KXBTCD`). Polls Kalshi + Coinbase, prices each strike with a zero-drift
lognormal, logs every strike+poll to SQLite, and shows you where the model
disagrees with the order book.

**No order placement.** The Kalshi client only exposes GET helpers and asserts
the HTTP method is `GET` before any request goes out.

**👉 For the workflow walk-through, read [GUIDE.md](GUIDE.md).** This README
is just the technical reference.

## Settlement source caveat

`KXBTCD` markets settle against the simple average of **CF Benchmarks' Bitcoin
Real-Time Index (BRTI)** over the final 60 seconds before close. The BRTI is a
basket across Coinbase, Bitstamp, Kraken, LMAX, Gemini, and itBit (subject to
CF revisions). This pricer uses **Coinbase BTC-USD as a BRTI proxy** because
real-time BRTI requires a CF Benchmarks subscription. Coinbase tracks BRTI
tightly under normal conditions but can drift several dollars during fast
moves or exchange-specific events. Treat any apparent edge < ~$10 in BTC terms
as potentially basis noise rather than mispricing.

## Setup

```bash
# 1. Python 3.11+ recommended (works on 3.9 but the spec calls for 3.11+).
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Generate an API key in the Kalshi web app (Account → API Keys → Create key).
#    Save the .pem file your browser downloads into .secrets/ (Kalshi only shows
#    the private key once).
mkdir -p .secrets && chmod 700 .secrets
mv ~/Downloads/your-kalshi-key.txt .secrets/kalshi_private_key.pem
chmod 600 .secrets/kalshi_private_key.pem

# 3. Copy the Key ID (shown under the key name in the Kalshi dashboard) into .env.
cp .env.example .env
# edit .env → set KALSHI_KEY_ID=<the-uuid>
```

## Running

```bash
# Sanity-check auth and inspect the nearest hourly:
python scripts/verify_kalshi.py

# Live dashboard (engine + UI in one process). Open http://127.0.0.1:8000
python dashboard.py

# Or, headless (engine only, no HTTP server):
python main.py
```

Both commands poll every `poll_interval_seconds` (default 30s) and write every
strike to `pricer.db`. The headless version prints flagged rows to stdout; the
dashboard renders them in the browser with auto-refresh and color coding.

## Output schema

`pricer.db` is a single SQLite file. One row per (poll, strike):

| column          | meaning                                                |
|-----------------|--------------------------------------------------------|
| `ts_ms`         | poll time, unix milliseconds                           |
| `event_ticker`  | hourly event, e.g. `KXBTCD-26APR2623`                  |
| `market_ticker` | strike-level ticker, e.g. `KXBTCD-26APR2623-T79299.99` |
| `strike`        | the strike (BRTI must be > strike for YES)             |
| `spot`          | Coinbase last trade at poll time                       |
| `sigma`         | annualized realized vol from last 60×1m closes         |
| `minutes_left`  | minutes until the event close                          |
| `model_prob`    | P(BTC > strike at close) under zero-drift lognormal    |
| `yes_bid`       | best YES bid in dollars (0–1)                          |
| `yes_ask`       | best YES ask in dollars (0–1)                          |
| `yes_bid_size`  | depth at best bid                                      |
| `yes_ask_size`  | depth at best ask                                      |
| `volume`        | market lifetime volume                                 |
| `edge_cents`    | `model_prob*100 − mid*100` (signed; +ve = model > mid) |
| `proxy_source`  | `'coinbase'` (the BRTI proxy we used)                  |

## Tests

```bash
python -m pytest tests/ -v
```

## HTTP endpoints (dashboard)

| route | purpose |
|---|---|
| `GET /`                              | dashboard HTML |
| `GET /api/state`                     | latest poll snapshot (header + all strikes) |
| `GET /api/history/{market_ticker}`   | last N polls for one strike (default 120) |
| `GET /api/events`                    | events seen in the DB with row counts |

## Layout

```
kalshi-pricer/
  src/
    pricer.py          # zero-drift lognormal P(S_T > K)
    kalshi_client.py   # RSA-PSS-SHA256 signed GETs only
    btc_feed.py        # Coinbase spot + 1m candles
    vol.py             # realized vol estimator
    db.py              # SQLite schema + writes
    engine.py          # main 30s poll loop
  tests/               # pytest
  scripts/
    verify_kalshi.py   # one-shot auth + market inspection
  templates/
    dashboard.html     # single-page UI, vanilla JS, auto-refresh
  config.yaml
  .env                 # gitignored; KALSHI_KEY_ID + path to .pem
  .secrets/            # gitignored; the .pem lives here, chmod 600
  main.py              # headless engine entrypoint
  dashboard.py         # FastAPI + engine-in-thread entrypoint
  GUIDE.md             # workflow walkthrough
```
