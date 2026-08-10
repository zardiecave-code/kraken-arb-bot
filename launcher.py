"""One-click entry point: a menu over the scanners and the bot.

Packaged by build-exe.cmd into a single self-contained .exe. It reads .env and
writes logs\\ NEXT TO ITSELF, so keep the exe in its own folder.

Deliberately a menu rather than a single action. Two of the three entry points
are read-only reconnaissance; the third can place real orders. A one-click
binary that silently started the trading bot would be the wrong thing to build.
"""

from __future__ import annotations

import sys
import traceback

BANNER = r"""
 ============================================================
   kraken-arb-bot
   triangular + cross-exchange arbitrage scanner
 ============================================================
"""

MENU = """
  1)  Scan Kraken triangles          read-only, no API keys needed
  2)  Scan Kraken vs Coinbase        read-only, no API keys needed
  3)  Run the bot                    mode comes from .env
  4)  Show current configuration

  Q)  Quit
"""


def _read(prompt: str) -> str:
    """input(), tolerant of the BOM some shells prepend when piping stdin."""
    return input(prompt).lstrip("﻿").strip()


def _ask_seconds(prompt: str, default: int) -> int:
    raw = _read(f"{prompt} [{default}]: ")
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        print(f"  not a number; using {default}")
        return default
    if value < 10:
        print("  minimum is 10 seconds; using 10")
        return 10
    return value


def _pause() -> None:
    input("\n  press Enter to return to the menu... ")


def run_triangular() -> None:
    import scan

    seconds = _ask_seconds("  How many seconds to watch?", 300)
    print()
    sys.argv = ["scan.py", str(seconds)]
    scan.main()


def run_cross() -> None:
    import scan_cross

    seconds = _ask_seconds("  How many seconds to watch?", 900)
    print()
    sys.argv = ["scan_cross.py", str(seconds)]
    scan_cross.main()


def show_config() -> None:
    from karb.config import Config, ConfigError

    try:
        cfg = Config.load()
    except ConfigError as exc:
        print(f"\n  .env problem: {exc}")
        return

    print(f"""
  mode                {cfg.mode}
  start assets        {', '.join(cfg.start_assets)}
  min profit          {cfg.min_profit_bps:.1f} bps
  trade size          {cfg.trade_size:g}  (max {cfg.max_trade_size:g})
  max cycles / hour   {cfg.max_cycles_per_hour}
  daily loss cap      {cfg.max_daily_loss:g}
  API key present     {'yes' if cfg.api_key else 'no'}
  files next to       {cfg.state_file.parent}
""")


def run_bot() -> None:
    from karb.bot import run
    from karb.config import Config, ConfigError

    try:
        cfg = Config.load()
    except ConfigError as exc:
        print(f"\n  Cannot start: {exc}")
        return

    print(f"\n  Mode: {cfg.mode}")

    if cfg.live:
        # Last gate before real money. The .env flags are already three deep;
        # this one exists because a double-clicked exe is easy to launch by
        # accident and hard to un-launch.
        print(f"""
  ----------------------------------------------------------
   LIVE TRADING. This will place real orders with real money.
   Size {cfg.trade_size:g} per cycle, daily loss cap {cfg.max_daily_loss:g}.
   Create a file named STOP next to this exe to halt it.
  ----------------------------------------------------------""")
        if _read("  Type LIVE to continue, anything else to cancel: ") != "LIVE":
            print("  cancelled.")
            return

    run(cfg)


ACTIONS = {
    "1": run_triangular,
    "2": run_cross,
    "3": run_bot,
    "4": show_config,
}


def main() -> int:
    print(BANNER)
    while True:
        print(MENU)
        try:
            choice = _read("  choose: ").lower()
        except EOFError:
            return 0

        if choice in ("q", "quit", "exit"):
            return 0

        action = ACTIONS.get(choice)
        if action is None:
            print("  not an option.")
            continue

        try:
            action()
        except KeyboardInterrupt:
            print("\n  stopped.")
        except Exception:  # noqa: BLE001 - a crash must not close the window
            print("\n  something went wrong:\n")
            traceback.print_exc()
        _pause()


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - keep the window open on any failure
        traceback.print_exc()
        input("\npress Enter to close... ")
        code = 1
    raise SystemExit(code)
