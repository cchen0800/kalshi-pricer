"""Trading entrypoint: engine + executor.

Default is --dry-run: same code path, same order tickets logged to
intended_orders, but no POST. Use --live (and answer the confirmation prompt)
to actually place orders.

--profile selects the bot's risk profile (see src/executor.BOT_PROFILES).
Profiles are hardcoded in src/executor.py; YAML cannot widen any cap.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.db import open_db
from src.executor import (
    BOT_PROFILES,
    MIN_MINUTES_TO_CLOSE,
    BotProfile,
    Executor,
)
from src.engine import EngineConfig, run
from src.fill_sync import FillSyncer
from src.kalshi_client import KalshiClient
from src.kalshi_trader import KalshiTrader
from src.notify import TelegramKillListener, TelegramNotifier
from src.settlement_scraper import SettlementScraper
from src.trade_history import format_pnl_telegram, format_trades_telegram

from main import load_config

log = logging.getLogger("trade")

PROFILE_CONFIGS: dict[str, str] = {
    "selective": "config.yaml",
    "aggressive": "config.aggressive.yaml",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="log intended orders, do not POST")
    g.add_argument("--live", action="store_true", help="place real orders against Kalshi")
    p.add_argument(
        "--profile",
        choices=sorted(BOT_PROFILES.keys()),
        default="selective",
        help="risk profile (default: selective)",
    )
    p.add_argument(
        "--yes-i-know",
        action="store_true",
        help="skip the live-mode confirmation prompt",
    )
    p.add_argument(
        "--no-telegram-listen",
        action="store_true",
        help="skip the Telegram kill listener (use on all but one bot when "
             "multiple bots share a Telegram bot — Telegram only allows one "
             "long-poll holder, others get 409 Conflict)",
    )
    return p.parse_args()


def confirm_live(profile: BotProfile, balance_usd: float | None = None) -> bool:
    print("=" * 70)
    print(f"LIVE TRADING — REAL MONEY  [profile: {profile.bot_id}]")
    print("=" * 70)
    if balance_usd is not None:
        print(f"  Portfolio balance:        ${balance_usd:.2f}")
        notional = profile.max_notional_pct * balance_usd
        loss = profile.max_daily_loss_pct * balance_usd
        print(f"  Max notional outstanding: {profile.max_notional_pct:.0%} = ${notional:.2f}")
        print(f"  Max daily realized loss:  {profile.max_daily_loss_pct:.0%} = ${loss:.2f}")
    else:
        print(f"  Max notional outstanding: {profile.max_notional_pct:.0%} of portfolio")
        print(f"  Max daily realized loss:  {profile.max_daily_loss_pct:.0%} of portfolio")
    print(f"  Min edge to act:          {profile.min_edge_cents:.1f}¢")
    print(f"  Stop trading at:          T-{MIN_MINUTES_TO_CLOSE:.1f}min before close")
    print(f"  Kill switch:              touch {profile.kill_file}")
    print(f"  COID prefix:              {profile.coid_prefix}")
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
    profile = BOT_PROFILES[args.profile]
    cfg_path = Path(__file__).parent / PROFILE_CONFIGS[args.profile]
    cfg: EngineConfig = load_config(cfg_path)
    log.info("profile=%s db=%s", profile.bot_id, cfg.db_path)

    trader: KalshiTrader | None = None
    balance_usd: float | None = None
    if args.live:
        trader = KalshiTrader()
        bal = trader.get_balance()
        balance_usd = (bal.get("balance", 0) + bal.get("portfolio_value", 0)) / 100.0
        log.info("kalshi balance: %s (portfolio $%.2f)", bal, balance_usd)

    if args.live and not args.yes_i_know:
        if not confirm_live(profile, balance_usd):
            if trader is not None:
                trader.close()
            print("aborted")
            return 1

    if args.live:
        log.warning("LIVE MODE — real orders will be placed (profile=%s)", profile.bot_id)
    else:
        log.info(
            "DRY-RUN MODE — orders will be logged to intended_orders only (profile=%s)",
            profile.bot_id,
        )

    # Read-only client for the settlement scraper. Engine has its own internal
    # client for poll fetches; this one's lifecycle is independent so the
    # scraper can run inside the on_poll callback without coupling to engine.
    scrape_client: KalshiClient | None = None
    notifier = TelegramNotifier()
    # Build read-side Telegram handlers. Each opens its own short-lived DB
    # connection so it doesn't have to coordinate with the executor's writer.
    def _pnl():
        with open_db(cfg.db_path) as h_db:
            return format_pnl_telegram(h_db, trader)

    def _trades():
        with open_db(cfg.db_path) as h_db:
            return format_trades_telegram(h_db, trader, limit=8)

    kill_listener = TelegramKillListener(
        kill_file=profile.kill_file,
        command_handlers={"pnl": _pnl, "trades": _trades},
    )
    try:

        if not args.no_telegram_listen:
            kill_listener.start()
        else:
            log.info("Telegram listener disabled by --no-telegram-listen")

        scrape_client = KalshiClient()
        with open_db(cfg.db_path) as db:
            executor = Executor(
                db, trader, live=args.live, notifier=notifier, profile=profile,
            )
            # Local mirror of Kalshi fills + settlements, for offline analysis.
            # Throttled to once / 60s — the dashboard reads live so we don't
            # need fresher than that here.
            syncer = FillSyncer(trader, interval_s=60.0) if trader is not None else None
            # Ground-truth resolution scrape for *every* settled market — fills
            # the gap that portfolio_settlements only covers markets we held.
            # 5min cadence: events close hourly, so this is far more than enough.
            scraper = SettlementScraper(scrape_client, interval_s=300.0)

            def on_poll(rows):
                d = executor.handle_poll(rows)
                if d.placed:
                    log.info("ORDER %s — %s", d.reason, d.ticket)
                else:
                    log.debug("no order: %s", d.reason)
                if syncer is not None:
                    syncer.maybe_sync(db)
                scraper.maybe_scrape(db)

            run(cfg, on_poll=on_poll, shadow_policy=profile.policy)
    finally:
        kill_listener.stop()
        if trader is not None:
            trader.close()
        if scrape_client is not None:
            scrape_client.close()
        notifier.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
