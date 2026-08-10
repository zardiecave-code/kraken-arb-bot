"""Logging to stdout + rotating-ish file, plus optional Discord alerts."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import requests

# Windows consoles default to cp1252 and mangle anything outside it. The log
# file is always utf-8; make stdout match rather than emit mojibake.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class Notifier:
    def __init__(self, log_file: Path, trades_file: Path, discord_webhook: str = ""):
        self.log_file = log_file
        self.trades_file = trades_file
        self.discord_webhook = discord_webhook
        self._lock = threading.Lock()
        for path in (log_file, trades_file):
            path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} [{level}] {message}"
        with self._lock:
            print(line, flush=True)
            try:
                with self.log_file.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass  # never let logging kill the loop

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)
        self._discord(f":rotating_light: {message}")

    def alert(self, message: str) -> None:
        self._write("ALERT", message)
        self._discord(message)

    def cycle_record(self, record: dict) -> None:
        """Append a machine-readable record of every attempted cycle."""
        record = {"ts": time.time(), **record}
        with self._lock:
            try:
                with self.trades_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, default=str) + "\n")
            except OSError:
                pass

    def _discord(self, message: str) -> None:
        if not self.discord_webhook:
            return
        try:
            requests.post(self.discord_webhook, json={"content": message[:1900]}, timeout=10)
        except requests.RequestException:
            pass
