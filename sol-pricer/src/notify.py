"""Telegram notifications and remote-kill listener.

Failsafe by design: a Telegram outage must NEVER block trading. Errors are
logged at WARNING and swallowed.

Configuration via env (loaded by python-dotenv from .env):
  TELEGRAM_BOT_TOKEN   — bot token from @BotFather (REQUIRED)
  TELEGRAM_CHAT_ID     — your chat ID with the bot (REQUIRED)
  TELEGRAM_ALERT_CHAT_IDS          — optional comma-separated alert-only chats
  TELEGRAM_ALLOW_ALERT_SUBSCRIBE   — set to 1 to let /start subscribe a chat
  TELEGRAM_ALERT_SUBSCRIBERS_PATH  — optional subscriber file path

If either is missing, the notifier and listener are no-ops.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("notify")

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _split_chat_ids(value: str | None) -> list[str]:
    if not value:
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for raw in value.replace("\n", ",").split(","):
        chat_id = raw.strip()
        if chat_id and chat_id not in seen:
            ids.append(chat_id)
            seen.add(chat_id)
    return ids


def _default_subscribers_path() -> Path:
    # Shared by kalshi-pricer, eth-pricer, and sol-pricer when they live under
    # the same repository root, so one /start subscription receives all alerts.
    return Path(__file__).resolve().parents[2] / ".telegram_alert_subscribers"


def _subscribers_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = os.environ.get("TELEGRAM_ALERT_SUBSCRIBERS_PATH")
    return Path(configured) if configured else _default_subscribers_path()


def _load_subscriber_chat_ids(path: str | Path | None = None) -> list[str]:
    try:
        return _split_chat_ids(_subscribers_path(path).read_text())
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("telegram subscriber load failed: %s", e)
        return []


def _subscribe_chat_id(chat_id: str, path: str | Path | None = None) -> bool:
    chat_id = str(chat_id).strip()
    if not chat_id:
        return False
    p = _subscribers_path(path)
    current = _load_subscriber_chat_ids(p)
    if chat_id in current:
        return False
    current.append(chat_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text("\n".join(current) + "\n")
        tmp.replace(p)
    except Exception as e:
        log.warning("telegram subscriber persist failed: %s", e)
        return False
    return True


class TelegramNotifier:
    """Posts text to the admin chat plus optional alert-only subscribers."""

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        timeout: float = 5.0,
        include_alert_subscribers: bool = True,
        subscriber_path: str | Path | None = None,
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "")
        self.include_alert_subscribers = include_alert_subscribers
        self.subscriber_path = subscriber_path
        self._http = httpx.Client(timeout=timeout)
        self.enabled = bool(self.token and self._target_chat_ids())
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
        for chat_id in self._target_chat_ids():
            try:
                resp = self._http.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code >= 400:
                    log.warning("telegram %d to %s: %s", resp.status_code, chat_id, resp.text[:200])
            except Exception as e:
                # Swallow: a Telegram outage must not block trading.
                log.warning("telegram send to %s failed: %s", chat_id, e)

    def _target_chat_ids(self) -> list[str]:
        ids = _split_chat_ids(self.chat_id)
        if self.include_alert_subscribers:
            ids.extend(_split_chat_ids(os.environ.get("TELEGRAM_ALERT_CHAT_IDS")))
            ids.extend(_load_subscriber_chat_ids(self.subscriber_path))
        deduped: list[str] = []
        seen: set[str] = set()
        for chat_id in ids:
            if chat_id not in seen:
                deduped.append(chat_id)
                seen.add(chat_id)
        return deduped


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
        command_handlers: dict[str, Callable[[], str]] | None = None,
        allow_alert_subscribe: bool | None = None,
        subscriber_path: str | Path | None = None,
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "")
        self.kill_file = Path(kill_file)
        self.long_poll_seconds = long_poll_seconds
        self.allow_alert_subscribe = (
            _env_truthy("TELEGRAM_ALLOW_ALERT_SUBSCRIBE")
            if allow_alert_subscribe is None
            else allow_alert_subscribe
        )
        self.subscriber_path = subscriber_path
        self.enabled = bool(self.token and self.chat_id)
        self._stop = threading.Event()
        self._http: httpx.Client | None = None
        self._thread: threading.Thread | None = None
        self._offset = 0
        # Optional read-side handlers (e.g. /pnl, /trades). Keys are command
        # names without leading slash; values are zero-arg callables returning
        # a Markdown reply string. Exceptions are caught so a buggy handler
        # can't crash the listener.
        self.command_handlers: dict[str, Callable[[], str]] = command_handlers or {}

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
        text = (msg.get("text") or "").strip().lower()
        if (
            chat_id != self.chat_id
            and self.allow_alert_subscribe
            and text in ("/start", "start")
        ):
            added = _subscribe_chat_id(chat_id, self.subscriber_path)
            self._reply(
                "Subscribed to trading alerts." if added else "Already subscribed to trading alerts.",
                chat_id=chat_id,
            )
            log.info("telegram alert subscriber %s via /start", chat_id)
            return
        if chat_id != self.chat_id:
            log.warning("ignoring command from foreign chat_id=%s", chat_id)
            return

        # Custom handlers first (so caller can override e.g. /status if desired).
        cmd_key = text.lstrip("/")
        if cmd_key in self.command_handlers:
            try:
                reply = self.command_handlers[cmd_key]()
            except Exception as e:
                log.exception("command handler %r failed", cmd_key)
                reply = f"⚠️ `{cmd_key}` failed: {e}"
            self._reply(reply)
            return

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
            extras = "\n".join(f"  /{k}" for k in sorted(self.command_handlers))
            extras = ("\n" + extras) if extras else ""
            subscribe = "\n  /start — subscribe this chat to alerts" if self.allow_alert_subscribe else ""
            self._reply(
                "commands:\n"
                "  /kill   — halt new orders (touches .kill)\n"
                "  /status — report kill-switch state"
                f"{extras}{subscribe}\n"
                "to resume after /kill: SSH in and `rm .kill`"
            )

    def _reply(self, text: str, *, chat_id: str | None = None) -> None:
        if self._http is None:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            self._http.post(
                url,
                json={"chat_id": chat_id or self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            log.warning("kill listener reply failed: %s", e)
