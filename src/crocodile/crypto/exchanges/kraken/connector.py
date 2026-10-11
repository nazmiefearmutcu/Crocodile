"""Kraken public L2 or token-authenticated visible L3, without private capture."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from crocodile.core.capture.schema import RawFrame
from crocodile.core.errors import ConfigError
from crocodile.core.ingest.transport import Transport
from crocodile.core.sink.base import Sink
from crocodile.crypto.exchanges.public_books import GatedConnector, PublicBookError
from crocodile.crypto.instruments.registry import Instrument, InstrumentRegistry, Kind

from .book import KrakenBookGate


def token_signature(nonce: str, secret: str) -> str:
    body = urlencode({"nonce": nonce})
    message = b"/0/private/GetWebSocketsToken" + hashlib.sha256((nonce + body).encode()).digest()
    return base64.b64encode(
        hmac.new(base64.b64decode(secret, validate=True), message, hashlib.sha512).digest()
    ).decode()


class KrakenConnector(GatedConnector):
    name = "kraken"
    market = "spot"
    rest_url = "https://api.kraken.com"
    decoder_id = "kraken-v2"
    quantity_unit = "base_asset"

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        *,
        mode: str = "book",
        depth: int = 10,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        if mode not in {"book", "level3"} or depth not in (
            {10, 100, 1000} if mode == "level3" else {10, 25, 100, 500, 1000}
        ):
            raise ConfigError("Kraken mode/depth is not supported")
        if mode == "level3" and not (api_key and api_secret):
            raise ConfigError("Kraken level3 requires CROCODILE_KRAKEN_API_KEY and API_SECRET")
        if mode == "level3":
            try:
                base64.b64decode(api_secret or "", validate=True)
            except (ValueError, TypeError):
                raise ConfigError("Kraken API secret must be base64 encoded") from None
        if any(c not in {"book_delta", "book_snapshot"} for c in channels):
            raise ConfigError("Kraken native connector supports book channels")
        super().__init__(symbols, channels, out, registry)
        self.mode, self.depth = mode, depth
        self._api_key, self._api_secret = api_key, api_secret
        self.ws_url = (
            "wss://ws-auth.kraken.com/v2" if mode == "level3" else "wss://ws.kraken.com/v2"
        )
        self.capture_endpoint_id = f"kraken-{mode}-{depth}"
        self.coverage = {
            "price_levels_per_side": depth,
            "visible_orders_only": mode == "level3",
            "checksum_price_levels_per_side": 10,
            "cadence": "realtime",
        }
        self.gate = KrakenBookGate(mode=mode, depth=depth)
        self._nonce = 0

    def subscribe_channels(self) -> list[str]:
        return [self.mode]

    async def _token(self) -> str:
        self._nonce = max(time.time_ns() // 1000, self._nonce + 1)
        nonce = str(self._nonce)
        if self._api_key is None or self._api_secret is None:
            raise PublicBookError("Kraken L3 authorization is not configured")
        headers = {"API-Key": self._api_key, "API-Sign": token_signature(nonce, self._api_secret)}
        try:
            async with self.http_session.post(
                self.rest_url + "/0/private/GetWebSocketsToken",
                data={"nonce": nonce},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except (aiohttp.ClientError, ValueError):
            raise PublicBookError("Kraken token request failed") from None
        token = (data.get("result") or {}).get("token")
        if data.get("error") or not isinstance(token, str) or not token:
            raise PublicBookError("Kraken WebSocket authorization rejected")
        return token

    async def _subscribe(self, transport: Transport) -> None:
        params: dict[str, Any] = {
            "channel": self.mode,
            "symbol": self.symbols,
            "depth": self.depth,
            "snapshot": True,
        }
        if self.mode == "level3":
            params["token"] = await self._token()
        await transport.send(json.dumps({"method": "subscribe", "params": params}).encode())

    async def _handle_control_message(self, msg: object, frame: RawFrame) -> bool:
        if isinstance(msg, dict) and (msg.get("success") is False or msg.get("error")):
            raise PublicBookError("Kraken subscription rejected")
        return True

    async def list_instruments(self) -> list[Instrument]:
        raw = await self.http_get(self.rest_url + "/0/public/AssetPairs")
        if raw.get("error"):
            raise PublicBookError("Kraken instrument query rejected")
        result = []
        for item in raw["result"].values():
            symbol = item.get("wsname")
            if not symbol or "/" not in symbol:
                continue
            symbol = symbol.replace("XBT", "BTC")
            base, quote = symbol.split("/", 1)
            result.append(
                Instrument(
                    canonical=f"kraken:{symbol}",
                    exchange="kraken",
                    symbol_raw=symbol,
                    kind=Kind.SPOT,
                    base=base,
                    quote=quote,
                    tick_size=10 ** -item["pair_decimals"],
                )
            )
        return result
