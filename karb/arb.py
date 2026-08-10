"""Cycle evaluation: walk real book depth, charge real fees, report net edge.

The reason naive triangular scanners produce phantom profit is that they price
every leg at top-of-book and ignore that the top level rarely holds enough size.
Everything here consumes actual levels and refuses to report a number it cannot
fill.

Fee handling: Kraken takes its taker fee in the quote currency. Charging
`(1 - fee)` against the *output* of every leg is exact for sells and very
slightly pessimistic for buys (1 - f < 1 / (1 + f)), which is the direction of
error worth having.
"""

from __future__ import annotations

from dataclasses import dataclass

from .universe import Cycle, Leg

# Floating point slop tolerated when deciding whether a level fully filled.
_EPS = 1e-12


@dataclass
class LegFill:
    leg: Leg
    amount_in: float
    amount_out: float
    worst_price: float  # the price of the deepest level touched
    volume_base: float  # what AddOrder's `volume` parameter would be


@dataclass
class Evaluation:
    cycle: Cycle
    size: float  # start-asset amount put in
    end_amount: float  # start-asset amount returned
    fills: list[LegFill]
    oldest_book_ms: float

    @property
    def profit(self) -> float:
        return self.end_amount - self.size

    @property
    def profit_bps(self) -> float:
        if self.size <= 0:
            return 0.0
        return (self.end_amount / self.size - 1.0) * 10_000.0


def consume_asks(asks: list[tuple[float, float]], quote_in: float, fee: float):
    """Spend `quote_in` buying into the ask side. Returns (base_out, worst_price)."""
    base_out = 0.0
    remaining = quote_in
    worst = 0.0
    for price, qty in asks:
        if price <= 0 or qty <= 0:
            continue
        take = min(remaining, price * qty)
        base_out += take / price
        remaining -= take
        worst = price
        if remaining <= _EPS:
            break
    if remaining > _EPS:
        return None  # not enough depth in the tracked window
    return base_out * (1.0 - fee), worst


def consume_bids(bids: list[tuple[float, float]], base_in: float, fee: float):
    """Sell `base_in` into the bid side. Returns (quote_out, worst_price)."""
    quote_out = 0.0
    remaining = base_in
    worst = 0.0
    for price, qty in bids:
        if price <= 0 or qty <= 0:
            continue
        take = min(remaining, qty)
        quote_out += take * price
        remaining -= take
        worst = price
        if remaining <= _EPS:
            break
    if remaining > _EPS:
        return None
    return quote_out * (1.0 - fee), worst


def evaluate(cycle: Cycle, store, size: float, fee_override: float | None, max_book_age_ms: float) -> Evaluation | None:
    """Run `size` of the start asset around the cycle. None if it can't fill."""
    amount = size
    fills: list[LegFill] = []
    oldest = 0.0

    for leg in cycle.legs:
        book = store.snapshot(leg.pair.wsname)
        if book is None or not book.bids or not book.asks:
            return None
        age = book.age_ms
        if age > max_book_age_ms:
            return None
        oldest = max(oldest, age)

        fee = leg.pair.taker_fee if fee_override is None else fee_override

        if leg.side == "buy":
            result = consume_asks(book.asks, amount, fee)
            if result is None:
                return None
            out, worst = result
            volume_base = out / (1.0 - fee) if fee < 1.0 else out
        else:
            result = consume_bids(book.bids, amount, fee)
            if result is None:
                return None
            out, worst = result
            volume_base = amount

        fills.append(
            LegFill(leg=leg, amount_in=amount, amount_out=out, worst_price=worst, volume_base=volume_base)
        )
        amount = out

    return Evaluation(cycle=cycle, size=size, end_amount=amount, fills=fills, oldest_book_ms=oldest)


def max_fillable_size(cycle: Cycle, store, ceiling: float, fee_override: float, max_book_age_ms: float) -> float:
    """Largest start size the tracked depth can absorb, via bisection.

    Feasibility is monotone in size — if `x` fills, anything smaller does too —
    so bisection is exact to the tolerance we stop at.
    """
    if evaluate(cycle, store, ceiling, fee_override, max_book_age_ms) is not None:
        return ceiling
    low, high = 0.0, ceiling
    for _ in range(24):
        mid = (low + high) / 2.0
        if evaluate(cycle, store, mid, fee_override, max_book_age_ms) is not None:
            low = mid
        else:
            high = mid
    return low


def scan(cycles, store, size: float, fee_override: float | None, max_book_age_ms: float) -> list[Evaluation]:
    """Evaluate every cycle at `size`, best edge first."""
    results = []
    for cycle in cycles:
        evaluation = evaluate(cycle, store, size, fee_override, max_book_age_ms)
        if evaluation is not None:
            results.append(evaluation)
    results.sort(key=lambda item: item.profit_bps, reverse=True)
    return results


def meets_minimums(evaluation: Evaluation) -> tuple[bool, str]:
    """Kraken rejects orders below a pair's ordermin/costmin — catch it here."""
    for fill in evaluation.fills:
        pair = fill.leg.pair
        if pair.ordermin and fill.volume_base < pair.ordermin:
            return False, f"{pair.wsname} volume {fill.volume_base:.8f} below ordermin {pair.ordermin}"
        if pair.costmin:
            cost = fill.amount_in if fill.leg.side == "buy" else fill.amount_out
            if cost < pair.costmin:
                return False, f"{pair.wsname} cost {cost:.6f} below costmin {pair.costmin}"
    return True, ""
