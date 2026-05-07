# Codebase Review: Alpha Drains and Logic Flaws

After thoroughly reviewing the codebase, particularly the pricing model, execution logic, and position tracking, I've identified four major issues affecting your trading alpha and backend stability. Two of these are critical logic flaws that will cause the bot to either bleed capital limits or completely miss the very alpha it was designed to capture.

## 1. Capital Leak in `open_notional_usd` When Selling (Critical)
**Location:** `src/positions.py` in `snapshot()`

**The Flaw:** When you sell an existing long position, the `open_notional_usd` (which tracks how much of your $30 budget is currently tied up) is reduced by the *sale price* of the contract, not the *cost basis* you originally paid.

```python
# In src/positions.py
key = (market, side)
signed_count = count if action == "buy" else -count
open_by_market[key] = open_by_market.get(key, 0) + signed_count
open_notional_usd += (limit_cents / 100.0) * signed_count
```

**Impact on Alpha:** 
If you buy 10 contracts at 50¢ ($5.00 notional) and the market moves against you so you sell them at 20¢, you receive $2.00 back. You now have **0 open contracts**, but your `open_notional_usd` only drops by $2.00. 
The bot thinks you still have **$3.00** tied up in open positions. If you do this a few times, your "open notional" will hit the `MAX_NOTIONAL_USD = 30.0` ceiling despite having no actual open positions, and the bot will **permanently stop trading** for the day until the markets settle.

**Fix:** `open_notional_usd` should only be the sum of the capital tied up in the *remaining* open contracts. You should calculate the average entry price for the longs and use that, or compute the net cash outlay per market and bound it by `open_contracts`.

## 2. Path-Dependent Pricer is Blocked by Execution Cutoff (Massive Alpha Drain)
**Location:** `src/executor.py` (`MIN_MINUTES_TO_CLOSE`) vs `src/pricer.py` vs `src/realized.py`

**The Flaw:** You have an incredibly sophisticated `prob_above_strike_path_dependent` pricer and a `RealizedAverager` designed to perfectly price the contract during the final 60 seconds (the Kalshi BRTI averaging window). It accurately models the variance collapse and the locked-in realized portion.
However, in `src/executor.py`, you have a hardcoded guard:
```python
MIN_MINUTES_TO_CLOSE = 2.0
```
```python
if rows and rows[0].minutes_left < MIN_MINUTES_TO_CLOSE:
    return Decision(False, "too close to settle...")
```

**Impact on Alpha:** 
Because the bot stops trading **2 minutes** before settlement, it will *never* place a trade during the final 60 seconds. The entire `T < W` code path in your path-dependent pricer—which is likely where your largest statistical edge exists—is essentially dead code and never capitalized on.

**Fix:** If you want to exploit the edge inside the averaging window, you need to lower `MIN_MINUTES_TO_CLOSE` to something like `0.0` or `0.5`.

## 3. Potential `fee_cost` Unit Mismatch (Kill-Switch Risk)
**Location:** `src/positions.py` in `snapshot()`

**The Flaw:** When calculating today's realized PnL from Kalshi settlements, the code does:
```python
revenue_usd = _f(sett.get("revenue", 0)) / 100.0
cost_usd = _f(sett.get("yes_total_cost_dollars")) + _f(sett.get("no_total_cost_dollars"))
fee_usd = _f(sett.get("fee_cost"))
settled_pnl_by_market[market] = revenue_usd - cost_usd - fee_usd
```
It correctly divides `revenue` by 100 (since Kalshi v2 returns revenue in cents), but treats `fee_cost` as dollars. While the mock tests in `test_positions.py` use `"fee_cost": "0.05"`, Kalshi's API often returns all monetary values (including fees) in cents. 

**Impact on Alpha:** If the real Kalshi API returns `fee_cost: 5` (cents), the code will interpret this as a **$5.00 fee**. Your `realized_pnl_today_usd` will plummet artificially, and you will hit the `MAX_DAILY_LOSS_USD` kill-switch almost instantly after your first few settlements.

**Fix:** Verify Kalshi's actual API response for `/portfolio/settlements` in production. If `fee_cost` is in cents, divide it by 100.0.

## 4. Arithmetic vs Geometric Brownian Motion in Final Minutes
**Location:** `src/pricer.py`
The path-dependent pricer uses a Normal distribution (Bachelier/Arithmetic Brownian Motion) rather than Lognormal:
`return float(norm.cdf((mean - strike) / sd))`
The docstring rightly notes: *"exact to leading order at BTC/short-window scales"*. This is mathematically sound for very short timeframes and is actually a great design choice to avoid the complexity of lognormal integrals of averages, but keep in mind that if the bot is running during extremely volatile spikes (e.g., >5% moves in 5 minutes), the approximation error increases, slightly mispricing deep out-of-the-money options.

## 5. Basis Risk via Coinbase Proxy
**Location:** `src/btc_feed.py`
You are using Coinbase spot to proxy the CME CF BRTI index. During high-volatility events, Coinbase often leads or lags the blended BRTI index due to liquidity imbalances. This latency arbitrage basis can temporarily invert your edge. (This is known per your README, but is an unavoidable alpha leak unless you pull the exact CF Benchmarks feed).
