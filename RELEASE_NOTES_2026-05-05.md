# Release notes — 2026-05-05: Calibrated BUY_NO entry + top-K orders

This deploy ships PRs #1–#6 across both `kalshi-pricer` (BTC) and the
`eth-pricer` mirror. It is the largest behavior change to the bots since
the multi-profile split: it switches the selective profile away from
BUY_YES entries entirely and onto BUY_NO entries derived from the
SELL-side alpha that backtests located, with a calibrated probability,
a tightened edge floor, a tail-mp band gate, and the ability to place
multiple orders per poll. The aggressive profile was kept on the legacy
BUY_YES policy as a canary so the pre-PR-#3 strategy continues running
in parallel for comparison.

This file is the post-mortem-style writeup of what changed and why,
intended to be readable months later when nobody remembers the context.


## 1. Why we did this

The proximate cause was an alpha audit of the BTC pricer. Five days /
27 events / 18,854 non-degenerate quote rows from `pricer.db` showed
two findings that contradicted the bot's actual behavior:

1. **The raw model is biased high in the lower-mid deciles.** Across
   the trading band (`yes_bid ≥ 2¢` and `yes_ask ≤ 98¢`), at deciles
   d3–d5 the raw model predicted 13% / 24% / 38% probability of YES,
   while the realized rate was 7% / 15% / 26%. That is a systematic
   overprediction in exactly the price band where most edge appears.
2. **All BUY_YES variants were -EV in backtest. The SELL-side was
   strongly +EV** — at a 3¢ post-fee edge floor:
   - Raw `SELL_YES`: +$125 over 5 days, +3.4¢/trade.
   - Calibrated `SELL_YES`: +$309, +4.9¢/trade.
   - Calibrated + `mp < 0.85` filter: +$320, +5.5¢/trade.

The second finding was load-bearing. The bot was trading BUY_YES live;
the data said BUY_YES was a money-loser and the SELL-side was where the
edge was. But there was a critical wrinkle in the engine that the
analysis initially missed:

> `executor.py`'s `SELL_YES` branch only **closes** existing YES
> inventory. `held = open_contracts_by_market.get(...)` and we skip
> if `held <= 0`. With BUY_YES disabled, the bot would have nothing
> to sell, and SELL_YES would never fire on a fresh entry.

The live expression of a short-YES edge is therefore not `SELL_YES`,
but `BUY_NO` at `no_ask` — where `no_ask ≈ 100 − yes_bid` (the two
sides cross-book). Kalshi natively supports `side='no'` orders
(`kalshi_trader.py:184` writes `body["no_price"]` for NO-side limits)
and the API surface exists. The gap was the engine's signal layer,
the executor's order-building layer, and a few schema fields. Six
PRs were planned to land the change incrementally.


## 2. PR sequencing rationale

The PRs were intentionally not bundled. Rationale:

- **PR #1 first** (calibrator only): zero behavior change, just a new
  column. Lets us verify in-sample + LOO Brier before we bet on it.
- **PR #2 next** (NO-side ingest): also zero behavior change, just
  populates two new columns from the markets endpoint. The engine
  doesn't read them yet, so any plumbing bug fails harmlessly.
- **PR #3** (SidePolicy on BotProfile, `BUY_NO` return label): now
  `actionable_edge` can return `BUY_NO`, but the executor doesn't
  know how to place it — selective effectively pauses fresh entries.
  Aggressive is left on `LEGACY_POLICY` so trading continues.
- **PR #4** (executor `_build_ticket` BUY_NO branch): unblocks PR #3
  for selective. After #4, selective starts placing real BUY_NO
  orders.
- **PR #5** (edge floor 8 → 3, calibrated mp band gate): tunes the
  knobs to match the backtest sweep. Touches the band gate which is
  shared across BUY_YES and BUY_NO so aggressive's BUY_YES becomes
  band-filtered too.
- **PR #6** (top-K orders per poll, vol-window horizon-matching,
  spot-drift plumbing): independent risk-accounting changes that
  shouldn't ride the alpha-change PRs. Top-K activates by default;
  the other two are off-by-default knobs.

After all 6 land, the eth-pricer mirror was applied as one operation
since the two trees diverge only in identifiers, not logic.


