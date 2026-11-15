"""Shanghai Stock Exchange's own quote service (``yunhq.sse.com.cn``): five-level books for XSHG.

Two GETs per listing, addressed by the six-digit code (``600519``) of the listing:

- ``/v1/sh1/snap/<code>`` with ``select=name,prev_close,open,high,low,last,volume,amount,bid,ask,
  tradephase``. The answer is ``{"code", "date" (YYYYMMDD), "time" (HHMMSS), "snap": [...]}``
  where ``snap`` is positional in the order of the ``select`` list. ``bid`` and ``ask`` are flat
  arrays alternating price and size, five levels, best first.
- ``/v1/sh1/trd1/<code>`` with ``begin=-20&end=-1&select=time,price,volume``. The answer is
  ``{"trd1": [[HHMMSS, price, volume], ...]}``, oldest first: the publisher's own prints. Each
  row's time is an exchange-local wall clock on the day stated by the snapshot.

Units. Volume and sizes are stated in shares already: in the captured answer the session's
``amount`` divided by its ``volume`` is 1401.42, a price inside the day's range (1388.11 to
1411.00), so ``volume`` is shares and ``amount`` is turnover in CNY. Nothing is converted.
Prices are yuan per share with four decimals and are kept as published.

Ruling: a price of zero (or less) is the publisher's "nothing here" and is stated as ``None``, so
a snapshot never carries a 0.0 last price. A volume or turnover of zero is a true figure (no
trade yet); a negative one is refused.

Ruling: the service states no delay, so ``delay_s`` is ``None``. The snapshot's timestamp is the
publisher's own date and time, read as Asia/Shanghai wall clock; nothing is read from the clock.
``last_size`` is always ``None``: the tape carries the size of every print, and a last price
without its size produces no trade.

Ruling: a trade request that fails with a payload error leaves the snapshot in place with no
prints, because the snapshot is a separate and complete answer. A wall on either request is not
caught here; it propagates to the feed.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from datetime import date, datetime, time
from typing import Any, ClassVar, Final
from zoneinfo import ZoneInfo

from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import Side
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
    LivePrint,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = [
    "SNAP_FIELDS",
    "SNAP_URL",
    "SOURCE",
    "TRADES_FIELDS",
    "TRADES_URL",
    "SseYunhqLive",
    "parse_snap",
    "parse_trades",
]

SOURCE: Final = "sse_yunhq"
SNAP_URL: Final = "https://yunhq.sse.com.cn:32042/v1/sh1/snap/{code}"
TRADES_URL: Final = "https://yunhq.sse.com.cn:32042/v1/sh1/trd1/{code}"
SNAP_FIELDS: Final = "name,prev_close,open,high,low,last,volume,amount,bid,ask,tradephase"
TRADES_FIELDS: Final = "time,price,volume"
_MARKET_MIC: Final = "XSHG"
_CURRENCY: Final = "CNY"
_SNAP_ITEMS: Final = 10
_NAME, _PREV_CLOSE, _OPEN, _HIGH, _LOW, _LAST, _VOLUME, _AMOUNT, _BID, _ASK = range(10)


def _is_code(text: str) -> bool:
    return len(text) == 6 and text.isascii() and text.isdigit()


def _day(value: Any) -> date | None:
    """A YYYYMMDD integer as a date, or ``None``. Out-of-range integers are refused first."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 19000101 <= value <= 99991231:
        return None
    try:
        return date(value // 10000, value // 100 % 100, value % 100)
    except ValueError:
        return None


def _clock(value: Any) -> time | None:
    """An HHMMSS integer as a time, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 235959:
        return None
    try:
        return time(value // 10000, value // 100 % 100, value % 100)
    except ValueError:
        return None


def _ladder(flat: list[Any], *, descending: bool) -> tuple[LiveLevel, ...]:
    """A flat price,size,price,size,... array as levels, published order kept."""
    pairs = [
        (to_float(flat[index]), to_float(flat[index + 1]) if index + 1 < len(flat) else None)
        for index in range(0, len(flat), 2)
    ]
    return clean_levels(pairs, descending=descending)


def _positive(value: float) -> bool:
    return math.isfinite(value) and value > 0.0


def _price(value: Any) -> float | None:
    """A published price, or ``None``. Zero and negatives are the publisher's "nothing here"."""
    number = to_float(value)
    return number if number is not None and number > 0.0 else None


def _amount(value: Any) -> float | None:
    """A published volume or turnover, or ``None``. Zero is a true figure: no trade yet."""
    number = to_float(value)
    return number if number is not None and number >= 0.0 else None


def parse_snap(body: bytes, listing: LiveListing, *, captured_ns: int) -> LiveSnapshot | None:
    """Read one snap answer into the snapshot of ``listing``.

    ``None`` when the answer is for another code or carries no price at all. Raises
    :class:`LivePayloadError` when the document is not a snap answer: no ``snap`` array of ten
    items, or a bid or ask that is not a list.
    """
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "snap answer is not an object")
    code = document.get("code")
    if not isinstance(code, str):
        raise LivePayloadError(SOURCE, "snap answer has no code")
    if code != listing.local_symbol:
        return None
    snap = document.get("snap")
    if not isinstance(snap, list) or len(snap) < _SNAP_ITEMS:
        raise LivePayloadError(SOURCE, f"snap array of {_SNAP_ITEMS} items is missing")
    bid_flat, ask_flat = snap[_BID], snap[_ASK]
    if not isinstance(bid_flat, list) or not isinstance(ask_flat, list):
        raise LivePayloadError(SOURCE, "bid or ask is not a list")
    day, clock = _day(document.get("date")), _clock(document.get("time"))
    name = snap[_NAME]
    snapshot = LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=code,
        captured_ns=captured_ns,
        source_ts_ns=(
            local_datetime_ns(day, clock, timezone_of(_MARKET_MIC))
            if day is not None and clock is not None
            else None
        ),
        last=_price(snap[_LAST]),
        last_size=None,
        prev_close=_price(snap[_PREV_CLOSE]),
        open=_price(snap[_OPEN]),
        high=_price(snap[_HIGH]),
        low=_price(snap[_LOW]),
        volume=_amount(snap[_VOLUME]),
        turnover=_amount(snap[_AMOUNT]),
        bids=_ladder(bid_flat, descending=True),
        asks=_ladder(ask_flat, descending=False),
        currency=_CURRENCY,
        price_scale=0,
        delay_s=None,
        name=name if isinstance(name, str) else None,
    )
    return None if snapshot.is_empty else snapshot


