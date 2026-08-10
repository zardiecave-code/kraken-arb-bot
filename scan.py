"""Read-only reconnaissance: how much edge is actually there?

Needs no API keys and places nothing. Run this before you even think about
trading — it watches live books for a while and reports the distribution of the
best net edge across every triangle, so you can see for yourself whether the
opportunity clears fees at your size.

    python scan.py            # 60 seconds
    python scan.py 300        # 5 minutes
"""

from __future__ import annotations

import sys
import time
from collections import Counter

from karb import arb, universe
from karb.book import BookFeed, BookStore
from karb.config import Config
from karb.kraken import KrakenClient
from karb.notify import Notifier

HEARTBEAT_SECONDS = 300.0


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    cfg = Config.load()
    notify = Notifier(cfg.log_file, cfg.trades_file, "")

    client = KrakenClient()
    pairs = universe.load_pairs(client, cfg.min_pair_volume, cfg.max_pairs, notify)
    cycles = universe.build_cycles(pairs, cfg.start_assets)
    symbols = universe.required_symbols(cycles)
    notify.info(f"{len(cycles)} triangles across {len(symbols)} symbols; watching for {duration:.0f}s")

    fee = cfg.taker_fee_bps / 10_000.0 if cfg.taker_fee_bps is not None else None
    notify.info(
        # `if fee` would be False for a 0bps override and silently claim the
        # tier-0 schedule was used — exactly backwards for a zero-fee run.
        f"fees: {'%.2fbps/leg (override)' % (fee * 10_000) if fee is not None else 'per-pair tier-0 schedule'} "
        f"| size {cfg.trade_size} {cfg.start_assets[0]}"
    )

    store = BookStore(cfg.book_depth)
    feed = BookFeed(cfg.ws_url, symbols, store, cfg.book_depth, notify)
    feed.start()

    deadline = time.time() + 30.0
    while time.time() < deadline and store.ready_count() < len(symbols) * 0.8:
        time.sleep(0.5)
    notify.info(f"books ready: {store.ready_count()}/{len(symbols)}\n")

    buckets: Counter[str] = Counter()
    winners: Counter[str] = Counter()
    best_ever = None
    samples = 0
    end = time.time() + duration

    started = time.time()
    last_report = started
    window_best = None

    try:
        while time.time() < end:
            results = arb.scan(cycles, store, cfg.trade_size, fee, cfg.max_book_age_ms)
            samples += 1
            if not results:
                buckets["no fillable cycle"] += 1
            else:
                top = results[0]
                if best_ever is None or top.profit_bps > best_ever.profit_bps:
                    best_ever = top
                if window_best is None or top.profit_bps > window_best.profit_bps:
                    window_best = top
                buckets[_bucket(top.profit_bps)] += 1
                for item in results:
                    if item.profit_bps >= cfg.min_profit_bps:
                        winners[item.cycle.path] += 1

            # Long runs are the useful ones, so say something occasionally
            # rather than going quiet for an hour.
            now = time.time()
            if now - last_report >= HEARTBEAT_SECONDS:
                last_report = now
                elapsed, remaining = now - started, max(end - now, 0.0)
                if window_best is not None:
                    notify.info(
                        f"[{elapsed / 60:5.1f}m elapsed, {remaining / 60:4.1f}m left] "
                        f"{samples} scans | best this window {window_best.profit_bps:+.2f}bps "
                        f"({window_best.cycle.path}) | cleared {cfg.min_profit_bps:.0f}bps: "
                        f"{sum(winners.values())} times"
                    )
                else:
                    notify.info(f"[{elapsed / 60:5.1f}m elapsed] {samples} scans | no fillable cycle this window")
                window_best = None

            time.sleep(cfg.scan_interval_seconds)
    except KeyboardInterrupt:
        print("\ninterrupted — reporting on what was collected so far")
    finally:
        feed.stop()

    print("\n" + "=" * 68)
    print(f"{samples} scans over {duration:.0f}s — distribution of the BEST cycle per scan")
    print("=" * 68)
    for label, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
        share = 100.0 * count / max(samples, 1)
        print(f"  {label:>22} : {count:6d}  ({share:5.1f}%)  {'#' * int(share / 2)}")

    if best_ever is not None:
        print(f"\nbest single observation: {best_ever.profit_bps:+.2f}bps on {best_ever.cycle.path}")
        for fill in best_ever.fills:
            print(
                f"    {fill.leg.side:>4} {fill.leg.pair.wsname:<14} "
                f"{fill.amount_in:>14.6f} {fill.leg.from_asset:<6} -> "
                f"{fill.amount_out:>14.6f} {fill.leg.to_asset:<6} @ {fill.worst_price:g}"
            )

    if winners:
        print(f"\ncycles that cleared {cfg.min_profit_bps:.1f}bps at least once:")
        for path, count in winners.most_common(15):
            print(f"  {count:6d} scans  {path}")
    else:
        print(f"\nNothing cleared {cfg.min_profit_bps:.1f}bps net of fees during this window.")
        print("That is the normal and expected result. See the README's reality-check section.")

    return 0


def _bucket(bps: float) -> str:
    if bps < -100:
        return "worse than -100bps"
    if bps < -50:
        return "-100 to -50bps"
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
    return "better than +20bps"


if __name__ == "__main__":
    raise SystemExit(main())
