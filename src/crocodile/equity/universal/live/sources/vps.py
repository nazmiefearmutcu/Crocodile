"""VPS Securities' public quote feed for Vietnam: a broker's view of the HOSE and HNX books.

This is a broker's feed, not the exchange's. ``official`` is False and the usage class is
``INTERNAL``. One GET to ``/getliststockdata/<symbols>`` carries many listings, addressed by their
local symbols joined with commas (the symbols are path text; there are no query parameters).
The answer is a JSON array with one row per listing. A row states its market (``marketId``:
``STO`` is Ho Chi Minh City, ``XSTC``; ``STX`` is Hanoi, ``HSTC``; any other market is skipped),
its symbol (``sym``), the last price (``lastPrice``) and its size (``lastVolume``), the cumulative
volume (``lot``), the previous close (``r``), the session's open/high/low (``openPrice``,
``highPrice``, ``lowPrice``, all strings), and three bid levels (``g1`` to ``g3``, best first)
and three ask levels (``g4`` to ``g6``, best first). Each level is ``"price|size|flag"``. An
empty slot is ``"0|0|e"``. A price can also be a session code such as ``"ATO"`` or ``"ATC"``
(an auction order), which has no numeric price and is dropped. ``g7`` is not read.

Units, stated on the evidence of the captured answer (``fixtures/live/vps/quotes.json``):

- Prices are in thousands of VND. ``lastPrice`` 54.2 is 54,200 VND. The same row states
  ``closePrice`` "54500.0" while its ``r`` is 54.5, so the publisher's own VND figure is the
  thousands figure times 1,000. Every price is multiplied by 1,000 and rounded to 4 decimals to
  drop float noise.
- Volumes and sizes are in units of 10 shares. ``lot`` 133730 is 1,337,300 shares. The row's
  foreign-buy figures agree: for SHS, ``fBValue`` / ``fBVol`` is 134.0, which is 10 times the
  price in thousands of VND (13.4), when the value is read in thousands of VND. Every
  ``lastVolume``, ``lot`` and ladder size is multiplied by 10.

Ruling: the answer carries no source timestamp, so ``source_ts_ns`` is ``None``. The publisher
states no delay, so ``delay_s`` is ``None``, not 0. Cost if wrong: a consumer would read the
figures as real time, or stamp them with a moment the publisher never gave.

Ruling: a price that is not positive (``0``, an empty slot, a session code) is no price and is
``None``. A size that is zero is ``None``, never 0. Ladders go through :func:`clean_levels`.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final
from urllib.parse import quote

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    to_float,
)
from crocodile.equity.universal.live.model import (
    LiveLevel,
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = ["LOT_SHARES", "PRICE_UNIT", "QUOTE_URL", "SOURCE", "VpsLive", "parse_quotes"]

SOURCE: Final = "vps"
QUOTE_URL: Final = "https://bgapidatafeed.vps.com.vn/getliststockdata/"
"""The path prefix; the comma-joined local symbols are appended to it."""

PRICE_UNIT: Final = 1000.0
"""Published prices are in thousands of VND."""

LOT_SHARES: Final = 10.0
"""Published volumes and sizes are in units of 10 shares."""

_MIC_OF_MARKET: Final[Mapping[str, str]] = {"STO": "XSTC", "STX": "HSTC"}
_MARKET_OF_MIC: Final[Mapping[str, str]] = {mic: market for market, mic in _MIC_OF_MARKET.items()}
_CURRENCY: Final = "VND"
_BID_FIELDS: Final = ("g1", "g2", "g3")
_ASK_FIELDS: Final = ("g4", "g5", "g6")


def _number(value: Any) -> float | None:
    """A published number, or ``None``. An integer too large for a float is no number either."""
    try:
        return to_float(value)
    except OverflowError:
        return None


def _price(value: Any) -> float | None:
    """A published price in VND, or ``None`` when it is not a positive, finite number."""
    number = _number(value)
    if number is None or number <= 0.0:
        return None
    scaled = round(number * PRICE_UNIT, 4)
    return scaled if math.isfinite(scaled) else None


def _shares(value: Any) -> float | None:
    """A published volume or size in shares, or ``None`` when it is not a finite number."""
    number = _number(value)
    if number is None:
        return None
    shares = number * LOT_SHARES
    return shares if math.isfinite(shares) else None


def _volume(value: Any) -> float | None:
    """A cumulative volume in shares. A negative figure is no volume, so it is ``None``."""
    shares = _shares(value)
    return shares if shares is not None and shares >= 0.0 else None


def _level(raw: Any) -> tuple[float | None, float | None]:
    """One ``"price|size|flag"`` slot as a (price, size) pair; a session code has no price."""
    if not isinstance(raw, str):
        return (None, None)
    parts = raw.split("|")
    price = _price(parts[0])
    size = _shares(parts[1]) if len(parts) > 1 else None
    return (price, size)


def _ladder(
    row: Mapping[str, Any], fields: Sequence[str], *, descending: bool
) -> tuple[LiveLevel, ...]:
    return clean_levels([_level(row.get(field)) for field in fields], descending=descending)


def parse_quotes(
    body: bytes, listings: Sequence[LiveListing], *, captured_ns: int
) -> list[LiveSnapshot]:
    """Read one ``getliststockdata`` answer into snapshots for the requested listings."""
    document = decode_json(SOURCE, body)
    if not isinstance(document, list):
        raise LivePayloadError(SOURCE, "answer is not a JSON array")
    wanted = {(listing.mic, listing.local_symbol): listing for listing in listings}
    out: list[LiveSnapshot] = []
    for row in document:
        if not isinstance(row, dict):
            continue
        mic = _MIC_OF_MARKET.get(str(row.get("marketId", "")))
        sym = row.get("sym")
        if mic is None or not isinstance(sym, str):
            continue
        listing = wanted.get((mic, sym))
        if listing is None:
            continue
        last = _price(row.get("lastPrice"))
        last_volume = _number(row.get("lastVolume"))
        snapshot = LiveSnapshot(
            source=SOURCE,
            listing=listing,
            symbol_raw=sym,
            captured_ns=captured_ns,
            source_ts_ns=None,
            last=last,
            last_size=(
                _shares(row.get("lastVolume"))
                if last is not None and last_volume is not None and last_volume > 0.0
                else None
            ),
            prev_close=_price(row.get("r")),
            open=_price(row.get("openPrice")),
            high=_price(row.get("highPrice")),
            low=_price(row.get("lowPrice")),
            volume=_volume(row.get("lot")),
            bids=_ladder(row, _BID_FIELDS, descending=True),
            asks=_ladder(row, _ASK_FIELDS, descending=False),
            currency=_CURRENCY,
            price_scale=0,
            delay_s=None,
        )
        if not snapshot.is_empty:
            out.append(snapshot)
    return out


class VpsLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    attribution: ClassVar[str | None] = None
    official: ClassVar[bool] = False
    supported_mics: ClassVar[frozenset[str]] = frozenset(_MARKET_OF_MIC)
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 3
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = True
    batch_size: ClassVar[int] = 40
    min_interval_s: ClassVar[float] = 5.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        served = [listing for listing in listings if self.serves(listing)]
        if not served:
            return []
        # Each symbol is one path segment: quoted, so a symbol can never add a path or a query.
        url = QUOTE_URL + ",".join(quote(listing.local_symbol, safe="") for listing in served)
        response = await self._get(url)
        return parse_quotes(response.body, served, captured_ns=captured_ns)
