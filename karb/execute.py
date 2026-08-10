"""Leg-by-leg execution of a cycle.

A triangular cycle is not atomic. Three orders go out in sequence and the market
can move between any two of them, so the only honest way to run one is to size
each leg from what the *previous* leg actually returned, never from what the
scan predicted. That is what this module does.

Every leg is an immediate-or-cancel limit order priced through the level the
scan expected to reach, plus a slippage buffer. IOC means a leg either crosses
now or it cancels — it never rests on the book turning an arbitrage into an
accidental directional position.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .kraken import KrakenError

_TERMINAL = ("closed", "canceled", "expired")


def round_volume(volume: float, decimals: int) -> str:
    """Always round volume down — never try to sell more than you hold.

    Formatted with ``:f`` rather than ``str()``: Decimal renders small
    quantities in exponential notation ("0E-8", "9E-9") and Kraken rejects
    those outright.
    """
    quantum = Decimal(1).scaleb(-decimals)
    return f"{Decimal(str(volume)).quantize(quantum, rounding=ROUND_FLOOR):f}"


def round_price(price: float, decimals: int, side: str) -> str:
    """Round in the direction that still crosses the spread."""
    quantum = Decimal(1).scaleb(-decimals)
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    return f"{Decimal(str(price)).quantize(quantum, rounding=rounding):f}"


@dataclass
class LegResult:
    pair: str
    side: str
    txid: str = ""
    requested_volume: float = 0.0
    filled_volume: float = 0.0
    amount_in: float = 0.0
    amount_out: float = 0.0
    fee: float = 0.0
    status: str = ""

    @property
    def filled(self) -> bool:
        return self.filled_volume > 0


@dataclass
class CycleResult:
    path: str
    size: float
    end_amount: float = 0.0
    legs: list[LegResult] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    stranded_asset: str = ""
    stranded_amount: float = 0.0

    @property
    def pnl(self) -> float:
        return self.end_amount - self.size


class Executor:
    def __init__(self, client, config, notify):
        self.client = client
        self.cfg = config
        self.notify = notify

    # ------------------------------------------------------------------ public

    def run_cycle(self, evaluation) -> CycleResult:
        cycle = evaluation.cycle
        result = CycleResult(path=cycle.path, size=evaluation.size)
        amount = evaluation.size
        current_asset = cycle.start_asset

        for index, expected in enumerate(evaluation.fills):
            leg = expected.leg
            try:
                leg_result = self._run_leg(leg, amount, expected)
            except KrakenError as exc:
                result.error = f"leg {index + 1} ({leg.pair.wsname} {leg.side}) failed: {exc}"
                result.stranded_asset, result.stranded_amount = current_asset, amount
                break

            result.legs.append(leg_result)

            if not leg_result.filled:
                result.error = f"leg {index + 1} ({leg.pair.wsname} {leg.side}) did not fill: {leg_result.status}"
                result.stranded_asset, result.stranded_amount = current_asset, amount
                break

            amount = leg_result.amount_out
            current_asset = leg.to_asset
        else:
            result.ok = True
            result.end_amount = amount
            return result

        # Fell out of the loop early: we are holding something that is not the
        # start asset, or we never left it.
        if result.stranded_asset and result.stranded_asset != cycle.start_asset:
            if self.cfg.unwind_on_failure:
                self._unwind(result, cycle)
            else:
                self.notify.error(
                    f"UNWIND DISABLED — holding {result.stranded_amount:.8f} {result.stranded_asset}. "
                    "This is now a directional position. Close it manually."
                )
        elif result.stranded_asset == cycle.start_asset:
            # Nothing moved; the start capital is intact.
            result.end_amount = result.stranded_amount
            result.stranded_asset, result.stranded_amount = "", 0.0

        return result

    # ------------------------------------------------------------------- legs

    def _run_leg(self, leg, amount_in: float, expected) -> LegResult:
        pair = leg.pair
        buffer = self.cfg.slippage_bps / 10_000.0

        if leg.side == "buy":
            limit_price = expected.worst_price * (1.0 + buffer)
            volume = amount_in / limit_price
        else:
            limit_price = expected.worst_price * (1.0 - buffer)
            volume = amount_in

        price_str = round_price(limit_price, pair.pair_decimals, leg.side)
        volume_str = round_volume(volume, pair.lot_decimals)

        result = LegResult(
            pair=pair.wsname,
            side=leg.side,
            requested_volume=float(volume_str),
            amount_in=amount_in,
        )

        if float(volume_str) <= 0:
            result.status = "volume rounded to zero"
            return result
        if pair.ordermin and float(volume_str) < pair.ordermin:
            result.status = f"below ordermin {pair.ordermin}"
            return result

        response = self.client.add_order(
            pair=pair.altname,
            side=leg.side,
            ordertype="limit",
            volume=volume_str,
            price=price_str,
            timeinforce="IOC",
            userref=self.cfg.userref,
            validate=not self.cfg.live,
        )

        if not self.cfg.live:
            # Kraken accepted the parameters but placed nothing. Model the fill
            # from the scan so the run still exercises the full code path.
            result.status = "paper"
            result.txid = "PAPER"
            result.filled_volume = float(volume_str)
            result.amount_out = expected.amount_out
            return result

        txids = response.get("txid") or []
        if not txids:
            result.status = "no txid returned"
            return result
        result.txid = txids[0]

        order = self._await_fill(result.txid)
        result.status = order.get("status", "unknown")
        vol_exec = float(order.get("vol_exec", 0) or 0)
        cost = float(order.get("cost", 0) or 0)
        fee = float(order.get("fee", 0) or 0)
        result.filled_volume = vol_exec
        result.fee = fee

        if vol_exec <= 0:
            result.amount_out = 0.0
            return result

        if leg.side == "buy":
            # Bought `vol_exec` base; the quote actually spent was cost + fee.
            result.amount_in = cost + fee
            result.amount_out = vol_exec
        else:
            # Sold `vol_exec` base for `cost` quote, fee deducted from proceeds.
            result.amount_in = vol_exec
            result.amount_out = cost - fee

        return result

    def _await_fill(self, txid: str, timeout: float = 10.0) -> dict:
        """IOC resolves immediately, but the order record can lag a beat."""
        deadline = time.time() + timeout
        order: dict = {}
        while time.time() < deadline:
            orders = self.client.query_orders(txid)
            order = orders.get(txid) or {}
            if order.get("status") in _TERMINAL:
                return order
            time.sleep(0.25)
        self.notify.warn(f"order {txid} still {order.get('status', 'unknown')} after {timeout:.0f}s")
        return order

    # ----------------------------------------------------------------- unwind

    def _unwind(self, result: CycleResult, cycle) -> None:
        """Market out of a stranded asset, back toward the start asset."""
        asset, amount = result.stranded_asset, result.stranded_amount
        leg = next((leg for leg in cycle.legs if leg.from_asset == asset), None)
        if leg is None:
            self.notify.error(f"cannot unwind {amount:.8f} {asset}: no leg in the cycle spends it. Close manually.")
            return

        pair = leg.pair
        volume = amount if leg.side == "sell" else amount / max(result.legs[-1].amount_out or 1.0, 1e-12)
        volume_str = round_volume(volume, pair.lot_decimals)

        self.notify.warn(f"unwinding {volume_str} via {pair.wsname} {leg.side} (market)")
        try:
            response = self.client.add_order(
                pair=pair.altname,
                side=leg.side,
                ordertype="market",
                volume=volume_str,
                userref=self.cfg.userref,
                validate=not self.cfg.live,
            )
        except KrakenError as exc:
            self.notify.error(f"UNWIND FAILED for {amount:.8f} {asset}: {exc}. Close this position manually.")
            return

        if not self.cfg.live:
            self.notify.info("unwind validated (paper mode, nothing placed)")
            return

        txids = response.get("txid") or []
        if txids:
            order = self._await_fill(txids[0])
            recovered = float(order.get("cost", 0) or 0) - float(order.get("fee", 0) or 0)
            result.end_amount += recovered
            result.stranded_asset, result.stranded_amount = "", 0.0
            self.notify.warn(f"unwound to {recovered:.4f} {leg.to_asset}")
        else:
            self.notify.error(f"UNWIND returned no txid for {amount:.8f} {asset}. Check your Kraken account.")
