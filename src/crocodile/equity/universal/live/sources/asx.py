"""ASX's own website API (Markit Digital, ``asx.api.markitdigital.com``): one listing's header.

One GET per listing to ``/asx-research/1.0/companies/<code>/header`` answers the listing's header:
the last trade price (``priceLast``), the best bid and ask prices (``priceBid``, ``priceAsk``), the
change on the day (``priceChange``) and the cumulative session volume (``volume``). Each field is
read as the publisher states it; the answer carries no sizes, no open, high or low, no tape and no
timestamp, and none of those are filled in.

Units. Prices are in Australian dollars (AUD), the unit the ASX quotes in, not cents. Evidence in
the fixture ``header_CBA.json``: ``priceLast`` is 171.76 for Commonwealth Bank, and its
``marketCap`` of 279,367,806,045 divided by that price is about 1.63 billion shares, a plausible
share count for a bank of that size, which places the price in dollars. ``volume`` is in shares:
2,305,426 shares at 171.76 is about A$396 million of turnover, a plausible session for that bank.

Ruling: the publisher states the top of book as prices only. The snapshot carries one bid and one
ask level with ``size`` ``None``, so the tier is ``L1`` and :func:`snapshot_records` yields neither
a quote nor a book: a quote without sizes would be invented. Cost if wrong: a consumer would
display a size the exchange did not publish.

Ruling: the publisher states that its prices are delayed by at least twenty minutes, so
``delay_s`` is 1200 and the figures are never presented as real time.

Ruling: an answer whose ``data.symbol`` is not the listing's local symbol is about another
listing, so it yields no snapshot for the one asked for.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = ["DELAY_S", "HEADER_URL", "SOURCE", "AsxLive", "header_url", "parse_header"]

SOURCE: Final = "asx"
HEADER_URL: Final = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/header"
DELAY_S: Final = 1200
_CURRENCY: Final = "AUD"


def header_url(local_symbol: str) -> str:
    """The header endpoint of one ASX listing, e.g. ``.../companies/CBA/header``."""
    return HEADER_URL.format(code=quote(local_symbol, safe=""))


def _price(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number > 0.0 else None


def _quantity(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number >= 0.0 else None


def parse_header(body: bytes, listing: LiveListing, *, captured_ns: int) -> LiveSnapshot | None:
    """Read one header answer into the snapshot of ``listing``, or ``None`` if it has no price."""
    document = decode_json(SOURCE, body)
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict):
        raise LivePayloadError(SOURCE, "no data object in the answer")
    if data.get("symbol") != listing.local_symbol:
        return None
    last = _price(data.get("priceLast"))
    change = to_float(data.get("priceChange"))
    prev_close = _price(
        round(last - change, 6) if last is not None and change is not None else None
    )
    bids = clean_levels([(to_float(data.get("priceBid")), None)], descending=True)
    asks = clean_levels([(to_float(data.get("priceAsk")), None)], descending=False)
    name = data.get("displayName")
    snapshot = LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=listing.local_symbol,
        captured_ns=captured_ns,
        last=last,
        prev_close=prev_close,
        volume=_quantity(data.get("volume")),
        bids=bids,
        asks=asks,
        currency=_CURRENCY,
        price_scale=0,
        delay_s=DELAY_S,
        name=name if isinstance(name, str) else None,
    )
    return None if snapshot.is_empty else snapshot


class AsxLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: ASX (asx.com.au)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({"XASX"})
    max_tier: ClassVar[LiveTier] = LiveTier.L1
    depth_levels: ClassVar[int] = 1
    delay_s: ClassVar[int | None] = DELAY_S
    has_tape: ClassVar[bool] = False
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 30.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            if not self.serves(listing):
                continue
            response = await self._get(header_url(listing.local_symbol))
            snapshot = parse_header(response.body, listing, captured_ns=captured_ns)
            if snapshot is not None:
                out.append(snapshot)
        return out
