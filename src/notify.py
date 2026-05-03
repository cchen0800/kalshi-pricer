"""Telegram notifications for placed orders.

Failsafe by design: a Telegram outage must NEVER block trading. Errors are
logged at WARNING and swallowed.

Configuration via env (loaded by python-dotenv from .env):
  TELEGRAM_BOT_TOKEN   — bot token from @BotFather (REQUIRED)
  TELEGRAM_CHAT_ID     — your chat ID with the bot (REQUIRED)

If either is missing, the notifier is a no-op and logs once at startup.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("notify")


class TelegramNotifier:
    """Posts text to a single chat. Use `enabled` to gate cheaply."""

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self._http = httpx.Client(timeout=timeout)
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            log.info("Telegram notifications disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")

    def __enter__(self) -> "TelegramNotifier":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            resp = self._http.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
            )
            if resp.status_code >= 400:
                log.warning("telegram %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            # Swallow: a Telegram outage must not block trading.
            log.warning("telegram send failed: %s", e)
