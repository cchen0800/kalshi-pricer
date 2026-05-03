"""Trading entrypoint: engine + executor.

Default is --dry-run: same code path, same order tickets logged to
intended_orders, but no POST. Use --live (and answer the confirmation prompt)
to actually place orders.

Hardcoded $30 budget — see src/executor.py guards.
"""

from __future__ import annotations

import argparse
import logging
import sys

from src.db import open_db
from src.executor import (
    MAX_DAILY_LOSS_USD,
    MAX_NOTIONAL_USD,
    MIN_EDGE_CENTS,
    MIN_MINUTES_TO_CLOSE,
    Executor,
)
from src.engine import EngineConfig, run
from src.executor import KILL_FILE
from src.kalshi_trader import KalshiTrader
from src.notify import TelegramKillListener, TelegramNotifier

from main import load_config

log = logging.getLogger("trade")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="log intended orders, do not POST")
    g.add_argument("--live", action="store_true", help="place real orders against Kalshi")
    p.add_argument(
        "--yes-i-know",
        action="store_true",
        help="skip the live-mode confirmation prompt",
    )
    return p.parse_args()


def confirm_live() -> bool:
    print("=" * 70)
    print("LIVE TRADING — REAL MONEY")
    print("=" * 70)
    print(f"  Max notional outstanding: ${MAX_NOTIONAL_USD:.2f}")
    print(f"  Max daily realized loss:  ${MAX_DAILY_LOSS_USD:.2f}")
    print(f"  Min edge to act:          {MIN_EDGE_CENTS:.1f}¢")
    print(f"  Stop trading at:          T-{MIN_MINUTES_TO_CLOSE:.1f}min before close")
    print(f"  Kill switch:              touch .kill")
    print("=" * 70)
    print("Type 'i accept the risk' to proceed:")
    try:
        line = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return line == "i accept the risk"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    cfg: EngineConfig = load_config()

    if args.live and not args.yes_i_know:
        if not confirm_live():
            print("aborted")
            return 1

    if args.live:
        log.warning("LIVE MODE — real orders will be placed")
    else:
        log.info("DRY-RUN MODE — orders will be logged to intended_orders only")

    trader: KalshiTrader | None = None
    notifier = TelegramNotifier()
    kill_listener = TelegramKillListener(kill_file=KILL_FILE)
    try:
        if args.live:
            trader = KalshiTrader()
            bal = trader.get_balance()
            log.info("kalshi balance: %s", bal)

        kill_listener.start()

        with open_db(cfg.db_path) as db:
            executor = Executor(db, trader, live=args.live, notifier=notifier)

            def on_poll(rows):
                d = executor.handle_poll(rows)
                if d.placed:
                    log.info("ORDER %s — %s", d.reason, d.ticket)
                else:
                    log.debug("no order: %s", d.reason)

            run(cfg, on_poll=on_poll)
    finally:
        kill_listener.stop()
        if trader is not None:
            trader.close()
        notifier.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
