"""Coinbase Advanced Trade public market data: products + level2 book feed.

Market data needs no API key. The level2 channel is not auth-gated — the reason
a naive client fails on it is the opening snapshot, which for BTC-USD carries
~46,000 price levels in a single frame and blows past the default 1MB websocket
frame limit. Hence `max_size` below.

Books land in the same `BookStore` the Kraken feed writes to, keyed by Coinbase
product id, so the arbitrage math does not care which venue a book came from.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass

import requests
import websockets

REST_PRODUCTS = "https://api.coinbase.com/api/v3/brokerage/market/products"
WS_URL = "wss://advanced-trade-ws.coinbase.com"

# Coinbase caps level2 subscriptions per connection — measured empirically at
# roughly 20, past which it replies "too many L2 streams requested in a single
# session" and silently tracks a subset. Shard across connections, well under.
_MAX_STREAMS_PER_CONNECTION = 15
_MAX_FRAME = 64 * 1024 * 1024


@dataclass(frozen=True)
class Product:
    product_id: str  # "BTC-USD"
    base: str  # "BTC"
    quote: str  # "USD"
    volume_24h: float  # in quote currency
    base_min_size: float
    price: float  # last trade, for cross-venue sanity checks


def load_products(quote: str, min_volume: float, notify) -> dict[str, Product]:
    """Online products quoted in `quote`, keyed by base asset."""
    response = requests.get(REST_PRODUCTS, params={"limit": 500}, timeout=20)
    response.raise_for_status()
    products: dict[str, Product] = {}

    for entry in response.json().get("products", []):
        if entry.get("quote_currency_id") != quote:
            continue
        if entry.get("status") != "online":
            continue
        if entry.get("trading_disabled") or entry.get("is_disabled") or entry.get("view_only"):
            continue
        try:
            volume = float(entry.get("approximate_quote_24h_volume") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume < min_volume:
            continue
        try:
            min_size = float(entry.get("base_min_size") or 0)
        except (TypeError, ValueError):
            min_size = 0.0
        try:
            price = float(entry.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0

        products[entry["base_currency_id"]] = Product(
            product_id=entry["product_id"],
            base=entry["base_currency_id"],
            quote=quote,
            volume_24h=volume,
            base_min_size=min_size,
            price=price,
        )

    notify.info(f"coinbase: {len(products)} online {quote} products above ${min_volume:,.0f}")
    return products


REST_CURRENCIES = "https://api.exchange.coinbase.com/currencies"


def load_currency_status() -> dict[str, tuple[bool, str]]:
    """Which assets can actually move in and out of Coinbase.

    A cross-venue spread is only capturable if the asset can travel between the
    venues. Coinbase exposes this publicly; Kraken exposes its half via Assets.
    """
    response = requests.get(REST_CURRENCIES, timeout=20)
    response.raise_for_status()
    status: dict[str, tuple[bool, str]] = {}
    for entry in response.json():
        details = entry.get("details") or {}
        if entry.get("status") != "online":
            status[entry["id"]] = (False, f"coinbase status {entry.get('status')}")
        elif details.get("deposit_disabled"):
            status[entry["id"]] = (False, "coinbase deposits disabled")
        elif details.get("withdrawal_disabled"):
            status[entry["id"]] = (False, "coinbase withdrawals disabled")
        else:
            status[entry["id"]] = (True, "")
    return status


class CoinbaseFeed(threading.Thread):
    """level2 for every product, normalised into the shared BookStore."""

    def __init__(self, product_ids: list[str], store, notify, url: str = WS_URL):
        super().__init__(name="coinbase-feed", daemon=True)
        self.product_ids = product_ids
        self.store = store
        self.notify = notify
        self.url = url
        self._stop = threading.Event()
        self.connected = threading.Event()
        # A rejected subscription means an asset is missing from every scan that
        # follows. Count them so a degraded run is never mistaken for a clean one.
        self.rejections = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 - the thread must not die silently
            self.notify.error(f"coinbase feed thread stopped: {exc}")

    async def _main(self) -> None:
        shards = [
            self.product_ids[i : i + _MAX_STREAMS_PER_CONNECTION]
            for i in range(0, len(self.product_ids), _MAX_STREAMS_PER_CONNECTION)
        ]
        self.notify.info(
            f"coinbase: {len(self.product_ids)} products across {len(shards)} connections "
            f"(cap {_MAX_STREAMS_PER_CONNECTION}/connection)"
        )
        await asyncio.gather(*(self._run_shard(i, shard) for i, shard in enumerate(shards)))

    async def _run_shard(self, index: int, products: list[str]) -> None:
        backoff = 1.0
        # Stagger connections; four shards subscribing at once trips Coinbase's
        # rate limiter and the refused products go missing for the whole run.
        await asyncio.sleep(index * 2.0)
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, ping_timeout=20, max_size=_MAX_FRAME
                ) as socket:
                    # One product per frame, paced — the opening snapshots are
                    # large and arriving all at once invites a drop.
                    for product_id in products:
                        await socket.send(
                            json.dumps({"type": "subscribe", "product_ids": [product_id], "channel": "level2"})
                        )
                        await asyncio.sleep(0.35)
                    self.notify.info(f"coinbase shard {index}: {len(products)} products subscribed")
                    self.connected.set()
                    backoff = 1.0

                    async for raw in socket:
                        if self._stop.is_set():
                            break
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        channel = message.get("channel")
                        if channel == "error" or message.get("type") == "error":
                            self.rejections += 1
                            self.notify.warn(f"coinbase shard {index} error: {str(message)[:180]}")
                            continue
                        if channel != "l2_data":
                            continue
                        for event in message.get("events") or []:
                            self._apply(event)
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                if self._stop.is_set():
                    return
                self.notify.warn(f"coinbase shard {index} dropped ({exc}); reconnecting in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _apply(self, event: dict) -> None:
        product_id = event.get("product_id")
        if not product_id:
            return
        event_type = "snapshot" if event.get("type") == "snapshot" else "update"

        bids: list[dict] = []
        asks: list[dict] = []
        for change in event.get("updates") or []:
            level = {"price": change.get("price_level"), "qty": change.get("new_quantity")}
            # Coinbase says "offer" where Kraken says "ask".
            (bids if change.get("side") == "bid" else asks).append(level)

        self.store.apply(event_type, {"symbol": product_id, "bids": bids, "asks": asks})
