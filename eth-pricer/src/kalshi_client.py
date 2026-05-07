"""Read-only Kalshi API client.

Auth: RSA-PSS-SHA256 signature over `{ts_ms}{method}{full_path}` (path includes
`/trade-api/v2` prefix, no query string), per Kalshi's API key spec.

Safety: this client only exposes GET helpers — no order-placement endpoints.
Any caller passing a non-GET method to `_request` will trigger an assertion.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

DEFAULT_HOST = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"


class KalshiClient:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        host: str = DEFAULT_HOST,
        timeout: float = 10.0,
    ) -> None:
        load_dotenv()
        self.key_id = key_id or os.environ["KALSHI_KEY_ID"]
        pk_path = private_key_path or os.environ["KALSHI_PRIVATE_KEY_PATH"]
        with open(pk_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        self.host = host.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _sign(self, ts_ms: str, method: str, full_path: str) -> str:
        msg = f"{ts_ms}{method}{full_path}".encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict:
        assert method == "GET", f"read-only client; refusing {method}"
        rel = path if path.startswith("/") else f"/{path}"
        full_path = f"{API_PREFIX}{rel}"
        url = f"{self.host}{full_path}"

        for attempt in range(4):
            ts_ms = str(int(time.time() * 1000))
            headers = {
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts_ms,
                "KALSHI-ACCESS-SIGNATURE": self._sign(ts_ms, method, full_path),
                "Accept": "application/json",
            }
            try:
                resp = self._http.request(method, url, params=params, headers=headers)
            except httpx.RequestError:
                if attempt == 3:
                    raise
                time.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 1.0))
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600:
                if attempt == 3:
                    resp.raise_for_status()
                time.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.reason_phrase}: {resp.text[:500]}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()
        raise RuntimeError("retry budget exhausted")

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_event(self, event_ticker: str) -> dict:
        return self._request("GET", f"/events/{event_ticker}")

    def list_events(
        self,
        series_ticker: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return self._request("GET", "/events", params=params)

    def list_markets(
        self,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        return self._request("GET", "/markets", params=params)
