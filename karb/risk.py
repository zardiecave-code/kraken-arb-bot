"""Hard limits that sit between a detected edge and an actual order.

Every check here can only ever say "no". The bot is allowed to miss
opportunities; it is not allowed to run away.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path


class RiskManager:
    def __init__(self, config, notify):
        self.cfg = config
        self.notify = notify
        self.state_file: Path = config.state_file
        self._state = self._load()

    # ------------------------------------------------------------------ state

    def _load(self) -> dict:
        default = {"day": date.today().isoformat(), "realised_pnl": 0.0, "cycle_times": [], "cooldown_until": 0.0}
        try:
            loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if loaded.get("day") != date.today().isoformat():
            # New day: the loss budget resets, the cooldown does not.
            return {**default, "cooldown_until": loaded.get("cooldown_until", 0.0)}
        return {**default, **loaded}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state_file.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        except OSError as exc:
            self.notify.warn(f"could not persist risk state: {exc}")

    def _roll_day(self) -> None:
        today = date.today().isoformat()
        if self._state.get("day") != today:
            self._state["day"] = today
            self._state["realised_pnl"] = 0.0
            self._state["cycle_times"] = []
            self._save()

    # ----------------------------------------------------------------- checks

    def blocked_reason(self) -> str | None:
        """Why trading is not allowed right now, or None if it is."""
        self._roll_day()

        if self.cfg.stop_file.exists():
            return f"kill switch present ({self.cfg.stop_file.name})"

        now = time.time()
        if now < self._state.get("cooldown_until", 0.0):
            return f"cooling down for {self._state['cooldown_until'] - now:.0f}s after a failed cycle"

        cutoff = now - 3600.0
        recent = [t for t in self._state.get("cycle_times", []) if t > cutoff]
        if len(recent) != len(self._state.get("cycle_times", [])):
            self._state["cycle_times"] = recent
        if len(recent) >= self.cfg.max_cycles_per_hour:
            return f"hourly cycle cap reached ({len(recent)}/{self.cfg.max_cycles_per_hour})"

        if -self._state.get("realised_pnl", 0.0) >= self.cfg.max_daily_loss:
            return f"daily loss limit hit ({self._state['realised_pnl']:.2f})"

        return None

    # --------------------------------------------------------------- recording

    def record_cycle(self, pnl: float) -> None:
        self._roll_day()
        self._state.setdefault("cycle_times", []).append(time.time())
        self._state["realised_pnl"] = self._state.get("realised_pnl", 0.0) + pnl
        self._save()

    def start_cooldown(self, reason: str) -> None:
        self._state["cooldown_until"] = time.time() + self.cfg.cooldown_after_failure_seconds
        self._save()
        self.notify.warn(f"cooldown {self.cfg.cooldown_after_failure_seconds:.0f}s: {reason}")

    @property
    def realised_pnl(self) -> float:
        return self._state.get("realised_pnl", 0.0)

    @property
    def cycles_this_hour(self) -> int:
        cutoff = time.time() - 3600.0
        return len([t for t in self._state.get("cycle_times", []) if t > cutoff])