## 3. PR-by-PR detail

### PR #1 — Isotonic calibrator + `polls.model_prob_calibrated`

**New module** `src/calibration.py` implements pool-adjacent-violators
(PAV) isotonic regression by hand (no sklearn). The `IsotonicCalibrator`
frozen dataclass holds (xs, ys) breakpoints plus train metadata; `apply`
uses bisect + linear interpolation with endpoint clamping; `fit` groups
ties, runs PAV, anchors the curve at (0, 0) and (1, 1) so out-of-train
queries are sensible, and computes pre/post-fit Brier scores.

**Schema**: `polls.model_prob_calibrated REAL` (nullable). Migration
in `_migrate()`. Insert SQL extended.

**Engine wiring**: `EngineConfig.calibrator_path: str = "./calibrator.json"`.
At engine startup, `load_calibrator(path)` returns a real calibrator if
the file exists or `identity()` (no-op `(0,0)→(1,1)` map) with a WARN
log if it doesn't. `build_poll_rows` accepts `calibrator` kwarg, applies
`calibrator.apply(model_prob)`, and stores the result in
`model_prob_calibrated`. Critically, **raw `model_prob` and `edge_cents`
were not changed** — the calibrated value is logged but not consumed
by `actionable_edge` until PR #5's band gate uses it.

**Fit script** `scripts/fit_calibrator.py` pulls (model_prob, outcome)
pairs from `polls JOIN settlements`, restricted to the trading band
(`yes_bid ≥ 2¢` and `yes_ask ≤ 98¢`) so the fit isn't dominated by
mechanically-determined far-OTM strikes. Optional `--loo` flag runs
leave-one-event-out Brier validation.

**Validation** on local BTC DB at the time:
- In-sample Brier 0.0955 → 0.0927 (improvement).
- LOO Brier 0.0955 → 0.0972 (slight overfit on point prediction —
  1,373 knots from 22,606 points is dense; but Brier is the wrong
  metric for trading).
- LOO **trading EV**: SELL @ 3¢ floor +$125 → +$140 (+12%);
  @ 8¢ floor +$23 → +$67 (+193%). Out-of-sample EV improved at both
  floors despite Brier degrading.

**Tests**: 15 unit tests covering identity, apply clamping, linear
interpolation between knots, JSON roundtrip, missing-file fallback,
bad-JSON fallback, identity recovery on perfectly-calibrated data,
correction of systematic bias, monotonicity of fit output, endpoint
anchoring.

### PR #2 — NO-side quote ingestion

Kalshi's markets endpoint returns `no_bid_dollars` and `no_ask_dollars`
but **not** NO-side depth — only `yes_bid_size_fp` / `yes_ask_size_fp`
are exposed. We ingest the prices into `polls.no_bid` / `polls.no_ask`
and `PollRow.no_bid` / `PollRow.no_ask`; sizes are not exposed in this
PR (when BUY_NO depth becomes load-bearing, the orderbook endpoint
exists separately).

**Schema**: two new nullable columns + ALTER migrations.

**Engine wiring**: `build_poll_rows` reads the new fields from each
market dict. No semantic change to existing callers.

**Tests**: 3 new — ingest, missing-NO falls back to `None`, schema
roundtrip persists NO-side prices.

### PR #3 — `SidePolicy` on `BotProfile`

**New dataclass** `SidePolicy(allow_buy_yes, allow_buy_no, sell_yes_to_close_only)`
in `engine.py`. `LEGACY_POLICY = SidePolicy(True, False, False)` is the
default. `actionable_edge(row, policy=LEGACY_POLICY)` now considers
BUY_NO as an additional candidate (`(1 - model_prob) * 100 - no_ask*100 - fee(no_ask)`),
applies the same regime gates (`_passes_buy_gates`) to both BUY sides,
and returns one of `'BUY_YES' | 'BUY_NO' | 'SELL_YES' | 'NONE'` based
on the policy plus eligibility plus best edge.

**`BotProfile.policy: SidePolicy`** field added (default `LEGACY_POLICY`).

**Production profiles** at this PR:
- `selective`: `SidePolicy(False, True, True)` — BUY_NO entries only,
  SELL_YES only to close.
