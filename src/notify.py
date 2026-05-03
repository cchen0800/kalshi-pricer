"""Telegram notifications and remote-kill listener.

Failsafe by design: a Telegram outage must NEVER block trading. Errors are
logged at WARNING and swallowed.

Configuration via env (loaded by python-dotenv from .env):
  TELEGRAM_BOT_TOKEN   — bot token from @BotFather (REQUIRED)
  TELEGRAM_CHAT_ID     — your chat ID with the bot (REQUIRED)

If either is missing, the notifier and listener are no-ops.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

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


class TelegramKillListener:
    """Background long-poll for /kill and /status from the configured chat.

    Runs in a daemon thread. On `/kill` it touches the kill file (the same
    one the executor checks before each order). To resume, you must SSH in
    and `rm .kill` — by design the asymmetry: easy to halt, hard to resume.

    Only messages from the configured `chat_id` are honored. Anything else
    is ignored (so a leaked token doesn't let a stranger halt your trader).
    """

    def __init__(
        self,
        kill_file: str | Path,
        token: str | None = None,
        chat_id: str | None = None,
        long_poll_seconds: int = 30,
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "")
        self.kill_file = Path(kill_file)
        self.long_poll_seconds = long_poll_seconds
        self.enabled = bool(self.token and self.chat_id)
        self._stop = threading.Event()
        self._http: httpx.Client | None = None
        self._thread: threading.Thread | None = None
        self._offset = 0

    def start(self) -> None:
        if not self.enabled:
            log.info("Telegram kill listener disabled (missing token or chat_id)")
            return
        # Long-poll holds for up to `long_poll_seconds`; give httpx a slightly
        # longer read timeout so we don't time out before Telegram responds.
        self._http = httpx.Client(timeout=self.long_poll_seconds + 10)
        self._discard_backlog()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tg-kill")
        self._thread.start()
        log.info("Telegram kill listener armed — send '/kill' to halt trading")

    def stop(self) -> None:
        self._stop.set()
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass

    def _discard_backlog(self) -> None:
        """Skip past any unread messages so we don't replay old commands."""
        if self._http is None:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates"
            resp = self._http.get(url, params={"timeout": 0}, timeout=5)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                self._offset = data["result"][-1]["update_id"] + 1
        except Exception as e:
            log.warning("kill listener backlog discard failed: %s", e)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log.warning("kill listener poll error: %s; sleeping 5s", e)
                self._stop.wait(5.0)

    def _poll_once(self) -> None:
        assert self._http is not None
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        resp = self._http.get(
            url,
            params={"timeout": self.long_poll_seconds, "offset": self._offset},
        )
        data = resp.json()
        if not data.get("ok"):
            self._stop.wait(2.0)
            return
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            self._handle(update)

    def _handle(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id = str((msg.get("chat") or {}).get("id"))
        if chat_id != self.chat_id:
            log.warning("ignoring command from foreign chat_id=%s", chat_id)
            return
        text = (msg.get("text") or "").strip().lower()
        if text in ("/kill", "kill", "/stop", "stop", "halt", "/halt"):
            self.kill_file.touch()
            self._reply(
                f"⚠️ kill switch ARMED — executor will stop placing new orders at next poll.\n"
                f"To resume: SSH in and `rm {self.kill_file}`"
            )
            log.warning("kill switch ARMED via Telegram")
        elif text in ("/status", "status"):
            armed = self.kill_file.exists()
            self._reply(
                f"kill: {'ARMED ⛔' if armed else 'idle ✅'}\n"
                f"file: `{self.kill_file}`"
            )
        elif text in ("/start", "/help", "help"):
            self._reply(
                "commands:\n"
                "  /kill   — halt new orders (touches .kill)\n"
                "  /status — report kill-switch state\n"
                "to resume after /kill: SSH in and `rm .kill`"
            )

    def _reply(self, text: str) -> None:
        if self._http is None:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            self._http.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            log.warning("kill listener reply failed: %s", e)