def parse_trades(body: bytes, day: date, tz: ZoneInfo) -> tuple[LivePrint, ...]:
    """Read one trd1 answer into the publisher's prints, oldest first.

    Each row is ``[HHMMSS, price, volume]`` on ``day`` in ``tz``. A row whose price or volume is
    not a positive number is not a print and is dropped. A row whose time does not parse keeps
    its price and size with ``ts_ns=None``. Raises :class:`LivePayloadError` when the answer has
    no ``trd1`` list or a row is not a list of at least three items.
    """
    document = decode_json(SOURCE, body)
    rows = document.get("trd1") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise LivePayloadError(SOURCE, "no trd1 list in the answer")
    prints: list[LivePrint] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            raise LivePayloadError(SOURCE, "trd1 row is not [time, price, volume]")
        price, size = to_float(row[1]), to_float(row[2])
        if price is None or size is None or not _positive(price) or not _positive(size):
            continue
        clock = _clock(row[0])
        prints.append(
            LivePrint(
                price=price,
                size=size,
                ts_ns=local_datetime_ns(day, clock, tz) if clock is not None else None,
                side=Side.UNKNOWN,
                id=None,
            )
        )
    return tuple(prints)


class SseYunhqLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: Shanghai Stock Exchange (yunhq.sse.com.cn)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({_MARKET_MIC})
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 5
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = True
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 5.0

    def serves(self, listing: LiveListing) -> bool:
        return super().serves(listing) and _is_code(listing.local_symbol)

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            if not self.serves(listing):
                continue
            code = listing.local_symbol
            response = await self._get(
                SNAP_URL.format(code=code), params=(("select", SNAP_FIELDS),)
            )
            snapshot = parse_snap(response.body, listing, captured_ns=captured_ns)
            if snapshot is None:
                continue
            out.append(await self._with_prints(snapshot, code))
        return out

    async def _with_prints(self, snapshot: LiveSnapshot, code: str) -> LiveSnapshot:
        if snapshot.source_ts_ns is None:
            return snapshot
        tz = timezone_of(_MARKET_MIC)
        day = datetime.fromtimestamp(snapshot.source_ts_ns // 1_000_000_000, tz=tz).date()
        try:
            response = await self._get(
                TRADES_URL.format(code=code),
                params=(("begin", "-20"), ("end", "-1"), ("select", TRADES_FIELDS)),
            )
            prints = parse_trades(response.body, day, tz)
        except LivePayloadError:
            return snapshot
        return dataclasses.replace(snapshot, prints=prints)
