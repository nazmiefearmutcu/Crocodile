"""Shenzhen Stock Exchange's own site API (``www.szse.cn``): a five-level book per listing.

One GET to ``/api/market/ssjjhq/getTimeData`` quotes one listing, addressed by ``marketId=1``
(the Shenzhen market) and its six-digit ``code``. The answer carries the previous close
(``close``), the session's open/high/low, the last price (``now``), the size of the last trade
(``lastVolume``), the cumulative volume (``volume``) and turnover (``amount``), the publisher's
own stamp (``marketTime``, exchange-local wall clock) and ``sellbuy5``: ten levels, the first five
asks (lowest first) and the last five bids (highest first). The ``pic*`` arrays are not read.

Units. ``volume``, ``lastVolume`` and every ``sellbuy5`` size are in lots of 100 shares; they are
converted to shares here. Evidence in the captured answer of ``000001`` (2026-11-12, closing
``marketTime`` 15:30:00): ``amount / (volume * 100)`` is 11.76 against a last price of 11.79, which
holds only for a lot of 100 shares (a lot of 1 would give 0.0118). The 15:00 row of
``picupdata`` carries 12,186 lots and an amount of 14,366,928.51, which is 11.79 per share at a lot
of 100 and matches ``lastVolume`` = 12186. Prices and turnover are in CNY.

Ruling: the answer's ``data.code`` must be the code that was asked for; a different code is not
this listing's answer and yields no snapshot. A ``code`` other than ``"0"`` on the outer envelope,
or a missing ``data`` object, is a payload error. A ``now`` the publisher leaves as a placeholder
is ``None``: the last price is not filled in from ``close`` or from the book. A zero or
non-numeric ``close``, ``open``, ``high``, ``low`` or ``now`` is ``None`` too (a zero is the
placeholder for "no trade yet"), while a published zero volume or turnover stays ``0``. Cost if
wrong: a consumer would read a placeholder or a previous close as a trade.

The publisher is the exchange. It states no delay, so none is claimed.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Final

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    local_datetime_ns,
    timezone_of,
    to_float,
)
from crocodile.equity.universal.live.model import (
    LiveLevel,
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = ["LOT_SHARES", "QUOTE_URL", "SOURCE", "SzseLive", "parse_quote"]

SOURCE: Final = "szse"
QUOTE_URL: Final = "https://www.szse.cn/api/market/ssjjhq/getTimeData"
MARKET_ID: Final = "1"
LOT_SHARES: Final = 100.0
_MIC: Final = "XSHE"
_CURRENCY: Final = "CNY"
_LEVELS: Final = 5
_STAMP_FORMAT: Final = "%Y-%m-%d %H:%M:%S"


def _lots(value: Any) -> float | None:
    number = to_float(value)
    return None if number is None else number * LOT_SHARES


def _positive_lots(value: Any) -> float | None:
    shares = _lots(value)
    return shares if shares is not None and shares > 0.0 else None


def _price(value: Any) -> float | None:
    """A published price; zero or negative is a placeholder for "no trade yet", so ``None``."""
    number = to_float(value)
    return number if number is not None and number > 0.0 else None


def _ladder(items: Sequence[Any], *, descending: bool) -> tuple[LiveLevel, ...]:
    pairs: list[tuple[float | None, float | None]] = []
    for item in items:
        if isinstance(item, dict):
            pairs.append((to_float(item.get("price")), _lots(item.get("volume"))))
        else:
            pairs.append((None, None))
    return clean_levels(pairs, descending=descending)


def _book(items: Any) -> tuple[tuple[LiveLevel, ...], tuple[LiveLevel, ...]]:
    """The ``(bids, asks)`` of a ``sellbuy5`` list; two empty ladders when its layout is unknown."""
    if not isinstance(items, list) or len(items) != 2 * _LEVELS:
        return (), ()
    asks = _ladder(items[:_LEVELS], descending=False)
    bids = _ladder(items[_LEVELS:], descending=True)
    return bids, asks


def _stamp_ns(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.strptime(value.strip(), _STAMP_FORMAT)
    except ValueError:
        return None
    return local_datetime_ns(moment.date(), moment.time(), timezone_of(_MIC))


def parse_quote(body: bytes, listing: LiveListing, *, captured_ns: int) -> LiveSnapshot | None:
    """Read one ``getTimeData`` answer into the snapshot of the listing it was asked for.

    ``None`` when the answer is for another code or carries no price at all.
    """
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict) or document.get("code") != "0":
        raise LivePayloadError(SOURCE, "answer code is not 0")
    data = document.get("data")
    if not isinstance(data, dict):
        raise LivePayloadError(SOURCE, "no data object in the answer")
    if data.get("code") != listing.local_symbol:
        return None
    bids, asks = _book(data.get("sellbuy5"))
    last = _price(data.get("now"))
    name = data.get("name")
    snapshot = LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=listing.local_symbol,
        captured_ns=captured_ns,
        source_ts_ns=_stamp_ns(data.get("marketTime")),
        last=last,
        last_size=_positive_lots(data.get("lastVolume")) if last is not None else None,
        prev_close=_price(data.get("close")),
        open=_price(data.get("open")),
        high=_price(data.get("high")),
        low=_price(data.get("low")),
        volume=_lots(data.get("volume")),
        turnover=to_float(data.get("amount")),
        bids=bids,
        asks=asks,
        currency=_CURRENCY,
        price_scale=0,
        name=name if isinstance(name, str) else None,
    )
    return None if snapshot.is_empty else snapshot


class SzseLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: Shenzhen Stock Exchange (www.szse.cn)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({_MIC})
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 5
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = True
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 5.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            if not self.serves(listing):
                continue
            response = await self._get(
                QUOTE_URL, params=(("marketId", MARKET_ID), ("code", listing.local_symbol))
            )
            snapshot = parse_quote(response.body, listing, captured_ns=captured_ns)
            if snapshot is not None:
                out.append(snapshot)
        return out
