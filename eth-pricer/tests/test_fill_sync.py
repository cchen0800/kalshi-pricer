from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from src.db import connect
from src.fill_sync import FillSyncer


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "test.db")
    yield conn
    conn.close()


class FakeTrader:
    def __init__(self, fills: list[dict]) -> None:
        self.fills = fills

    def get_fills(self, *, limit: int = 200) -> dict:
        return {"fills": self.fills[:limit]}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        return {"settlements": []}


class FakeNotifier:
    enabled = True

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


def _insert_intent(db: sqlite3.Connection, *, order_id: str = "order-1") -> None:
    db.execute(
        """
        INSERT INTO intended_orders (
            ts_ms, mode, event_ticker, market_ticker, side, action,
            limit_price_cents, count, notional_usd, model_prob,
            model_prob_calibrated, edge_cents, minutes_left, spot,
            client_order_id, status, kalshi_order_id, bot_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(time.time() * 1000), "live", "KXETHD-26MAY1315",
            "KXETHD-26MAY1315-T2250", "no", "buy",
            66, 10, 6.60, 0.267, None, 5.3, 51.0, 2_244.0,
            "ethp-client-1", "submitted", order_id, "eth-selective",
        ),
    )


def test_fill_sync_notifies_new_matched_fill_once(db):
    _insert_intent(db)
    notifier = FakeNotifier()
    fill = {
        "trade_id": "trade-1",
        "order_id": "order-1",
        "ticker": "KXETHD-26MAY1315-T2250",
        "side": "no",
        "action": "buy",
        "count": 10,
        "no_price_dollars": 0.66,
        "fee_cost": "0.12",
        "created_time": "2026-05-13T21:03:00Z",
    }
    syncer = FillSyncer(FakeTrader([fill]), interval_s=0, notifier=notifier)
    syncer._notify_after_ts_ms = 0

    syncer.maybe_sync(db)
    syncer.maybe_sync(db)

    assert len(notifier.messages) == 1
    msg = notifier.messages[0]
    assert "BUY NO FILLED" in msg
    assert "10 x 66¢" in msg
    assert "submitted" not in msg.lower()


def test_fill_sync_does_not_notify_unmatched_fill(db):
    notifier = FakeNotifier()
    fill = {
        "trade_id": "manual-trade",
        "order_id": "manual-order",
        "ticker": "KXETHD-26MAY1315-T2250",
        "side": "yes",
        "action": "buy",
        "count": 1,
        "yes_price_dollars": 0.20,
        "created_time": "2026-05-13T21:03:00Z",
    }
    syncer = FillSyncer(FakeTrader([fill]), interval_s=0, notifier=notifier)

    syncer.maybe_sync(db)

    assert notifier.messages == []
