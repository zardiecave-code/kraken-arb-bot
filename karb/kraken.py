"""Minimal Kraken REST client: public market data + signed private calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
import urllib.parse

import requests


class KrakenError(RuntimeError):
    """Kraken returned an error envelope, or the HTTP call was unusable."""


_RETRYABLE_CODES = (
    "EAPI:Rate limit exceeded",
    "EGeneral:Temporary lockout",
    "EService:Unavailable",
    "EService:Busy",
)


def _is_retryable(errors: list[str]) -> bool:
    return any(e.startswith(_RETRYABLE_CODES) for e in errors)


class KrakenClient:
    BASE = "https://api.kraken.com"

    def __init__(self, key: str = "", secret: str = "", timeout: float = 15.0, max_retries: int = 3):
        self._key = key
        self._secret = secret
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "kraken-arb-bot/1.0"
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    # ------------------------------------------------------------------ auth

    def _nonce(self) -> int:
        # Kraken requires a strictly increasing nonce per key. Millisecond clock
        # plus a monotonic guard so two calls in the same ms still increase.
        with self._nonce_lock:
            nonce = max(int(time.time() * 1000), self._last_nonce + 1)
            self._last_nonce = nonce
            return nonce

    def _signature(self, urlpath: str, data: dict) -> str:
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self._secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    # --------------------------------------------------------------- plumbing

    def _envelope(self, response: requests.Response) -> tuple[list[str], dict]:
        if response.status_code >= 500:
            raise KrakenError(f"HTTP {response.status_code} from Kraken")
        try:
            payload = response.json()
        except ValueError:
            raise KrakenError(f"non-JSON response (HTTP {response.status_code}): {response.text[:200]}")
        return payload.get("error") or [], payload.get("result") or {}

    def _public(self, method: str, params: dict | None = None) -> dict:
        path = f"/0/public/{method}"
        delay = 1.0
        last_error: Exception | None = None
        for _ in range(self._max_retries):
            try:
                resp = self._session.get(self.BASE + path, params=params or {}, timeout=self._timeout)
                errors, result = self._envelope(resp)
                if errors and _is_retryable(errors):
                    last_error = KrakenError("; ".join(errors))
                elif errors:
                    raise KrakenError("; ".join(errors))
                else:
                    return result
            except (requests.RequestException, KrakenError) as exc:
                last_error = exc
                if isinstance(exc, KrakenError) and "HTTP 5" not in str(exc) and not _is_retryable([str(exc)]):
                    raise
            time.sleep(delay)
            delay *= 2
        raise KrakenError(f"public/{method} failed after {self._max_retries} attempts: {last_error}")

    def _private(self, method: str, data: dict | None = None, retry: bool = False) -> dict:
        if not self._key or not self._secret:
            raise KrakenError("private endpoint called without API credentials")
        path = f"/0/private/{method}"
        # Order-placing calls are never retried: a retried AddOrder that actually
        # succeeded the first time trades twice.
        attempts = self._max_retries if retry else 1
        delay = 1.0
        last_error: Exception | None = None
        for _ in range(attempts):
            body = dict(data or {})
            body["nonce"] = self._nonce()
            headers = {"API-Key": self._key, "API-Sign": self._signature(path, body)}
            try:
                resp = self._session.post(self.BASE + path, data=body, headers=headers, timeout=self._timeout)
                errors, result = self._envelope(resp)
                if errors and retry and _is_retryable(errors):
                    last_error = KrakenError("; ".join(errors))
                elif errors:
                    raise KrakenError("; ".join(errors))
                else:
                    return result
            except requests.RequestException as exc:
                # A network failure on AddOrder is genuinely ambiguous — the order
                # may or may not have landed. Surface it rather than guessing.
                raise KrakenError(f"private/{method} network failure: {exc}") from exc
            time.sleep(delay)
            delay *= 2
        raise KrakenError(f"private/{method} failed: {last_error}")

    # ----------------------------------------------------------------- public

    def asset_pairs(self) -> dict:
        return self._public("AssetPairs")

    def assets(self) -> dict:
        """Per-asset funding status: enabled / deposit_only / withdrawal_only / …

        Decides whether a cross-venue price gap is capturable at all — an asset
        you cannot deposit is one whose gap you cannot close.
        """
        return self._public("Assets")

    def ticker(self, pair: str | None = None) -> dict:
        return self._public("Ticker", {"pair": pair} if pair else None)

    def depth(self, pair: str, count: int = 10) -> dict:
        return self._public("Depth", {"pair": pair, "count": count})

    def system_status(self) -> dict:
        return self._public("SystemStatus")

    # ---------------------------------------------------------------- private

    def balance(self) -> dict:
        return self._private("Balance", retry=True)

    def trade_volume(self, pair: str | None = None) -> dict:
        data = {"pair": pair} if pair else {}
        return self._private("TradeVolume", data, retry=True)

    def query_orders(self, txid: str) -> dict:
        return self._private("QueryOrders", {"txid": txid}, retry=True)

    def add_order(
        self,
        pair: str,
        side: str,
        ordertype: str,
        volume: str,
        price: str | None = None,
        timeinforce: str | None = None,
        userref: int | None = None,
        validate: bool = True,
    ) -> dict:
        data: dict[str, object] = {
            "pair": pair,
            "type": side,
            "ordertype": ordertype,
            "volume": volume,
        }
        if price is not None:
            data["price"] = price
        if timeinforce:
            data["timeinforce"] = timeinforce
        if userref is not None:
            data["userref"] = userref
        if validate:
            # Kraken validates inputs and returns the order description without
            # ever placing it. This is what paper mode rides on.
            data["validate"] = "true"
        return self._private("AddOrder", data, retry=False)
