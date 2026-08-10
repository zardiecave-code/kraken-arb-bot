"""Main scan/execute loop."""

from __future__ import annotations

import time

from . import arb, universe
from .book import BookFeed, BookStore
from .config import Config
from .execute import Executor
from .kraken import KrakenClient, KrakenError
from .notify import Notifier
from .risk import RiskManager

HEARTBEAT_SECONDS = 30.0

# wsname asset codes do not always match Kraken's internal balance keys.
_BALANCE_ALIASES = {"BTC": "XXBT", "DOGE": "XXDG", "USD": "ZUSD", "EUR": "ZEUR", "GBP": "ZGBP"}


def resolve_balance(balances: dict, asset: str) -> float:
    for candidate in (_BALANCE_ALIASES.get(asset), asset, f"Z{asset}", f"X{asset}"):
        if candidate and candidate in balances:
            return float(balances[candidate])
    return 0.0


def resolve_fee(client, config, pairs, notify) -> float | None:
    """None means 'use each pair's own tier-0 fee from AssetPairs'."""
    if config.taker_fee_bps is not None:
        fee = config.taker_fee_bps / 10_000.0
        notify.info(f"taker fee: {config.taker_fee_bps:.2f}bps per leg (from TAKER_FEE_BPS)")
        return fee
    if not (config.api_key and config.api_secret):
        notify.info("taker fee: per-pair tier-0 schedule (no keys, cannot read your volume tier)")
        return None
    try:
        sample = next(iter(pairs.values())).altname
        result = client.trade_volume(sample)
        fees = result.get("fees") or {}
        entry = next(iter(fees.values()), None)
        if entry and "fee" in entry:
            fee = float(entry["fee"]) / 100.0
            notify.info(f"taker fee: {fee * 10_000:.2f}bps per leg (your 30d volume tier)")
            return fee
    except (KrakenError, StopIteration, ValueError) as exc:
        notify.warn(f"could not read fee tier ({exc}); falling back to tier-0 schedule")
    return None


def run(config: Config | None = None) -> int:
    cfg = config or Config.load()
    notify = Notifier(cfg.log_file, cfg.trades_file, cfg.discord_webhook)

    notify.info("=" * 68)
    notify.info(f"kraken-arb-bot starting — mode: {cfg.mode}")
    notify.info(f"start assets: {', '.join(cfg.start_assets)} | min edge: {cfg.min_profit_bps:.1f}bps")
    if cfg.live:
        notify.alert(f"LIVE TRADING ENABLED — size {cfg.trade_size}, daily loss cap {cfg.max_daily_loss}")
    notify.info("=" * 68)

    client = KrakenClient(cfg.api_key, cfg.api_secret)

    try:
        status = client.system_status()
        if status.get("status") != "online":
            notify.error(f"Kraken reports status '{status.get('status')}' — refusing to start")
            return 1
    except KrakenError as exc:
        notify.error(f"cannot reach Kraken: {exc}")
        return 1

    pairs = universe.load_pairs(client, cfg.min_pair_volume, cfg.max_pairs, notify)
    if not pairs:
        notify.error("no pairs survived the liquidity filter — lower MIN_PAIR_VOLUME")
        return 1

    cycles = universe.build_cycles(pairs, cfg.start_assets)
    if not cycles:
        notify.error(
            f"no triangles found from {cfg.start_assets}. Raise MAX_PAIRS, lower MIN_PAIR_VOLUME, "
            "or check the asset code (Kraken's websocket names use BTC, not XBT)."
        )
        return 1
    symbols = universe.required_symbols(cycles)
    notify.info(f"{len(cycles)} triangles across {len(symbols)} symbols")

    fee_override = resolve_fee(client, cfg, pairs, notify)

    risk = RiskManager(cfg, notify)
    executor = Executor(client, cfg, notify)

    available = cfg.trade_size
    if cfg.trade_enabled:
        try:
            balances = client.balance()
            available = resolve_balance(balances, cfg.start_assets[0])
            notify.info(f"balance: {available:.2f} {cfg.start_assets[0]}")
            if cfg.live and available < cfg.trade_size:
                notify.error(f"balance {available:.2f} is below TRADE_SIZE {cfg.trade_size} — refusing to start")
                return 1
        except KrakenError as exc:
            notify.error(f"cannot read balance: {exc}")
            return 1

    store = BookStore(cfg.book_depth)
    feed = BookFeed(cfg.ws_url, symbols, store, cfg.book_depth, notify)
    feed.start()

    notify.info("warming up books…")
    warm_deadline = time.time() + 30.0
    while time.time() < warm_deadline and store.ready_count() < len(symbols) * 0.8:
        time.sleep(0.5)
    notify.info(f"books ready: {store.ready_count()}/{len(symbols)}")

    last_heartbeat = 0.0
    best_seen = float("-inf")

    try:
        while True:
            if cfg.stop_file.exists():
                notify.warn(f"kill switch {cfg.stop_file.name} present — stopping")
                break

            results = arb.scan(cycles, store, cfg.trade_size, fee_override, cfg.max_book_age_ms)
            best = results[0] if results else None
            if best:
                best_seen = max(best_seen, best.profit_bps)

            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = now
                if best:
                    notify.info(
                        f"scanned {len(results)}/{len(cycles)} fillable | best {best.profit_bps:+.2f}bps "
                        f"({best.cycle.path}) | 30s peak {best_seen:+.2f}bps | "
                        f"pnl today {risk.realised_pnl:+.2f} | cycles/h {risk.cycles_this_hour}"
                    )
                else:
                    notify.info(f"no fillable cycles (books ready {store.ready_count()}/{len(symbols)})")
                best_seen = float("-inf")

            if best and best.profit_bps >= cfg.min_profit_bps:
                _handle_opportunity(best, cfg, notify, risk, executor, store, fee_override, available)
                if cfg.trade_enabled:
                    try:
                        available = resolve_balance(client.balance(), cfg.start_assets[0])
                    except KrakenError as exc:
                        notify.warn(f"balance refresh failed: {exc}")

            time.sleep(cfg.scan_interval_seconds)
    except KeyboardInterrupt:
        notify.info("interrupted — shutting down")
    finally:
        feed.stop()

    notify.info(f"stopped. realised pnl today: {risk.realised_pnl:+.2f}")
    return 0


