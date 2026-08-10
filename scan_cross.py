"""Cross-exchange reconnaissance: Kraken vs Coinbase, same asset, both directions.

Read-only. No API keys on either venue, no orders.

This measures something different from the triangular scans, and the difference
matters. A triangular loop starts and ends in USD on one venue, so a positive
number is a genuine inconsistency. A cross-venue spread is not: to capture it
you must already hold USD on one exchange and the asset on the other, and you
must periodically move funds back to rebalance. That transfer costs a withdrawal
fee, an on-chain fee, and minutes-to-hours of price risk. The spread printed
here is gross of all of it.

So read the output as "how wide is the gap", not "how much is free".

    python scan_cross.py 600
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter, defaultdict

from karb import arb, coinbase, universe
from karb.book import BookFeed, BookStore
from karb.config import Config
from karb.kraken import KrakenClient
from karb.notify import Notifier

HEARTBEAT_SECONDS = 120.0
KRAKEN_DEPTH = 10
# Coinbase streams full-book updates rather than a depth-limited window, so keep
# a deeper local window; truncating too tightly erodes the book over time.
COINBASE_DEPTH = 200


class CrossQuote:
    """One asset's simultaneous view on both venues."""

    def __init__(self, asset, kraken_symbol, coinbase_symbol, kraken_volume, coinbase_volume,
                 transferable=True, block_reason="", suspect_reason=""):
        self.asset = asset
        self.kraken_symbol = kraken_symbol
        self.coinbase_symbol = coinbase_symbol
        self.kraken_volume = kraken_volume
        self.coinbase_volume = coinbase_volume
        # Whether the coin can actually travel between the two venues. A gap on
        # an asset that cannot move is not an opportunity, it is the reason the
        # gap exists.
        self.transferable = transferable
        self.block_reason = block_reason
        # Set when the two venues' prices are too far apart to be the same
        # instrument. Exchanges reuse tickers: Kraken's VELO is Velo Protocol,
        # Coinbase's VELO is Velodrome Finance, and comparing them produces a
        # fictitious 434% "arbitrage". Nothing else in this scanner would catch
        # that, and it presents as the largest opportunity on the board.
        self.suspect_reason = suspect_reason


def cross_edge(size, buy_book, sell_book, fee_buy, fee_sell):
    """Spend `size` USD buying on one venue, sell the lot on the other."""
    bought = arb.consume_asks(buy_book.asks, size, fee_buy)
    if bought is None:
        return None
    base, buy_price = bought
    sold = arb.consume_bids(sell_book.bids, base, fee_sell)
    if sold is None:
        return None
    proceeds, sell_price = sold
    return proceeds, buy_price, sell_price


def evaluate(quote, kraken_store, coinbase_store, size, fee_kr, fee_cb, max_age_ms):
    """Best of the two directions, or None if either book is stale or thin."""
    kb = kraken_store.snapshot(quote.kraken_symbol)
    cb = coinbase_store.snapshot(quote.coinbase_symbol)
    if kb is None or cb is None or not kb.bids or not kb.asks or not cb.bids or not cb.asks:
        return None
    if kb.age_ms > max_age_ms or cb.age_ms > max_age_ms:
        return None

    best = None
    for label, buy_book, sell_book, fee_b, fee_s in (
        ("buy Kraken -> sell Coinbase", kb, cb, fee_kr, fee_cb),
        ("buy Coinbase -> sell Kraken", cb, kb, fee_cb, fee_kr),
    ):
        result = cross_edge(size, buy_book, sell_book, fee_b, fee_s)
        if result is None:
            continue
        proceeds, buy_price, sell_price = result
        bps = (proceeds / size - 1.0) * 10_000.0
        if best is None or bps > best["bps"]:
            best = {
                "asset": quote.asset,
                "direction": label,
                "bps": bps,
                "proceeds": proceeds,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "age_ms": max(kb.age_ms, cb.age_ms),
                "transferable": quote.transferable,
                "block_reason": quote.block_reason,
            }
    return best


