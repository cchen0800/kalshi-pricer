"""actionable_edge returns net-of-fee edge — these tests lock that contract."""

from __future__ import annotations

import math
import time

import pytest

from src.db import PollRow
from src.engine import actionable_edge
from src.positions import kalshi_fee_cents


def make_row(*, model_prob: float, yes_bid: float | None, yes_ask: float | None) -> PollRow:
    return PollRow(
        ts_ms=int(time.time() * 1000),
        event_ticker="KXBTCD-26MAY0113",
        market_ticker="KXBTCD-26MAY0113-T80000",
        strike=80_000.0,
        spot=80_000.0,
        sigma=0.50,
        minutes_left=30.0,
        model_prob=model_prob,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=100,
        yes_ask_size=100,
        volume=1000,
        edge_cents=0.0,
    )


def test_buy_edge_subtracts_fee():
    # Model 80c, ask 25c → gross 55c. Fee at 25c = ceil(0.07 * .25 * .75 * 100) = 2c.
    row = make_row(model_prob=0.80, yes_bid=0.20, yes_ask=0.25)
    side, edge = actionable_edge(row)
    assert side == "BUY_YES"
    expected_fee = kalshi_fee_cents(25, 1)
    assert edge == pytest.approx(55.0 - expected_fee)


def test_sell_edge_subtracts_fee():
    # Model 10c, bid 50c → gross sell 40c. Fee at 50c = ceil(0.07*.5*.5*100) = 2c.
    row = make_row(model_prob=0.10, yes_bid=0.50, yes_ask=0.55)
    side, edge = actionable_edge(row)
    assert side == "SELL_YES"
    expected_fee = kalshi_fee_cents(50, 1)
    assert edge == pytest.approx(40.0 - expected_fee)


def test_fee_kills_marginal_edge_at_50_50():
    # At a 50/50 contract the fee is largest (~2c). A 1c gross edge becomes negative net.
    row = make_row(model_prob=0.51, yes_bid=0.49, yes_ask=0.50)
    side, edge = actionable_edge(row)
    # Gross buy = 1c, fee at 50c = 2c → net -1c. Gross sell = -2c. Both negative → NONE.
    assert side == "NONE"
    assert edge == 0.0


def test_no_book_returns_none():
    row = make_row(model_prob=0.50, yes_bid=None, yes_ask=None)
    side, edge = actionable_edge(row)
    assert side == "NONE"
    assert edge == 0.0


def test_picks_larger_side():
    # Symmetric model, asymmetric book: ask much wider than bid → sell side bigger.
    row = make_row(model_prob=0.50, yes_bid=0.65, yes_ask=0.95)
    side, _edge = actionable_edge(row)
    assert side == "SELL_YES"
