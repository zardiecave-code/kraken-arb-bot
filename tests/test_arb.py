"""Unit tests for the cycle math. No network, no keys.

    python -m pytest tests -q      (or: python tests/test_arb.py)
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from karb import arb  # noqa: E402
from karb.execute import round_price, round_volume  # noqa: E402
from karb.universe import Cycle, Leg, Pair, build_cycles, ws_v2_name  # noqa: E402


class FakeBook:
    def __init__(self, bids, asks, age_ms=0.0):
        self.bids, self.asks = bids, asks
        self.updated_at = time.time() - age_ms / 1000.0

    @property
    def age_ms(self):
        return (time.time() - self.updated_at) * 1000.0


class FakeStore:
    def __init__(self, books):
        self.books = books

    def snapshot(self, symbol):
        return self.books.get(symbol)


def make_pair(wsname, base, quote, fee=0.0, ordermin=0.0, costmin=0.0):
    return Pair(
        altname=wsname.replace("/", ""),
        wsname=wsname,
        base=base,
        quote=quote,
        lot_decimals=8,
        pair_decimals=2,
        ordermin=ordermin,
        costmin=costmin,
        taker_fee=fee,
    )


def triangle(fee=0.0):
    btc_usd = make_pair("BTC/USD", "BTC", "USD", fee)
    eth_btc = make_pair("ETH/BTC", "ETH", "BTC", fee)
    eth_usd = make_pair("ETH/USD", "ETH", "USD", fee)
    return Cycle(
        legs=(
            Leg(btc_usd, "buy", "USD", "BTC"),  # USD -> BTC
            Leg(eth_btc, "buy", "BTC", "ETH"),  # BTC -> ETH
            Leg(eth_usd, "sell", "ETH", "USD"),  # ETH -> USD
        )
    )


# ------------------------------------------------------------------ depth walk


def test_consume_asks_spans_multiple_levels():
    # 100 quote: 50 fills level one entirely, 50 goes into level two.
    asks = [(100.0, 0.5), (200.0, 5.0)]
    out, worst = arb.consume_asks(asks, 100.0, fee=0.0)
    assert abs(out - (0.5 + 0.25)) < 1e-12
    assert worst == 200.0


def test_consume_asks_returns_none_when_depth_runs_out():
    assert arb.consume_asks([(100.0, 0.5)], 100.0, fee=0.0) is None


def test_consume_bids_applies_fee_to_proceeds():
    out, worst = arb.consume_bids([(100.0, 1.0)], 1.0, fee=0.01)
    assert abs(out - 99.0) < 1e-12
    assert worst == 100.0


def test_zero_and_negative_levels_are_ignored():
    out, _ = arb.consume_asks([(0.0, 5.0), (100.0, 2.0)], 100.0, fee=0.0)
    assert abs(out - 1.0) < 1e-12


# -------------------------------------------------------------- cycle results


def test_flat_market_with_no_fees_returns_exactly_the_stake():
    # 1 BTC = 100 USD, 1 ETH = 0.5 BTC, 1 ETH = 50 USD -> perfectly consistent.
    store = FakeStore(
        {
            "BTC/USD": FakeBook(bids=[(100.0, 100.0)], asks=[(100.0, 100.0)]),
            "ETH/BTC": FakeBook(bids=[(0.5, 100.0)], asks=[(0.5, 100.0)]),
            "ETH/USD": FakeBook(bids=[(50.0, 100.0)], asks=[(50.0, 100.0)]),
        }
    )
    result = arb.evaluate(triangle(), store, 100.0, fee_override=0.0, max_book_age_ms=10_000)
    assert result is not None
    assert abs(result.end_amount - 100.0) < 1e-9
    assert abs(result.profit_bps) < 1e-6


def test_mispriced_leg_produces_positive_edge():
    # ETH/USD bid lifted to 55: the loop now returns more than it started with.
    store = FakeStore(
        {
            "BTC/USD": FakeBook(bids=[(100.0, 100.0)], asks=[(100.0, 100.0)]),
            "ETH/BTC": FakeBook(bids=[(0.5, 100.0)], asks=[(0.5, 100.0)]),
            "ETH/USD": FakeBook(bids=[(55.0, 100.0)], asks=[(55.0, 100.0)]),
        }
    )
    result = arb.evaluate(triangle(), store, 100.0, fee_override=0.0, max_book_age_ms=10_000)
    assert abs(result.end_amount - 110.0) < 1e-9
    assert abs(result.profit_bps - 1000.0) < 1e-6


def test_fees_erase_a_thin_edge():
    """The whole point of the bot: 10bps gross does not survive 26bps x 3."""
    store = FakeStore(
        {
            "BTC/USD": FakeBook(bids=[(100.0, 100.0)], asks=[(100.0, 100.0)]),
            "ETH/BTC": FakeBook(bids=[(0.5, 100.0)], asks=[(0.5, 100.0)]),
            "ETH/USD": FakeBook(bids=[(50.05, 100.0)], asks=[(50.05, 100.0)]),
        }
    )
    gross = arb.evaluate(triangle(), store, 100.0, fee_override=0.0, max_book_age_ms=10_000)
    net = arb.evaluate(triangle(), store, 100.0, fee_override=0.0026, max_book_age_ms=10_000)
    assert gross.profit_bps > 0
    assert net.profit_bps < 0
    assert abs(net.profit_bps - (-68.0)) < 2.0  # ~10bps gross - ~78bps of fees


def test_stale_book_is_rejected():
    store = FakeStore(
        {
            "BTC/USD": FakeBook(bids=[(100.0, 100.0)], asks=[(100.0, 100.0)], age_ms=5000),
            "ETH/BTC": FakeBook(bids=[(0.5, 100.0)], asks=[(0.5, 100.0)]),
            "ETH/USD": FakeBook(bids=[(50.0, 100.0)], asks=[(50.0, 100.0)]),
        }
    )
    assert arb.evaluate(triangle(), store, 100.0, fee_override=0.0, max_book_age_ms=1000) is None


def test_thin_depth_makes_the_cycle_unfillable():
    store = FakeStore(
        {
            "BTC/USD": FakeBook(bids=[(100.0, 0.01)], asks=[(100.0, 0.01)]),  # only 1 USD deep
            "ETH/BTC": FakeBook(bids=[(0.5, 100.0)], asks=[(0.5, 100.0)]),
            "ETH/USD": FakeBook(bids=[(50.0, 100.0)], asks=[(50.0, 100.0)]),
        }
    )
    assert arb.evaluate(triangle(), store, 100.0, fee_override=0.0, max_book_age_ms=10_000) is None


def test_max_fillable_size_finds_the_depth_ceiling():
    store = FakeStore(
        {
            "BTC/USD": FakeBook(bids=[(100.0, 1.0)], asks=[(100.0, 1.0)]),  # 100 USD of depth
            "ETH/BTC": FakeBook(bids=[(0.5, 100.0)], asks=[(0.5, 100.0)]),
            "ETH/USD": FakeBook(bids=[(50.0, 100.0)], asks=[(50.0, 100.0)]),
        }
    )
    size = arb.max_fillable_size(triangle(), store, 1000.0, 0.0, 10_000)
    assert 99.0 < size <= 100.0


def test_missing_symbol_yields_no_evaluation():
    store = FakeStore({"BTC/USD": FakeBook(bids=[(100.0, 10.0)], asks=[(100.0, 10.0)])})
    assert arb.evaluate(triangle(), store, 10.0, fee_override=0.0, max_book_age_ms=10_000) is None


def test_ordermin_blocks_a_dust_cycle():
    cycle = Cycle(
        legs=(
            Leg(make_pair("BTC/USD", "BTC", "USD", ordermin=1.0), "buy", "USD", "BTC"),
            Leg(make_pair("ETH/BTC", "ETH", "BTC"), "buy", "BTC", "ETH"),
            Leg(make_pair("ETH/USD", "ETH", "USD"), "sell", "ETH", "USD"),
        )
    )
    store = FakeStore(
        {
            "BTC/USD": FakeBook(bids=[(100.0, 100.0)], asks=[(100.0, 100.0)]),
            "ETH/BTC": FakeBook(bids=[(0.5, 100.0)], asks=[(0.5, 100.0)]),
            "ETH/USD": FakeBook(bids=[(50.0, 100.0)], asks=[(50.0, 100.0)]),
        }
    )
    result = arb.evaluate(cycle, store, 100.0, fee_override=0.0, max_book_age_ms=10_000)
    ok, why = arb.meets_minimums(result)  # buys 1.0 BTC with 100 USD, ordermin is 1.0
    assert ok, why

    result_small = arb.evaluate(cycle, store, 10.0, fee_override=0.0, max_book_age_ms=10_000)
    ok, why = arb.meets_minimums(result_small)  # 0.1 BTC, below ordermin
    assert not ok and "ordermin" in why


# ------------------------------------------------------------------- universe


def test_build_cycles_finds_both_directions():
    pairs = {
        "BTC/USD": make_pair("BTC/USD", "BTC", "USD"),
        "ETH/BTC": make_pair("ETH/BTC", "ETH", "BTC"),
        "ETH/USD": make_pair("ETH/USD", "ETH", "USD"),
    }
    cycles = build_cycles(pairs, ["USD"])
    paths = {cycle.path for cycle in cycles}
    assert "USD -> BTC -> ETH -> USD" in paths
    assert "USD -> ETH -> BTC -> USD" in paths
    assert len(cycles) == 2
    assert all(cycle.start_asset == "USD" for cycle in cycles)


def test_ws_v2_name_translates_legacy_asset_codes():
    # AssetPairs still reports v1 names; the v2 book feed rejects those.
    assert ws_v2_name("XBT/USD") == "BTC/USD"
    assert ws_v2_name("ETH/XBT") == "ETH/BTC"
    assert ws_v2_name("XDG/USD") == "DOGE/USD"
    assert ws_v2_name("ETH/USD") == "ETH/USD"  # untouched when already correct


def test_build_cycles_returns_nothing_without_a_closing_leg():
    pairs = {
        "BTC/USD": make_pair("BTC/USD", "BTC", "USD"),
        "ETH/BTC": make_pair("ETH/BTC", "ETH", "BTC"),
    }
    assert build_cycles(pairs, ["USD"]) == []


# ------------------------------------------------------------------- rounding


def test_volume_always_rounds_down():
    assert round_volume(1.999999999, 8) == "1.99999999"
    assert round_volume(0.000000009, 8) == "0.00000000"


def test_price_rounds_in_the_crossing_direction():
    assert round_price(100.001, 2, "buy") == "100.01"  # up, so the buy still crosses
    assert round_price(100.009, 2, "sell") == "100.00"  # down, so the sell still crosses


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