def _bucket(bps: float) -> str:
    if bps < -50:
        return "worse than -50bps"
    if bps < -20:
        return "-50 to -20bps"
    if bps < -5:
        return "-20 to -5bps"
    if bps < 0:
        return "-5 to 0bps"
    if bps < 5:
        return "0 to +5bps"
    if bps < 20:
        return "+5 to +20bps"
    if bps < 50:
        return "+20 to +50bps"
    return "better than +50bps"


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
    cfg = Config.load()
    notify = Notifier(cfg.log_file, cfg.trades_file, "")

    fee_kr = float(os.getenv("CROSS_FEE_KRAKEN_BPS") or 0) / 10_000.0
    fee_cb = float(os.getenv("CROSS_FEE_COINBASE_BPS") or 0) / 10_000.0
    max_assets = int(os.getenv("CROSS_MAX_ASSETS") or 40)
    min_volume = float(os.getenv("CROSS_MIN_VOLUME") or 250_000)
    # Fraction by which the two venues' prices may differ before the pair is
    # treated as a ticker collision rather than an opportunity. Genuine
    # cross-venue spreads are basis points; 25% is far outside that.
    max_price_deviation = float(os.getenv("CROSS_MAX_PRICE_DEVIATION") or 0.25)

    client = KrakenClient()
    kraken_pairs = universe.load_pairs(client, min_volume, 2000, notify)
    kraken_usd = {p.base: p for p in kraken_pairs.values() if p.quote == "USD"}
    coinbase_products = coinbase.load_products("USD", min_volume, notify)

    # Funding status on both venues. An asset that cannot be deposited into the
    # venue you would sell on is one whose price gap can never be closed.
    kraken_status = {}
    for code, info in client.assets().items():
        altname = info.get("altname") or code
        altname = {"XBT": "BTC", "XDG": "DOGE"}.get(altname, altname)
        kraken_status[altname] = info.get("status", "unknown")
    coinbase_status = coinbase.load_currency_status()

    # Reference prices from both venues, used below to reject ticker collisions.
    kraken_price: dict[str, float] = {}
    try:
        raw_pairs = client.asset_pairs()
        tickers = client.ticker()
        # Ticker is keyed by Kraken's REST pair key, not the altname, and must
        # be restricted to USD quotes — otherwise USDT/JPY overwrites USDT/USD
        # and a yen price gets compared against Coinbase dollars.
        key_to_base = {}
        for key, info in raw_pairs.items():
            wsname = info.get("wsname") or ""
            if "/" not in wsname:
                continue
            base, quote = universe.ws_v2_name(wsname).split("/", 1)
            if quote == "USD":
                key_to_base[key] = base
        for key, row in tickers.items():
            base = key_to_base.get(key)
            if base is None:
                continue
            try:
                kraken_price[base] = float(row["c"][0])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
    except Exception as exc:  # noqa: BLE001 - fall back to no price check
        notify.warn(f"could not load Kraken reference prices ({exc}); collision check disabled")

    common = sorted(set(kraken_usd) & set(coinbase_products))
    quotes = []
    for a in common:
        k_state = kraken_status.get(a, "unknown")
        cb_ok, cb_why = coinbase_status.get(a, (True, ""))
        if k_state != "enabled":
            transferable, reason = False, f"kraken {k_state}"
        elif not cb_ok:
            transferable, reason = False, cb_why
        else:
            transferable, reason = True, ""

        # Same ticker, wildly different price = not the same instrument. Real
        # cross-venue spreads on one asset live in basis points; anything past
        # a few percent means the two symbols name different tokens.
        suspect = ""
        kp, cp = kraken_price.get(a, 0.0), coinbase_products[a].price
        if kp > 0 and cp > 0:
            ratio = max(kp, cp) / min(kp, cp)
            if ratio > 1 + max_price_deviation:
                suspect = (
                    f"prices differ {((ratio - 1) * 100):.0f}% "
                    f"(kraken {kp:.8g} vs coinbase {cp:.8g}) — likely different assets"
                )
        quotes.append(
            CrossQuote(
                asset=a,
                kraken_symbol=kraken_usd[a].wsname,
                coinbase_symbol=coinbase_products[a].product_id,
                kraken_volume=0.0,
                coinbase_volume=coinbase_products[a].volume_24h,
                transferable=transferable,
                block_reason=reason,
                suspect_reason=suspect,
            )
        )
    quotes.sort(key=lambda q: -q.coinbase_volume)
    quotes = quotes[:max_assets]

    # Drop collisions outright — a fictitious 400% edge would otherwise top
    # every scan and drown the real signal.
    suspects = [q for q in quotes if q.suspect_reason]
    quotes = [q for q in quotes if not q.suspect_reason]

    blocked = [q for q in quotes if not q.transferable]
    notify.info(f"{len(common)} assets on both venues; tracking the {len(quotes) + len(suspects)} most liquid")
    if suspects:
        notify.warn(f"DROPPED {len(suspects)} as same-ticker-different-asset:")
        for q in suspects:
            notify.warn(f"    suspect  {q.asset:<8} {q.suspect_reason}")
    notify.info(f"transferable on both venues: {len(quotes) - len(blocked)} | blocked: {len(blocked)}")
    for q in blocked:
        notify.info(f"    blocked  {q.asset:<8} {q.block_reason}")
    notify.info(
        f"fees: kraken {fee_kr * 10_000:.2f}bps/leg, coinbase {fee_cb * 10_000:.2f}bps/leg "
        f"| size {cfg.trade_size} USD | watching {duration:.0f}s"
    )

    kraken_store = BookStore(KRAKEN_DEPTH)
    coinbase_store = BookStore(COINBASE_DEPTH)
    kraken_feed = BookFeed(cfg.ws_url, [q.kraken_symbol for q in quotes], kraken_store, KRAKEN_DEPTH, notify)
    coinbase_feed = CoinbaseFeedWrapper([q.coinbase_symbol for q in quotes], coinbase_store, notify)
    kraken_feed.start()
    coinbase_feed.start()

    requested = len(quotes)
    # Staggered, paced subscriptions take a while to all land.
    deadline = time.time() + 120.0
    while time.time() < deadline:
        if kraken_store.ready_count() >= requested and coinbase_store.ready_count() >= requested:
            break
        time.sleep(0.5)

    # Only measure assets whose books actually arrived on both venues. A missing
    # subscription would otherwise just remove an asset from every scan without
    # ever showing up in the results.
    live, dropped = [], []
    for quote in quotes:
        kb = kraken_store.snapshot(quote.kraken_symbol)
        cb = coinbase_store.snapshot(quote.coinbase_symbol)
        if kb and kb.bids and kb.asks and cb and cb.bids and cb.asks:
            live.append(quote)
        else:
            dropped.append(quote.asset)
    quotes = live

    notify.info(f"books ready — measuring {len(quotes)}/{requested} assets on both venues")
    if dropped:
        notify.warn(f"dropped {len(dropped)} without books on both venues: {', '.join(dropped[:20])}")
    if coinbase_feed.rejections:
        notify.warn(f"coinbase rejected {coinbase_feed.rejections} subscription(s) — results cover fewer assets")
    if not quotes:
        notify.error("no asset has a live book on both venues; aborting")
        kraken_feed.stop()
        coinbase_feed.stop()
        return 1
    print()

    buckets: Counter[str] = Counter()
    buckets_movable: Counter[str] = Counter()
    per_asset: dict[str, float] = defaultdict(lambda: float("-inf"))
    directions: Counter[str] = Counter()
    best_ever = None
    best_movable = [None]  # boxed so the scan loop can rebind it
    samples = 0
    started = time.time()
    last_report = started
    end = started + duration

    try:
        while time.time() < end:
            results = []
            for quote in quotes:
                found = evaluate(
                    quote, kraken_store, coinbase_store, cfg.trade_size, fee_kr, fee_cb, cfg.max_book_age_ms
                )
                if found is not None:
                    results.append(found)

            samples += 1
            if not results:
                buckets["no comparable book"] += 1
            else:
                top = max(results, key=lambda r: r["bps"])
                buckets[_bucket(top["bps"])] += 1
                directions[top["direction"]] += 1
                if best_ever is None or top["bps"] > best_ever["bps"]:
                    best_ever = top
                for item in results:
                    per_asset[item["asset"]] = max(per_asset[item["asset"]], item["bps"])

                # The same scan, restricted to assets that can actually move
                # between the venues. This is the number that means anything.
                movable = [r for r in results if r["transferable"]]
                if movable:
                    top_movable = max(movable, key=lambda r: r["bps"])
                    buckets_movable[_bucket(top_movable["bps"])] += 1
                    if best_movable[0] is None or top_movable["bps"] > best_movable[0]["bps"]:
                        best_movable[0] = top_movable
                else:
                    buckets_movable["no transferable book"] += 1

            now = time.time()
            if now - last_report >= HEARTBEAT_SECONDS:
                last_report = now
                top_txt = f"{best_ever['bps']:+.2f}bps {best_ever['asset']}" if best_ever else "nothing"
                notify.info(
                    f"[{(now - started) / 60:5.1f}m elapsed, {(end - now) / 60:4.1f}m left] "
                    f"{samples} scans | best so far {top_txt}"
                )

            time.sleep(cfg.scan_interval_seconds)
    except KeyboardInterrupt:
        print("\ninterrupted — reporting on what was collected")
    finally:
        kraken_feed.stop()
        coinbase_feed.stop()

    print("\n" + "=" * 72)
    print(f"{samples} scans over {duration:.0f}s — best cross-venue edge per scan (GROSS)")
    print(f"{len(quotes)} assets measured on both venues · coinbase rejections: {coinbase_feed.rejections}")
    print("=" * 72)
    for label, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        share = 100.0 * count / max(samples, 1)
        print(f"  {label:>22} : {count:6d}  ({share:5.1f}%)  {'#' * int(share / 2)}")

    if directions:
        print("\ndirection of the winning trade:")
        for label, count in directions.most_common():
            print(f"  {count:6d}  {label}")

    if best_ever:
        print(f"\nbest single observation: {best_ever['bps']:+.2f}bps on {best_ever['asset']}")
        print(f"    {best_ever['direction']}")
        print(f"    buy @ {best_ever['buy_price']:g}  sell @ {best_ever['sell_price']:g}")
        print(f"    ${cfg.trade_size:.2f} -> ${best_ever['proceeds']:.4f}  (book age {best_ever['age_ms']:.0f}ms)")

    print("\n" + "=" * 72)
    print("SAME SCANS, restricted to assets that can actually move between venues")
    print("=" * 72)
    for label, count in sorted(buckets_movable.items(), key=lambda kv: -kv[1]):
        share = 100.0 * count / max(samples, 1)
        print(f"  {label:>22} : {count:6d}  ({share:5.1f}%)  {'#' * int(share / 2)}")

    bm = best_movable[0]
    if bm:
        print(f"\nbest transferable observation: {bm['bps']:+.2f}bps on {bm['asset']}")
        print(f"    {bm['direction']}")
        print(f"    buy @ {bm['buy_price']:g}  sell @ {bm['sell_price']:g}")
        print(f"    ${cfg.trade_size:.2f} -> ${bm['proceeds']:.4f}  (book age {bm['age_ms']:.0f}ms)")

    transferable_assets = {q.asset for q in quotes if q.transferable}
    ranked = sorted(per_asset.items(), key=lambda kv: -kv[1])[:20]
    if ranked:
        print(f"\n{'asset':<8} {'best gross edge':>16}   transferable?")
        print("-" * 46)
        for asset, bps in ranked:
            ok = "yes" if asset in transferable_assets else "NO - gap cannot close"
            print(f"{asset:<8} {bps:>+15.2f}bps   {ok}")

    print("\nGROSS — no exchange fees, no withdrawal fees, no transfer cost, no rebalancing.")
    return 0


class CoinbaseFeedWrapper(coinbase.CoinbaseFeed):
    """Named separately so the thread shows up clearly in tracebacks."""


if __name__ == "__main__":
    raise SystemExit(main())
