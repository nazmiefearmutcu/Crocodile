"""Tradegate Exchange's own live quote (``www.tradegatebsx.com``): one flat object per ISIN.

One GET to ``/refresh.php?isin=<isin>`` answers one listing, so the source is keyed by ISIN and
declares ``needs_isin``. The answer is a flat JSON object: ``bid`` and ``bidsize``, ``ask`` and
``asksize`` (the best quote and its size), ``last``, ``high``, ``low``, ``close`` (the PREVIOUS
close), ``stueck`` (cumulative volume), ``umsatz`` (turnover), ``avg``, ``delta`` and
``executions``. The payload does not echo the ISIN, so the snapshot is attributed to the listing
that was asked for.

Number format. Values arrive as a mix of JSON numbers and strings in German notation: a decimal
comma, optionally with dots or spaces as thousands separators (``"212,60"``, ``"1.234,56"``).
:func:`german_number` reads both. Ruling: a dot in a string with no comma is a decimal point,
as the spec states it; the publisher is not seen to write such a string.

Units. ``stueck`` is read as shares and ``umsatz`` as EUR. The fixture supports both: ``stueck``
times ``avg`` equals ``umsatz`` to within a euro (16149 x 211.9628 = 3,422,987.26 against
3,422,987), which holds only if the volume is in shares and the turnover in EUR. The bid and ask
sizes are taken as shares, as the spec states; the fixture neither confirms nor contradicts
that. No unit conversion is applied to any value.

Ruling: a missing or unparseable field is ``None``, never a default. A price of zero or less is
not a price, so it is ``None`` too. ``last`` is not filled in
from ``close`` or from the book. A size of zero is ``None``, not ``0.0``. An answer with neither
a bid, an ask nor a last price is not a quote and raises :class:`LivePayloadError`.

Ruling: no open is published, so ``open`` is ``None``. The publisher states no timestamp, so
``source_ts_ns`` is ``None``, and no delay, so ``delay_s`` is ``None``. The publisher is the
exchange itself.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, ClassVar, Final

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import LivePayloadError, LiveSource, decode_json
from crocodile.equity.universal.live.model import (
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = ["QUOTE_URL", "SOURCE", "TradegateLive", "german_number", "parse_refresh"]

SOURCE: Final = "tradegate"
QUOTE_URL: Final = "https://www.tradegatebsx.com/refresh.php"
_CURRENCY: Final = "EUR"
_PLACEHOLDERS: Final = frozenset({"", "-", "./."})


def german_number(value: Any) -> float | None:
    """A published number in German or plain notation, or ``None``.

    A JSON number passes through when finite; ``bool`` is refused. A string is stripped of
    spaces and non-breaking spaces (``\\u00a0``). When it contains a comma, every dot is a
    thousands separator and the comma is the decimal point; without a comma the string is read
    as it stands. The placeholders ``""``, ``"-"`` and ``"./."`` are ``None``, as is anything
    that does not parse to a finite number. A string with an underscore is refused: Python's
    ``float`` would read ``"1_000"`` as 1000, which no German-notation publisher writes.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(" ", "").replace("\u00a0", "")
        if text in _PLACEHOLDERS or "_" in text:
            return None
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _price(value: Any) -> float | None:
    """A published price: a finite number above zero, else ``None`` (no price is zero or less)."""
    number = german_number(value)
    return number if number is not None and number > 0.0 else None


def _quantity(value: Any) -> float | None:
    """A published count (volume or turnover): a finite number of zero or more, else ``None``."""
    number = german_number(value)
    return number if number is not None and number >= 0.0 else None


def parse_refresh(body: bytes, listing: LiveListing, *, captured_ns: int) -> LiveSnapshot:
    """Read one ``refresh.php`` answer into the snapshot of the listing it was asked for.

    ``symbol_raw`` is the ISIN the request keyed on, or the canonical symbol when the listing
    carries none.
    """
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "the answer is not a JSON object")
    bid = _price(document.get("bid"))
    ask = _price(document.get("ask"))
    last = _price(document.get("last"))
    if bid is None and ask is None and last is None:
        raise LivePayloadError(SOURCE, "no bid, ask or last price in the answer")
    bids = clean_levels([(bid, german_number(document.get("bidsize")))], descending=True)
    asks = clean_levels([(ask, german_number(document.get("asksize")))], descending=False)
    return LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=listing.isin if listing.isin is not None else listing.symbol,
        captured_ns=captured_ns,
        last=last,
        prev_close=_price(document.get("close")),
        high=_price(document.get("high")),
        low=_price(document.get("low")),
        volume=_quantity(document.get("stueck")),
        turnover=_quantity(document.get("umsatz")),
        bids=bids,
        asks=asks,
        currency=_CURRENCY,
        price_scale=0,
    )


class TradegateLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: Tradegate Exchange (tradegatebsx.com)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({"TGAT"})
    max_tier: ClassVar[LiveTier] = LiveTier.L1
    depth_levels: ClassVar[int] = 1
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = False
    needs_isin: ClassVar[bool] = True
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 5.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            if listing.isin is None or not self.serves(listing):
                continue
            response = await self._get(QUOTE_URL, params=(("isin", listing.isin),))
            out.append(parse_refresh(response.body, listing, captured_ns=captured_ns))
        return out
