"""Fit the isotonic calibrator from polls JOIN settlements and write calibrator.json.

Reads ground-truth `settlements` (populated by scripts/fetch_settlements.py
or the engine's settlement scrape path), joins with `polls` to get per-poll
labels, and fits an isotonic mapping from raw `model_prob` to realized
hit-rate.

Usage:
    python scripts/fit_calibrator.py
    python scripts/fit_calibrator.py --db ./pricer.db --out ./calibrator.json
    python scripts/fit_calibrator.py --loo   # leave-one-event-out Brier check

Output: a JSON file the engine loads at startup (path in config.yaml).

This script does not change live behavior on its own — PR #1 only exposes
`model_prob_calibrated` as a column. PR #3 will switch the trade decision
to read it.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

# Make `src.` imports work whether invoked from project root or scripts/.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.calibration import IsotonicCalibrator, fit, save  # noqa: E402

log = logging.getLogger("fit_calibrator")


def load_pairs(db: sqlite3.Connection, *, min_bid_cents: int = 2, max_ask_cents: int = 98):
    """Pull (model_prob, outcome) pairs from polls JOIN settlements.

    We restrict to non-degenerate quote rows (yes_bid > 2¢, yes_ask < 98¢) so
    the calibrator isn't dominated by far-OTM strikes where every poll is
    quoted at the floor / ceiling and the realized rate is mechanically
    0 or 1. Calibration only matters in the trading band.
    """
    rows = db.execute(
        """
        SELECT p.model_prob, s.settle_price, p.strike, p.yes_bid, p.yes_ask
          FROM polls p
          JOIN settlements s USING(event_ticker)
         WHERE p.yes_bid IS NOT NULL AND p.yes_ask IS NOT NULL
           AND p.yes_bid >= ? AND p.yes_ask <= ?
        """,
        (min_bid_cents / 100.0, max_ask_cents / 100.0),
    ).fetchall()
    probs: list[float] = []
    outcomes: list[int] = []
    for mp, settle_price, strike, _yb, _ya in rows:
        if mp is None or settle_price is None or strike is None:
            continue
        probs.append(float(mp))
        outcomes.append(1 if float(settle_price) > float(strike) else 0)
    return probs, outcomes


def fit_loo(db: sqlite3.Connection) -> tuple[float, float, int]:
    """Leave-one-event-out cross-validated Brier score.

    Drop one event at a time, fit on the rest, score on the held-out event.
    Returns (raw_brier, cal_brier, n_events).
    """
    events = [r[0] for r in db.execute("SELECT DISTINCT event_ticker FROM settlements").fetchall()]
    raw_total = 0.0
    cal_total = 0.0
    n_total = 0
    for ev in events:
        rows = db.execute(
            """
            SELECT p.model_prob, s.settle_price, p.strike, p.event_ticker
              FROM polls p
              JOIN settlements s USING(event_ticker)
             WHERE p.yes_bid IS NOT NULL AND p.yes_ask IS NOT NULL
               AND p.yes_bid >= 0.02 AND p.yes_ask <= 0.98
            """
        ).fetchall()
        train_p: list[float] = []
        train_y: list[int] = []
        test_p: list[float] = []
        test_y: list[int] = []
        for mp, settle_price, strike, ev_t in rows:
            if mp is None or settle_price is None or strike is None:
                continue
            y = 1 if float(settle_price) > float(strike) else 0
            if ev_t == ev:
                test_p.append(float(mp)); test_y.append(y)
            else:
                train_p.append(float(mp)); train_y.append(y)
        if not test_p or len(train_p) < 100:
            continue
        cal = fit(train_p, train_y)
        for p, y in zip(test_p, test_y):
            raw_total += (p - y) ** 2
            cal_total += (cal.apply(p) - y) ** 2
            n_total += 1
    if n_total == 0:
        return 0.0, 0.0, 0
    return raw_total / n_total, cal_total / n_total, len(events)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "pricer.db"), help="path to pricer.db")
    ap.add_argument("--out", default=str(ROOT / "calibrator.json"), help="output JSON path")
    ap.add_argument("--loo", action="store_true", help="also report leave-one-event-out Brier")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1
    conn = sqlite3.connect(str(db_path))

    n_events = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    if n_events == 0:
        log.error(
            "no rows in `settlements` — run scripts/fetch_settlements.py first "
            "(or wait for the engine's settlement scraper)"
        )
        return 1

    probs, outcomes = load_pairs(conn)
    if len(probs) < 1000:
        log.warning(
            "only %d (mp, y) pairs available — calibrator may be unstable; "
            "consider waiting for more events to settle",
            len(probs),
        )

    cal = fit(probs, outcomes)
    save(cal, args.out)

    log.info(
        "fit complete: n=%d events=%d knots=%d raw_brier=%.4f cal_brier=%.4f → %s",
        len(probs), n_events, len(cal.xs), cal.fit_brier, cal.cal_brier, args.out,
    )

    # Per-decile diagnostic — quick eyeball of where the bias was largest.
    print("\nPer-decile calibration (raw → calibrated):")
    pairs = sorted(zip(probs, outcomes), key=lambda t: t[0])
    n = len(pairs)
    print(f"  {'decile':>6s}  {'raw_p':>7s}  {'cal_p':>7s}  {'real':>7s}  {'n':>6s}")
    for i in range(10):
        lo, hi = i * n // 10, (i + 1) * n // 10
        if hi <= lo:
            continue
        seg = pairs[lo:hi]
        raw_avg = sum(p for p, _ in seg) / len(seg)
        real = sum(y for _, y in seg) / len(seg)
        cal_avg = sum(cal.apply(p) for p, _ in seg) / len(seg)
        print(f"  d{i+1:>2d}     {raw_avg:>7.3f}  {cal_avg:>7.3f}  {real:>7.3f}  {len(seg):>6d}")

    if args.loo:
        log.info("running leave-one-event-out Brier check (slow)…")
        raw_loo, cal_loo, n_ev = fit_loo(conn)
        if n_ev == 0:
            log.warning("LOO not feasible (too few events)")
        else:
            print(
                f"\nLOO Brier (over {n_ev} events): "
                f"raw={raw_loo:.4f}  calibrated={cal_loo:.4f}  "
                f"Δ={cal_loo - raw_loo:+.4f} (lower is better)"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