def _handle_opportunity(best, cfg, notify, risk, executor, store, fee_override, available) -> None:
    ok, why = arb.meets_minimums(best)
    if not ok:
        notify.info(f"skipping {best.cycle.path} ({best.profit_bps:+.2f}bps): {why}")
        return

    notify.alert(
        f"EDGE {best.profit_bps:+.2f}bps on {best.cycle.path} "
        f"— {best.size:.2f} -> {best.end_amount:.4f} ({best.profit:+.4f}), book age {best.oldest_book_ms:.0f}ms"
    )

    if not cfg.trade_enabled:
        notify.cycle_record(
            {"event": "detected", "path": best.cycle.path, "bps": best.profit_bps, "size": best.size}
        )
        return

    blocked = risk.blocked_reason()
    if blocked:
        notify.warn(f"not trading: {blocked}")
        return

    size = min(cfg.trade_size, cfg.max_trade_size)
    if cfg.live:
        size = min(size, available)
    if size <= 0:
        notify.warn("not trading: no available balance in the start asset")
        return

    # Re-price against the live book at the size we will actually send. The
    # edge that triggered this was measured milliseconds ago and may be gone.
    confirmed = arb.evaluate(best.cycle, store, size, fee_override, cfg.max_book_age_ms)
    if confirmed is None:
        notify.warn(f"edge evaporated before confirm on {best.cycle.path} (depth or stale book)")
        return
    if confirmed.profit_bps < cfg.min_profit_bps:
        notify.warn(
            f"edge decayed {best.profit_bps:+.2f} -> {confirmed.profit_bps:+.2f}bps before confirm; skipping"
        )
        return

    notify.info(f"executing {confirmed.cycle.path} at size {size:.2f} for {confirmed.profit_bps:+.2f}bps")
    result = executor.run_cycle(confirmed)

    notify.cycle_record(
        {
            "event": "executed" if result.ok else "failed",
            "live": cfg.live,
            "path": result.path,
            "size": result.size,
            "end_amount": result.end_amount,
            "pnl": result.pnl,
            "expected_bps": confirmed.profit_bps,
            "error": result.error,
            "stranded": {"asset": result.stranded_asset, "amount": result.stranded_amount},
            "legs": [vars(leg) for leg in result.legs],
        }
    )

    if result.ok:
        realised_bps = (result.pnl / result.size) * 10_000.0 if result.size else 0.0
        notify.alert(
            f"CYCLE DONE {result.path}: {result.pnl:+.4f} ({realised_bps:+.2f}bps realised "
            f"vs {confirmed.profit_bps:+.2f}bps expected)"
        )
        if cfg.live:
            risk.record_cycle(result.pnl)
    else:
        notify.error(f"CYCLE FAILED {result.path}: {result.error}")
        if cfg.live:
            risk.record_cycle(result.pnl)
        risk.start_cooldown(result.error)


if __name__ == "__main__":
    raise SystemExit(run())
