"""Pair metadata and triangle discovery.

Kraken names the same market three ways: the REST key (``XXBTZUSD``), the
altname (``XBTUSD``) and the websocket name (``BTC/USD``). Orders go out with
the altname, the book feed speaks wsname, and the graph is keyed on the clean
asset codes from the wsname. Keeping those straight is most of this module.
"""

from __future__ import annotations

from dataclasses import dataclass


# `wsname` from AssetPairs is the *v1* websocket name. The v2 feed renamed a
# couple of assets and rejects the old codes outright ("Currency pair not
# supported XBT/USD"), which silently drops the most liquid triangles on the
# exchange. Translate the components before anything downstream sees them.
_WS_V2_ASSETS = {"XBT": "BTC", "XDG": "DOGE"}


def ws_v2_name(wsname: str) -> str:
    base, _, quote = wsname.partition("/")
    return f"{_WS_V2_ASSETS.get(base, base)}/{_WS_V2_ASSETS.get(quote, quote)}"


@dataclass(frozen=True)
class Pair:
    altname: str  # what AddOrder wants
    wsname: str  # what the websocket book feed wants
    base: str  # clean asset code, e.g. "BTC"
    quote: str  # clean asset code, e.g. "USD"
    lot_decimals: int  # volume precision
    pair_decimals: int  # price precision
    ordermin: float  # minimum order volume, in base
    costmin: float  # minimum order cost, in quote
    taker_fee: float  # fraction, e.g. 0.0026


@dataclass(frozen=True)
class Leg:
    """One conversion step: spend `from_asset`, receive `to_asset`."""

    pair: Pair
    side: str  # "buy" (quote -> base) or "sell" (base -> quote)
    from_asset: str
    to_asset: str


@dataclass(frozen=True)
class Cycle:
    """A closed loop back to the starting asset, e.g. USD -> BTC -> ETH -> USD."""

    legs: tuple[Leg, ...]

    @property
    def start_asset(self) -> str:
        return self.legs[0].from_asset

    @property
    def path(self) -> str:
        return " -> ".join([self.legs[0].from_asset] + [leg.to_asset for leg in self.legs])

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(leg.pair.wsname for leg in self.legs)


def _fee_from_schedule(schedule: list, fallback: float) -> float:
    """AssetPairs returns [[volume, pct], ...] ascending by volume tier."""
    if not schedule:
        return fallback
    try:
        return float(schedule[0][1]) / 100.0
    except (IndexError, TypeError, ValueError):
        return fallback


def load_pairs(client, min_volume: float, max_pairs: int, notify) -> dict[str, Pair]:
    """Fetch tradable pairs, keep the liquid online ones, key by wsname."""
    raw = client.asset_pairs()
    tickers = client.ticker()

    candidates: list[tuple[float, Pair]] = []
    for key, info in raw.items():
        if info.get("status") != "online":
            continue
        raw_wsname = info.get("wsname")
        if not raw_wsname or "/" not in raw_wsname:
            continue  # dark-pool and index pairs have no websocket name
        wsname = ws_v2_name(raw_wsname)
        base, quote = wsname.split("/", 1)

        tick = tickers.get(key) or {}
        try:
            # p[1] = 24h volume-weighted avg price, v[1] = 24h base volume.
            quote_volume = float(tick["p"][1]) * float(tick["v"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            quote_volume = 0.0
        if quote_volume < min_volume:
            continue

        pair = Pair(
            altname=info.get("altname") or key,
            wsname=wsname,
            base=base,
            quote=quote,
            lot_decimals=int(info.get("lot_decimals", 8)),
            pair_decimals=int(info.get("pair_decimals", 5)),
            ordermin=float(info.get("ordermin", 0) or 0),
            costmin=float(info.get("costmin", 0) or 0),
            taker_fee=_fee_from_schedule(info.get("fees") or [], 0.0026),
        )
        candidates.append((quote_volume, pair))

    candidates.sort(key=lambda item: item[0], reverse=True)
    kept = {pair.wsname: pair for _, pair in candidates[:max_pairs]}
    notify.info(f"universe: {len(raw)} pairs from Kraken -> {len(candidates)} liquid -> {len(kept)} tracked")
    return kept


def build_cycles(pairs: dict[str, Pair], start_assets: list[str]) -> list[Cycle]:
    """Enumerate every 3-leg loop that starts and ends in a start asset.

    Both directions of a triangle are distinct opportunities (only one of the
    two can be profitable at a time), so both are emitted.
    """
    # asset -> [(neighbour_asset, leg)]
    graph: dict[str, list[tuple[str, Leg]]] = {}
    for pair in pairs.values():
        buy = Leg(pair=pair, side="buy", from_asset=pair.quote, to_asset=pair.base)
        sell = Leg(pair=pair, side="sell", from_asset=pair.base, to_asset=pair.quote)
        graph.setdefault(pair.quote, []).append((pair.base, buy))
        graph.setdefault(pair.base, []).append((pair.quote, sell))

    cycles: list[Cycle] = []
    seen: set[tuple[tuple[str, str], ...]] = set()

    for start in start_assets:
        for mid, leg1 in graph.get(start, []):
            for end, leg2 in graph.get(mid, []):
                if end == start or end == mid:
                    continue
                for final, leg3 in graph.get(end, []):
                    if final != start:
                        continue
                    if leg3.pair.wsname in (leg1.pair.wsname, leg2.pair.wsname):
                        continue  # same market twice is not a triangle
                    signature = tuple((leg.pair.wsname, leg.side) for leg in (leg1, leg2, leg3))
                    if signature in seen:
                        continue
                    seen.add(signature)
                    cycles.append(Cycle(legs=(leg1, leg2, leg3)))

    return cycles


def required_symbols(cycles: list[Cycle]) -> list[str]:
    symbols: set[str] = set()
    for cycle in cycles:
        symbols.update(cycle.symbols)
    return sorted(symbols)
