"""Local order books fed by the Kraken websocket v2 `book` channel."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field

import websockets

# Kraken rejects oversized subscribe frames; chunk the symbol list.
_SUBSCRIBE_BATCH = 50


@dataclass
class Book:
    symbol: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # descending price
    asks: list[tuple[float, float]] = field(default_factory=list)  # ascending price
    updated_at: float = 0.0

    @property
    def age_ms(self) -> float:
        return (time.time() - self.updated_at) * 1000.0

    def top(self) -> tuple[float, float] | None:
        if not self.bids or not self.asks:
            return None
        return self.bids[0][0], self.asks[0][0]


class BookStore:
    """Thread-safe container. The feed thread writes; the scan loop reads."""

    def __init__(self, depth: int):
        self._depth = depth
        self._books: dict[str, Book] = {}
        self._lock = threading.Lock()

    def snapshot(self, symbol: str) -> Book | None:
        with self._lock:
            book = self._books.get(symbol)
            if book is None:
                return None
            # Copy so the evaluator can never read a half-applied update.
            return Book(symbol=book.symbol, bids=list(book.bids), asks=list(book.asks), updated_at=book.updated_at)

    def ready_count(self) -> int:
        with self._lock:
            return sum(1 for book in self._books.values() if book.bids and book.asks)

    def apply(self, message_type: str, data: dict) -> None:
        symbol = data.get("symbol")
        if not symbol:
            return
        with self._lock:
            book = self._books.get(symbol)
            if book is None or message_type == "snapshot":
                book = Book(symbol=symbol)
                self._books[symbol] = book

            book.bids = self._merge(book.bids, data.get("bids") or [], reverse=True)
            book.asks = self._merge(book.asks, data.get("asks") or [], reverse=False)
            book.updated_at = time.time()

    def _merge(self, current: list[tuple[float, float]], changes: list[dict], reverse: bool) -> list[tuple[float, float]]:
        levels = dict(current)
        for change in changes:
            try:
                price = float(change["price"])
                qty = float(change["qty"])
            except (KeyError, TypeError, ValueError):
                continue
            if qty <= 0:
                levels.pop(price, None)  # qty 0 means the level is gone
            else:
                levels[price] = qty
        ordered = sorted(levels.items(), key=lambda item: item[0], reverse=reverse)
        return ordered[: self._depth]


class BookFeed(threading.Thread):
    """Subscribes to `book` for every symbol and keeps the store current."""

    def __init__(self, url: str, symbols: list[str], store: BookStore, depth: int, notify):
        super().__init__(name="book-feed", daemon=True)
        self.url = url
        self.symbols = symbols
        self.store = store
        self.depth = depth
        self.notify = notify
        self._stop = threading.Event()
        self.connected = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 - the thread must not die silently
            self.notify.error(f"book feed thread stopped: {exc}")

    async def _main(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, max_size=16 * 1024 * 1024
                ) as socket:
                    for start in range(0, len(self.symbols), _SUBSCRIBE_BATCH):
                        batch = self.symbols[start : start + _SUBSCRIBE_BATCH]
                        await socket.send(
                            json.dumps(
                                {
                                    "method": "subscribe",
                                    "params": {"channel": "book", "symbol": batch, "depth": self.depth},
                                }
                            )
                        )
                    self.notify.info(f"book feed connected: {len(self.symbols)} symbols @ depth {self.depth}")
                    self.connected.set()
                    backoff = 1.0
                    async for raw in socket:
                        if self._stop.is_set():
                            break
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        if message.get("channel") != "book":
                            if message.get("error"):
                                self.notify.warn(f"book feed error frame: {message['error']}")
                            continue
                        message_type = message.get("type")
                        if message_type not in ("snapshot", "update"):
                            continue
                        for entry in message.get("data") or []:
                            self.store.apply(message_type, entry)
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.connected.clear()
                if self._stop.is_set():
                    return
                self.notify.warn(f"book feed dropped ({exc}); reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
