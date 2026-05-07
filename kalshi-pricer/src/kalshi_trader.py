"""Write-enabled Kalshi client. Used only by the executor.

Separate file from kalshi_client.py so the read-only assertion there stays
intact for the engine. Anything that POSTs lives here.

Auth: same RSA-PSS-SHA256 over `{ts_ms}{method}{full_path}` as the read client.
The body is not signed (Kalshi signs path only).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

log = logging.getLogger("kalshi_trader")

PROD_HOST = "https://api.elections.kalshi.com"
DEMO_HOST = "https://demo-api.kalshi.co"
API_PREFIX = "/trade-api/v2"

ALLOWED_METHODS = {"GET", "POST", "DELETE"}


class KalshiTrader:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        host: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        load_dotenv()
        self.key_id = key_id or os.environ["KALSHI_KEY_ID"]
        pk_path = private_key_path or os.environ["KALSHI_PRIVATE_KEY_PATH"]
        with open(pk_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        self.host = (host or os.environ.get("KALSHI_HOST", PROD_HOST)).rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def __enter__(self) -> "KalshiTrader":
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

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"unsupported method: {method}")
        rel = path if path.startswith("/") else f"/{path}"
        full_path = f"{API_PREFIX}{rel}"
        url = f"{self.host}{full_path}"

        # Orders are not safely retriable: a 5xx after the order was accepted
        # would double up. So GET retries up to 3, POST/DELETE retry 0.
        max_attempts = 4 if method == "GET" else 1
        for attempt in range(max_attempts):
            ts_ms = str(int(time.time() * 1000))
            headers = {
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts_ms,
                "KALSHI-ACCESS-SIGNATURE": self._sign(ts_ms, method, full_path),
                "Accept": "application/json",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                resp = self._http.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    content=json.dumps(body) if body is not None else None,
                )
            except httpx.RequestError:
                if attempt == max_attempts - 1:
                    raise
                time.sleep(0.5 * (2**attempt))
                continue

            if resp.status_code == 429 and method == "GET":
                wait = float(resp.headers.get("Retry-After", 1.0))
                time.sleep(wait)
                continue
            if 500 <= resp.status_code < 600 and method == "GET":
                if attempt == max_attempts - 1:
                    resp.raise_for_status()
                time.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.reason_phrase}: {resp.text[:500]}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json() if resp.content else {}
        raise RuntimeError("retry budget exhausted")

    # ---- read endpoints (parity with KalshiClient, useful for sanity checks) ----

    def get_balance(self) -> dict:
        """Returns {balance: cents, ...}. Use to sanity-check before live trading."""
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        return self._request("GET", "/portfolio/positions")

    def get_orders(self, status: str | None = None) -> dict:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        return self._request("GET", "/portfolio/orders", params=params)

    def get_fills(self, ticker: str | None = None, limit: int = 100) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._request("GET", "/portfolio/fills", params=params)

    # ---- write endpoints ----

    def place_order(
        self,
        *,
        ticker: str,
        client_order_id: str,
        side: str,           # 'yes' | 'no'
        action: str,         # 'buy' | 'sell'
        count: int,
        limit_price_cents: int,
    ) -> dict:
        """Place a limit order. Crossing limits act as taker, non-crossing rest.

        We always use limit orders (never market) to bound execution price.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
        if not (1 <= limit_price_cents <= 99):
            raise ValueError(f"limit_price_cents must be 1..99, got {limit_price_cents}")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")

        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "action": action,
            "type": "limit",
            "count": count,
        }
        # Kalshi expects yes_price for yes-side orders, no_price for no-side.
        if side == "yes":
            body["yes_price"] = limit_price_cents
        else:
            body["no_price"] = limit_price_cents
        return self._request("POST", "/portfolio/orders", body=body)

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
