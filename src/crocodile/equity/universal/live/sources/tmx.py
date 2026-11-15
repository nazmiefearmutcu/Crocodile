"""TMX Group's money.tmx.com quote, the last price and session figures of TSX and TSX Venture.

One POST per listing to ``app-money.tmx.com/graphql``, operation ``getQuoteBySymbol``, with the
local symbol and ``locale`` ``en`` as variables. The answer is one object under
``data.getQuoteBySymbol``: the last ``price``, the session's ``openPrice``, ``dayHigh``,
``dayLow``, the cumulative ``volume`` and the previous close (``prevClose``), plus the exchange
that quoted it (``exchangeCode``) and the ``currency``. It carries no book and no print, so a
snapshot is tier ``LAST`` and produces no records.

Units. Prices are in dollars of the quoted currency, not cents: the fixture's ``priceChange``
(4.45) is exactly ``price`` (212.57) less ``prevClose`` (208.12), and ``percentChange`` (2.13819)
is that difference over ``prevClose``. ``volume`` is passed through unchanged and read as a count
of shares. The evidence for that is the field name and the magnitude only: the response carries
no unit field, so the share unit is an assumption the fixture does not prove.

Ruling: the same ticker can trade on both boards. ``exchangeCode`` ``TSX`` is ``XTSE`` and
``TSXV`` is ``XTSX``; an answer whose exchange maps to a different MIC than the listing asked for
(or to none) is absent from the result, never attributed to the other board. Cost if wrong: a
TSX Venture quote would be read as the TSX price of the same ticker.

Ruling: the publisher states a delay of fifteen minutes for money.tmx.com quotes, so the
snapshot carries ``delay_s`` of 900 and no timestamp is invented: the answer has none.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Final

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    currency_of,
    decode_json,
    to_float,
)
from crocodile.equity.universal.live.model import LiveListing, LiveSnapshot, LiveTier

__all__ = ["GRAPHQL_URL", "QUERY", "SOURCE", "TmxLive", "parse_quote"]

SOURCE: Final = "tmx"
GRAPHQL_URL: Final = "https://app-money.tmx.com/graphql"
QUERY: Final = (
    "query getQuoteBySymbol($symbol: String, $locale: String) { getQuoteBySymbol(symbol: $symbol,"
    " locale: $locale) { symbol name price priceChange percentChange exchangeName exShortName"
    " exchangeCode volume openPrice dayHigh dayLow prevClose currency } }"
)
_MIC_OF_EXCHANGE: Final[dict[str, str]] = {"TSX": "XTSE", "TSXV": "XTSX"}
_DELAY_S: Final = 900


def _body(listing: LiveListing) -> dict[str, Any]:
    return {
        "operationName": "getQuoteBySymbol",
        "variables": {"symbol": listing.local_symbol, "locale": "en"},
        "query": QUERY,
    }


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() != "" else None


def _price(value: Any) -> float | None:
    """A published price, or ``None``: zero and negative figures are not prices."""
    number = to_float(value)
    return number if number is not None and number > 0.0 else None


def _size(value: Any) -> float | None:
    """A published volume, or ``None``: a published zero is kept, a negative figure is not."""
    number = to_float(value)
    return number if number is not None and number >= 0.0 else None


def parse_quote(body: bytes, listing: LiveListing, *, captured_ns: int) -> list[LiveSnapshot]:
    """Read one ``getQuoteBySymbol`` answer into the snapshot of the listing it was asked for.

    An answer with no quote (``getQuoteBySymbol`` is null), one from the other board of the
    ticker, or one that names a different symbol, yields no snapshot. An ``errors`` answer with
    no data, an answer without the ``getQuoteBySymbol`` key, or a body that is not the expected
    object, raises :class:`LivePayloadError`.
    """
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "answer is not a JSON object")
    data = document.get("data")
    if not isinstance(data, dict) or "getQuoteBySymbol" not in data:
        raise LivePayloadError(SOURCE, "no getQuoteBySymbol in the answer")
    quote = data["getQuoteBySymbol"]
    if quote is None:
        return []
    if not isinstance(quote, dict):
        raise LivePayloadError(SOURCE, "getQuoteBySymbol is not an object")
    if _MIC_OF_EXCHANGE.get(str(quote.get("exchangeCode", ""))) != listing.mic:
        return []
    answered = _text(quote.get("symbol"))
    if answered is not None and answered != listing.local_symbol:
        return []
    published = _text(quote.get("currency"))
    snapshot = LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=answered or listing.local_symbol,
        captured_ns=captured_ns,
        last=_price(quote.get("price")),
        prev_close=_price(quote.get("prevClose")),
        open=_price(quote.get("openPrice")),
        high=_price(quote.get("dayHigh")),
        low=_price(quote.get("dayLow")),
        volume=_size(quote.get("volume")),
        currency=published if published is not None else currency_of(listing.mic),
        price_scale=0,
        delay_s=_DELAY_S,
        name=_text(quote.get("name")),
    )
    return [] if snapshot.is_empty else [snapshot]


class TmxLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: TMX Group (money.tmx.com)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({"XTSE", "XTSX"})
    max_tier: ClassVar[LiveTier] = LiveTier.LAST
    depth_levels: ClassVar[int] = 0
    delay_s: ClassVar[int | None] = _DELAY_S
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
            response = await self._post(GRAPHQL_URL, _body(listing))
            out.extend(parse_quote(response.body, listing, captured_ns=captured_ns))
        return out
