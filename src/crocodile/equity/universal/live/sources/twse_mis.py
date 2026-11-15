"""Taiwan's Market Information System (``mis.twse.com.tw``): five-level books for TWSE and TPEx.

One GET to ``/stock/api/getStockInfo.jsp`` carries many listings, addressed as
``tse_<code>.tw`` (Taiwan Stock Exchange, ``XTAI``) or ``otc_<code>.tw`` (Taipei Exchange,
``ROCO``), joined with ``|``. Each row states the last print (``z`` price, ``tv`` size), the
session's open/high/low (``o``/``h``/``l``), the previous close (``y``), the cumulative volume
(``v``) and the five best asks (``a`` prices ascending, ``f`` sizes) and bids (``b`` prices
descending, ``g`` sizes), each list joined with ``_``. ``tlong`` is the publisher's stamp in
epoch milliseconds.

Units. The system quotes every size in board lots of 1,000 shares (the regular trading unit of
both markets). Sizes are converted to shares here, so a snapshot's sizes and volume are in the
same unit as every other source's.

Ruling: ``z`` is ``"-"`` on a poll in which nothing traded since the previous publication. The
row then has a book and no last price, and the snapshot says exactly that (``last`` is ``None``,
no trade record is built). The last price is not filled in from the previous close or from the
book. Cost if wrong: a consumer would read a print that did not happen.

The publisher is the exchange. The data is real time during the session.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    millis_ns,
    to_float,
)
from crocodile.equity.universal.live.model import (
    LiveLevel,
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = ["LOT_SHARES", "QUOTE_URL", "SOURCE", "TwseMisLive", "channel_key", "parse_quotes"]

SOURCE: Final = "twse_mis"
QUOTE_URL: Final = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
LOT_SHARES: Final = 1000.0
_PREFIX: Final[Mapping[str, str]] = {"XTAI": "tse", "ROCO": "otc"}
_MIC_OF_EX: Final[Mapping[str, str]] = {"tse": "XTAI", "otc": "ROCO"}
_CURRENCY: Final = "TWD"


def channel_key(listing: LiveListing) -> str:
    """The ``ex_ch`` token of one listing, e.g. ``tse_2330.tw``."""
    return f"{_PREFIX[listing.mic]}_{listing.local_symbol}.tw"


def _split(raw: Any) -> list[float | None]:
    if not isinstance(raw, str):
        return []
    return [to_float(part) for part in raw.split("_") if part != ""]


def _ladder(prices: Any, sizes: Any, *, descending: bool) -> tuple[LiveLevel, ...]:
    px, sz = _split(prices), _split(sizes)
    pairs = [
        (price, None if index >= len(sz) or sz[index] is None else (sz[index] or 0.0) * LOT_SHARES)
        for index, price in enumerate(px)
    ]
    return clean_levels(pairs, descending=descending)


def _lots(value: Any) -> float | None:
    number = to_float(value)
    return None if number is None else number * LOT_SHARES


def parse_quotes(
    body: bytes, listings: Sequence[LiveListing], *, captured_ns: int
) -> list[LiveSnapshot]:
    """Read one ``getStockInfo`` answer into snapshots for the requested listings."""
    document = decode_json(SOURCE, body)
    rows = document.get("msgArray") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise LivePayloadError(SOURCE, "no msgArray in the answer")
    wanted = {(listing.mic, listing.local_symbol): listing for listing in listings}
    out: list[LiveSnapshot] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mic = _MIC_OF_EX.get(str(row.get("ex", "")))
        code = row.get("c")
        if mic is None or not isinstance(code, str):
            continue
        listing = wanted.get((mic, code))
        if listing is None:
            continue
        name = row.get("n")
        snapshot = LiveSnapshot(
            source=SOURCE,
            listing=listing,
            symbol_raw=str(row.get("ch", code)),
            captured_ns=captured_ns,
            source_ts_ns=millis_ns(row.get("tlong")),
            last=to_float(row.get("z")),
            last_size=_lots(row.get("tv")) if to_float(row.get("z")) is not None else None,
            prev_close=to_float(row.get("y")),
            open=to_float(row.get("o")),
            high=to_float(row.get("h")),
            low=to_float(row.get("l")),
            volume=_lots(row.get("v")),
            bids=_ladder(row.get("b"), row.get("g"), descending=True),
            asks=_ladder(row.get("a"), row.get("f"), descending=False),
            currency=_CURRENCY,
            price_scale=0,
            delay_s=0,
            name=name if isinstance(name, str) else None,
        )
        if not snapshot.is_empty:
            out.append(snapshot)
    return out


class TwseMisLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = (
        "Source: Taiwan Stock Exchange and Taipei Exchange, Market Information System"
    )
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset(_PREFIX)
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 5
    delay_s: ClassVar[int | None] = 0
    has_tape: ClassVar[bool] = True
    batch_size: ClassVar[int] = 50
    min_interval_s: ClassVar[float] = 5.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        served = [listing for listing in listings if self.serves(listing)]
        if not served:
            return []
        keys = "|".join(channel_key(listing) for listing in served)
        response = await self._get(
            QUOTE_URL, params=(("ex_ch", keys), ("json", "1"), ("delay", "0"))
        )
        return parse_quotes(response.body, served, captured_ns=captured_ns)
