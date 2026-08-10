"""Configuration, loaded from .env next to the project root."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _root()


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    return float(raw) if raw else default


def _opt_float(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    return float(raw) if raw else None


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else default


def _csv(name: str, default: list[str]) -> list[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return list(default)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    # --- safety gates ---
    trade_enabled: bool = False
    live: bool = False
    confirm_live_trading: str = ""

    # --- universe ---
    start_assets: list[str] = field(default_factory=lambda: ["USD"])
    max_pairs: int = 180
    min_pair_volume: float = 250_000.0

    # --- detection ---
    min_profit_bps: float = 8.0
    max_book_age_ms: int = 1500
    scan_interval_seconds: float = 0.5
    ws_url: str = "wss://ws.kraken.com/v2"
    book_depth: int = 10

    # --- sizing ---
    trade_size: float = 50.0
    max_trade_size: float = 200.0
    taker_fee_bps: float | None = None

    # --- risk ---
    max_cycles_per_hour: int = 12
    max_daily_loss: float = 25.0
    cooldown_after_failure_seconds: float = 60.0
    slippage_bps: float = 5.0
    unwind_on_failure: bool = True
    userref: int = 880001

    # --- credentials ---
    api_key: str = ""
    api_secret: str = ""

    # --- alerting ---
    discord_webhook: str = ""

    # --- paths ---
    state_file: Path = ROOT / "state.json"
    log_file: Path = ROOT / "logs" / "bot.log"
    trades_file: Path = ROOT / "logs" / "cycles.ndjson"
    stop_file: Path = ROOT / "STOP"

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        load_dotenv(env_file or (ROOT / ".env"), override=False)
        cfg = cls(
            trade_enabled=_bool("TRADE_ENABLED", False),
            live=_bool("LIVE", False),
            confirm_live_trading=(os.getenv("CONFIRM_LIVE_TRADING") or "").strip(),
            start_assets=_csv("START_ASSETS", ["USD"]),
            max_pairs=_int("MAX_PAIRS", 180),
            min_pair_volume=_float("MIN_PAIR_VOLUME", 250_000.0),
            min_profit_bps=_float("MIN_PROFIT_BPS", 8.0),
            max_book_age_ms=_int("MAX_BOOK_AGE_MS", 1500),
            scan_interval_seconds=_float("SCAN_INTERVAL_SECONDS", 0.5),
            ws_url=(os.getenv("WS_URL") or "wss://ws.kraken.com/v2").strip(),
            book_depth=_int("BOOK_DEPTH", 10),
            trade_size=_float("TRADE_SIZE", 50.0),
            max_trade_size=_float("MAX_TRADE_SIZE", 200.0),
            taker_fee_bps=_opt_float("TAKER_FEE_BPS"),
            max_cycles_per_hour=_int("MAX_CYCLES_PER_HOUR", 12),
            max_daily_loss=_float("MAX_DAILY_LOSS", 25.0),
            cooldown_after_failure_seconds=_float("COOLDOWN_AFTER_FAILURE_SECONDS", 60.0),
            slippage_bps=_float("SLIPPAGE_BPS", 5.0),
            unwind_on_failure=_bool("UNWIND_ON_FAILURE", True),
            userref=_int("USERREF", 880001),
            api_key=(os.getenv("KRAKEN_API_KEY") or "").strip(),
            api_secret=(os.getenv("KRAKEN_API_SECRET") or "").strip(),
            discord_webhook=(os.getenv("DISCORD_WEBHOOK") or "").strip(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.scan_interval_seconds < 0.1:
            raise ConfigError("SCAN_INTERVAL_SECONDS below 0.1 just burns CPU; the book feed is push-based.")
        if self.book_depth < 1 or self.book_depth > 1000:
            raise ConfigError("BOOK_DEPTH must be between 1 and 1000.")
        if not self.start_assets:
            raise ConfigError("START_ASSETS cannot be empty.")
        if self.trade_size <= 0:
            raise ConfigError("TRADE_SIZE must be > 0.")
        if self.trade_size > self.max_trade_size:
            raise ConfigError("TRADE_SIZE exceeds MAX_TRADE_SIZE.")
        if self.min_profit_bps <= 0:
            raise ConfigError("MIN_PROFIT_BPS must be > 0 — a zero threshold trades every rounding error.")

        if self.trade_enabled and not (self.api_key and self.api_secret):
            raise ConfigError("TRADE_ENABLED=true requires KRAKEN_API_KEY and KRAKEN_API_SECRET.")
        if self.live:
            if not self.trade_enabled:
                raise ConfigError("LIVE=true is meaningless without TRADE_ENABLED=true.")
            # Deliberate friction: flipping one boolean should not be enough to
            # start spending real money.
            if self.confirm_live_trading != "I UNDERSTAND THE RISK":
                raise ConfigError(
                    "LIVE=true also requires CONFIRM_LIVE_TRADING='I UNDERSTAND THE RISK' in .env. "
                    "Read the risk section of the README before you set it."
                )

    @property
    def mode(self) -> str:
        if not self.trade_enabled:
            return "SCAN-ONLY (no orders, no keys needed)"
        return "LIVE-TRADING" if self.live else "PAPER (simulated fills + Kraken validate)"