- `aggressive`: kept on `LEGACY_POLICY` as canary.

**Executor wiring**: `handle_poll` passes `self.profile.policy` to
`actionable_edge`. `_build_ticket` rejects the `'BUY_NO'` label with a
debug log (PR #4 wires the actual placement). Engine display +
shadow-signal logging continues to use the default `LEGACY_POLICY` so
dashboards aren't distorted by per-bot config.

**Test refactor**: existing executor mechanic tests (`Executor(db, ...)`
defaulting to selective profile) needed a permissive policy to keep
exercising BUY_YES — added a `_legacy_profile()` test helper and
threaded it through every `Executor()` call site. The production
selective profile now has the new policy; tests use the helper.

**Tests**: 10 new policy tests covering legacy default, allow_buy_yes=False
blocks, allow_buy_no=True with profitable no_ask returns BUY_NO, BUY_NO
regime-gated, BUY_NO without no_ask is NONE, selective picks BUY_NO over
SELL_YES, selective falls back to SELL_YES with no NO quote, plus 2
end-to-end executor checks.

### PR #4 — Executor `_build_ticket` BUY_NO branch

**`OrderTicket.side`** is now `'yes' | 'no'` (was effectively `'yes'`).

**`_build_ticket`**: BUY_NO branch — `action='buy'`, `ticket_side='no'`,
`limit_cents = round(no_ask * 100)`. Concentration cap is keyed per
`(market, side)` so YES holdings and NO holdings consume separate
slots. Notional cap binds across YES + NO aggregate exposure (the
`PositionSnapshot.open_notional_usd` already aggregates both sides).
TTL cancel, kill switch, rate limit, time-to-close guards are inherited
unchanged from the existing path.

**Telegram label**: `_notify` now distinguishes `BUY NO` from `BUY YES`
(and `SELL NO` for symmetry, even though we don't emit it).

**Tests**: 5 new — dry-run records `side='no'` intent at correct price,
concentration cap on `(market, "no")` independent of `(market, "yes")`,
notional cap binds across YES + NO, live mode POSTs `side='no'` to the
trader, Telegram label says `BUY NO`.

### PR #5 — Edge floor 8 → 3 + calibrated mp band gate

**Selective `min_edge_cents`: 8.0 → 3.0**. The validated `_MIN_EDGE_FLOOR_CENTS = 2.0`
ceiling still binds (set in module-level executor constants).

**Band gate**: `BUY_GATE_MP_BAND_LO = 0.05`, `BUY_GATE_MP_BAND_HI = 0.85`.
`_passes_buy_gates` adds the check `BAND_LO <= cal_mp < BAND_HI` where
`cal_mp = row.model_prob_calibrated if not None else row.model_prob`.
Falls back to raw mp when the calibrator file is missing so the gate
still functions in degraded mode. Applies to BUY_YES + BUY_NO; SELL_YES
remains ungated (closing inventory is unconditional).

**Shadow logging**: `shadow_signals.gate_mp_band_passed INTEGER` column
+ migration. `_gate_flags` returns 4 values now.

**Tests**: 5 new — blocks BUY_YES at upper bound (0.86), blocks BUY_NO
at lower bound (0.04), SELL_YES bypasses the band, calibrated mp
overrides raw mp when present, `[lo, hi)` boundary semantics.

One existing test (`test_min_edge_floor_blocks_small_edges`) had to be
adjusted — its row produced exactly 3¢ net edge under the old setup,
which now passes the new 3¢ floor. Set `model_prob=0.275` (instead of
0.30) so the net edge is 0.5¢ and below the floor.

### PR #6 — Top-K orders per poll, vol-window horizon-matching, drift

Three independent risk-accounting features bundled because they touch
the same surface:

**A. Top-K orders per poll** (default ON, `MAX_ORDERS_PER_POLL = 3`).
Previously `handle_poll` returned after one placement. Now: deepcopy
the snapshot, loop through edge-ranked candidates, build + place each
ticket up to K, mutating the working snap between placements so each
subsequent `_build_ticket` sees running notional + per-strike held
counts. Re-checks `MAX_ORDERS_PER_MINUTE` between placements so a
single high-K poll can't blow past the per-minute rate limit. Returns
an aggregate `Decision` (placed=True if any).

**B. Vol-window horizon-matching** (default OFF, opt-in via
`match_vol_window_to_horizon: true` in config). When enabled, the σ
estimation window shrinks toward `min(vol_window_minutes, max(20, ceil(seconds_to_settlement/60)))`.
The 20-min floor (`VOL_WINDOW_MIN_FLOOR_MIN`) keeps Yang-Zhang stable.
Default is off because it changes the σ estimate live; user should A/B
in the dashboard before flipping to default.

**C. Spot drift parameter** (default 0.0). Both `prob_above_strike` and
`prob_above_strike_path_dependent` accept a `drift_per_year: float = 0.0`
kwarg. The path-dependent variant integrates drift correctly inside the
averaging window (drift contribution is scaled by `w_n` for the
unrealized portion). `EngineConfig.spot_drift_per_year: float = 0.0` is
plumbed through `build_poll_rows`. **No estimator** — pin a static
value if you want it active; default zero recovers the legacy formula
exactly.

**Tests**: 3 top-K tests (distinct strikes get K placements, running
notional blocks 4th, running held blocks 3rd same-market repeat) +
4 drift tests (zero recovery for both pricer functions, positive
drift raises P(above), drift sign affects path-dep P in expected
direction).


## 4. Eth-pricer mirror

The eth-pricer tree was at the pre-PR-#1 state. The mirror was applied
mostly mechanically because the two projects diverge only in:
- Bot ids: `eth-selective` / `eth-aggressive` vs `btc-*`
- COID prefixes: `ethp-` / `etha-` vs `btcp-` / `btca-`
- Series: `KXETHD` vs `KXBTCD`
- Feed module: `eth_feed` vs `btc_feed`
- Averaging-window comments: `ERTI` vs `BRTI`
- Caps: ETH selective is `$10` notional / `$10` daily-loss vs BTC's `$30`/`$30`.

Two notional-cap tests had to be rescaled (25 → 5, 29 → 9, 29.99 → 9.99,
20 → 1) for ETH's $10 cap.

Final test counts:
- BTC: **128 passed**.
- ETH: **128 passed**.


## 5. Deploy operation

### 5.1. Local → GitHub

Single commit `5aa44fe` pushed to `origin/main` of
`github.com/cchen0800/kalshi-pricer`. The eth-pricer tree has no
git remote (it is currently untracked), so it stayed local until the
VPS sync.

### 5.2. GitHub → VPS (kalshi-pricer)

The VPS at `personal-vps:~/kalshi-pricer` was at HEAD `41714d9` (two
commits behind origin) and had **uncommitted local edits to**
`dashboard.py`, `main.py`, `src/db.py`, `src/executor.py`,
`src/positions.py`, `templates/dashboard.html`, `tests/test_executor.py`,
`tests/test_positions.py`, `trade.py`. Inspection showed these edits
were essentially the content of commits `a08e6a1` (aggressive profile)
and `3817c58` (AM/PM dashboard) — i.e., the same logic had been edited
live on the VPS but never committed there, while it had been committed
locally on the laptop and pushed via origin.

Resolution: `git stash push -u -m "pre-PR1-6-pull-2026-05-05"` (the
`-u` flag stashes untracked files too) followed by `git pull --ff-only`.
The stash is preserved on the VPS for recovery if anything turns out
divergent.

**Caveat that bit us**: the `-u` flag swept the untracked
`logs/` directory into the stash. After the pull the `tee -a logs/*.log`
in the screen launchers had no directory to write to, the trade.py
processes died on broken-pipe, and the dashboard happened to still be
running. The fix was to recreate the logs/ directory and restart the
BTC trade bots; ETH was unaffected because its logs/ was excluded
from the tar push (separate sync mechanism — see below).

### 5.3. Local → VPS (eth-pricer)

`eth-pricer` has no git on the VPS. Tar-over-SSH was used after a
failed `rsync` attempt (rsync isn't installed on the VPS). Excludes:
`.venv/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.env`,
`.env.example`, `.secrets/`, `pricer*.db*` (live trading databases —
never overwrite), `*.log`, `logs/`, `.kill*`. The DB exclusion is
load-bearing; the bots write to those files continuously.

### 5.4. DB migrations

All four live SQLite DBs (`pricer.db` and `pricer.aggressive.db` for
each project) had their migrations applied by virtue of the next
`open_db()` call running through `_migrate()`. New columns are nullable
so legacy rows have NULL and the running engine inserts populated
values from now on. Verified post-migration:
- `polls.model_prob_calibrated`, `polls.no_bid`, `polls.no_ask` present
  on all 4 DBs.
- `shadow_signals.gate_mp_band_passed` present on all 4 DBs.

### 5.5. Bot restart

Six screen sessions on the VPS:
- `kalshi-web` (port 5051) — BTC dashboard, `--no-engine`
- `kalshi-eth-web` (port 5052) — ETH dashboard, `--no-engine`
- `kalshi-selective` — BTC, `trade.py --live --profile selective --yes-i-know`
- `kalshi-aggressive` — BTC, `trade.py --live --profile aggressive --yes-i-know --no-telegram-listen`
- `kalshi-eth-selective` — ETH, `trade.py --live --profile selective --yes-i-know --no-telegram-listen`
- `kalshi-eth-aggressive` — ETH, `trade.py --live --profile aggressive --yes-i-know --no-telegram-listen`

Note that **only `kalshi-selective` is the Telegram listener** (no
`--no-telegram-listen` flag). Kalshi's Telegram bot API rejects with
409 if more than one process holds the long poll, so this asymmetry
must be preserved on every restart.

Two restart waves:
1. First restart: all 6 sessions. ETH came up clean; BTC trade bots died
   on missing `logs/` dir.
2. Second restart (BTC only): `mkdir -p logs/`, then re-launched
   `kalshi-selective`, `kalshi-aggressive`, `kalshi-web`. All healthy.

### 5.6. Calibrator fit

After the bots came up on the new code (with the calibrator file absent,
so the band gate was running on raw mp), `scripts/fetch_settlements.py`
populated the `settlements` table for both projects from Coinbase 1m
candles, then `scripts/fit_calibrator.py` was run.

**BTC calibrator** (deployed):
- n=72,485 (mp, y) pairs from 78 events
- 397 knots
- raw_brier=0.1028 → cal_brier=0.1012
- Per-decile diagnostic clean — `cal_p` tracks `real_p` within ~0.005
  across all 10 deciles.

**ETH calibrator** (NOT deployed — overfit):
- n=922 pairs from only 5 events
- 624 knots (more knots than reasonable, given data size)
- `cal_p` diverges meaningfully from `real_p` in deciles d4–d8
  (e.g., d8: cal_p=0.627 vs real_p=0.511; d7: cal_p=0.328 vs real_p=0.533).
- Action: file renamed to `calibrator.json.disabled.2026-05-06`. The
  ETH engine logs the missing-file warning at startup and falls back to
  identity (`calibrated == raw`). The band gate still runs on raw mp.

After fit, all 4 trade bots were bounced. BTC selective + aggressive
loaded the calibrator (`calibration: loaded calibrator from
calibrator.json (n_knots=397, n_train=72485, fit_brier=0.10281...,
cal_brier=0.10120...)` in the startup logs). ETH selective +
aggressive logged the missing-file warning as expected.


## 6. What is live right now

| Bot                       | Profile     | Policy                                | min_edge | Calibrator | Top-K | Band gate |
|---------------------------|-------------|---------------------------------------|----------|------------|-------|-----------|
| `kalshi-selective`        | selective   | (False, True, True) — BUY_NO entries  | 3¢       | LOADED     | 3     | calibrated mp |
| `kalshi-aggressive`       | aggressive  | LEGACY — BUY_YES canary               | 3¢       | LOADED     | 3     | calibrated mp |
| `kalshi-eth-selective`    | selective   | (False, True, True) — BUY_NO entries  | 3¢       | identity   | 3     | raw mp        |
| `kalshi-eth-aggressive`   | aggressive  | LEGACY — BUY_YES canary               | 3¢       | identity   | 3     | raw mp        |

Both dashboards are up: BTC on port 5051, ETH on port 5052.

Vol-window horizon-matching and spot drift are both off for all bots
(default config). They are knob-only in this release.


## 7. Known follow-ups

1. **ETH calibrator fit** once enough events have settled. Probably
   needs ≥ 30 events / ~5K (mp, y) pairs to be stable. Re-fit, then
   bounce `kalshi-eth-selective` + `kalshi-eth-aggressive`. Same fit
   script + bounce sequence as BTC.
2. **Knot-smoothing / monotone spline** for the BTC calibrator. 397
   knots from 72K points is dense but not absurd; if Brier degrades
   over time as the calibrator trains on more data we may want to
   regularize. Not blocking.
3. **Periodic re-fit cron** (weekly?) once we have rolling settlement
   coverage. The calibrator should drift with regime; a static fit
   from one week stops being right.
4. **Eth-pricer git repo**. Currently untracked; the deploy mechanism
   is tar-over-SSH which is fine but not symmetric with BTC. Either
   bring it under the kalshi-pricer git repo as a subdir or give it
   its own remote.
5. **Spot-drift estimator**. The plumbing landed in PR #6 but no
   estimator exists. If we want this active, a small EWMA of log-returns
   over the past N minutes is the obvious starting point — but it
   should be backtested before going live.
6. **`MAX_CONTRACTS_PER_STRIKE` policy**. After PR #4, the per-strike
   cap is keyed per `(market, side)` so 10 YES + 10 NO can both be
   open. They net economically but consume separate slots. If you
   prefer one combined cap, change `_build_ticket` to sum across
   sides.
7. **Re-enable BUY_YES on selective**? Selective currently has
   `allow_buy_yes=False`. The data said BUY_YES was -EV; if the model
   improves, this gate flips back. The aggressive profile gives us a
   live A/B in the meantime.


## 8. Operational reference

### Refit and reload calibrator (BTC)
```bash
ssh personal-vps
cd ~/kalshi-pricer && source .venv/bin/activate
python scripts/fetch_settlements.py
python scripts/fit_calibrator.py
# bounce
screen -S kalshi-selective -X quit
screen -S kalshi-aggressive -X quit
sleep 2
screen -dmS kalshi-selective bash -c "cd /home/chris/kalshi-pricer && source .venv/bin/activate && python trade.py --live --profile selective --yes-i-know 2>&1 | tee -a logs/selective.log"
screen -dmS kalshi-aggressive bash -c "cd /home/chris/kalshi-pricer && source .venv/bin/activate && python trade.py --live --profile aggressive --yes-i-know --no-telegram-listen 2>&1 | tee -a logs/aggressive.log"
```

### Refit and reload calibrator (ETH)
Same as above, swap `~/kalshi-pricer` → `~/eth-pricer` and screen names
to `kalshi-eth-selective` / `kalshi-eth-aggressive`. Note ETH's selective
also needs `--no-telegram-listen`.

### Kill switches
- BTC selective: `touch ~/kalshi-pricer/.kill`
- BTC aggressive: `touch ~/kalshi-pricer/.kill.aggressive`
- ETH selective: `touch ~/eth-pricer/.kill`
- ETH aggressive: `touch ~/eth-pricer/.kill.aggressive`

The executor checks the file's existence on every poll before any other
guard, so kill is effectively immediate.

### Recover the pre-deploy stash on VPS
```bash
ssh personal-vps
cd ~/kalshi-pricer && git stash list
# pre-PR1-6-pull-2026-05-05 stash will be there until you drop it
git stash show -p stash@{0}     # inspect
git stash drop stash@{0}        # discard (likely safe — equivalent
                                # commits are already in origin)
```


## 9. Test inventory

| Suite              | Before deploy | After deploy |
|--------------------|---------------|--------------|
| `test_calibration` | (didn't exist)| 15           |
| `test_engine`      | 18            | 34           |
| `test_executor`    | 12            | 20           |
| `test_pricer`      | 17            | 21           |
| `test_realized`    | 10            | 10           |
| `test_vol`         | 10            | 10           |
| `test_positions`   | 18            | 18           |
| **Total**          | **85**        | **128**      |

128 passed in BTC, 128 passed in ETH, both on the VPS post-restart.
