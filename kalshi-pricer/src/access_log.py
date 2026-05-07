"""HTTP access log + per-visit Telegram ping for the dashboard.

Writes one JSON line per request to `logs/access.log`. Pings Telegram on
every page load of `notify_path` (default `/`), regardless of whether
the IP has been seen before. The set of unique IPs is still tracked in
`logs/seen_ips.json` so `/api/visitors` can return a cumulative count.

Failsafe: any Telegram or filesystem error is logged at WARNING and
swallowed. Access logging must never break the dashboard.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("access_log")


def client_ip_from_headers(headers: Any, fallback: str) -> str:
    """Resolve the real client IP when behind a reverse proxy (Caddy/nginx).

    `headers` is anything with case-insensitive `.get(name)`; Starlette's
    `Headers` qualifies. Falls back to the direct socket peer if no proxy
    header is present.
    """
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real = headers.get("x-real-ip")
    if real:
        return real.strip()
    return fallback or ""


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "localhost", "")


class AccessLogger:
    """File-backed access log with a Telegram ping on every page load.

    `label` is the prefix shown in Telegram pings (e.g. "kalshi-pricer")
    so a single bot serving both BTC and ETH dashboards can disambiguate.
    `notify_path` is the request path that triggers a ping — defaults to
    "/" so the per-second `/api/state` / `/api/spot` polling from the
    dashboard's own JS doesn't drown Telegram in messages.
    """

    def __init__(
        self,
        log_dir: Path,
        label: str,
        notifier: Any = None,
        notify_path: str = "/",
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.access_path = self.log_dir / "access.log"
        self.seen_path = self.log_dir / "seen_ips.json"
        self.label = label
        self.notifier = notifier
        self.notify_path = notify_path
        self._lock = threading.Lock()
        self._seen: set[str] = self._load_seen()

    def _load_seen(self) -> set[str]:
        try:
            if self.seen_path.exists():
                return set(json.loads(self.seen_path.read_text()))
        except Exception as e:
            log.warning("could not load seen_ips.json: %s", e)
        return set()

    def _persist_seen(self) -> None:
        try:
            tmp = self.seen_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sorted(self._seen)))
            tmp.replace(self.seen_path)
        except Exception as e:
            log.warning("could not persist seen_ips.json: %s", e)

    def record(
        self,
        ip: str,
        method: str,
        path: str,
        status: int,
        user_agent: str,
        referer: str,
        duration_ms: float,
    ) -> None:
        entry = {
            "ts": int(time.time() * 1000),
            "ip": ip,
            "method": method,
            "path": path,
            "status": status,
            "ua": (user_agent or "")[:200],
            "ref": (referer or "")[:200],
            "ms": round(duration_ms, 1),
        }
        with self._lock:
            try:
                with self.access_path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                log.warning("access log write failed: %s", e)

            is_new = bool(ip) and ip not in self._seen
            if is_new:
                self._seen.add(ip)
                self._persist_seen()

        # Fire Telegram outside the lock so a slow POST can't stall other
        # request threads waiting to log. Ping on every page load — the
        # `notify_path` filter (default `/`) is what stops the API polling
        # from spamming.
        if (
            path == self.notify_path
            and self.notifier is not None
            and getattr(self.notifier, "enabled", False)
            and not _is_loopback(ip)
        ):
            self._notify(ip, user_agent, referer, is_new)

    def _notify(self, ip: str, ua: str, ref: str, is_new: bool) -> None:
        try:
            tag = "new visitor" if is_new else "visit"
            text = (
                f"👀 *{self.label}* {tag}\n"
                f"`{ip}`\n"
                f"UA: `{(ua or '-')[:120]}`"
            )
            if ref:
                text += f"\nref: `{ref[:120]}`"
            self.notifier.send(text)
        except Exception as e:
            log.warning("visitor notify failed: %s", e)

    def recent(self, limit: int = 200) -> list[dict]:
        """Return the last `limit` log lines, parsed. Best-effort: skips
        any unparseable line so a corrupt write can't break the endpoint.
        """
        try:
            if not self.access_path.exists():
                return []
            with self.access_path.open() as f:
                lines = f.readlines()
        except Exception as e:
            log.warning("recent read failed: %s", e)
            return []
        out: list[dict] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def unique_ips(self) -> list[str]:
        with self._lock:
            return sorted(self._seen)
