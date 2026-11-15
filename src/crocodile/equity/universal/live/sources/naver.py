"""Naver Finance (``polling.finance.naver.com`` and ``m.stock.naver.com``): the Korea Exchange.

Covers KOSPI and KOSDAQ (both trade on the operating MIC ``XKRX``). Naver is an aggregator, not
the exchange: ``official`` is False. Two keyless GETs per listing, the local symbol being the
six-digit code:

- ``realtime``: the session's last price, open/high/low, cumulative volume and turnover, the
  signed change against the previous close, the publisher's own stamp and its stated delay.
- ``asking``: the ladder. ``buyInfos`` is the bid side, best (highest) bid first. ``sellInfo``
  is the ask side listed from the highest ask down to the best (lowest) ask, so it is reversed
  here to be best-first. ``lastClosePrice`` is the previous close as a number.

Units. Prices are KRW whole won (``price_scale`` 0). Volume is shares, as
``accumulatedTradingVolumeRaw`` states it. Evidence in ``realtime_005930.json``: the regular
volume 19,001,502 and the after-market
volume 10,659,423 add up to the integrated volume 29,660,925, and the regular turnover
3,977,310,000,000 KRW over 19,001,502 shares is 209,315.6 KRW a share, inside the day's range of
206,000 to 214,000; a volume in lots would put the average far outside that range. Turnover is
KRW (``accumulatedTradingValueRaw``). Ladder counts are shares: in ``asking_005930.json`` the
best bid of 36,768 shares at 206,500 and the best ask of 1,381 shares at 207,000 sit in the same
magnitude as the cumulative volume, and the ladder is quoted in the same per-share unit as the
realtime answer.

Ruling: ``prev_close`` is the asking answer's ``lastClosePrice`` when it is present. Otherwise it
is the last price minus the signed change against the previous close, and only when both parse;
a changed or unparseable change field leaves it ``None``. Cost if wrong: a consumer would read a
previous close the publisher did not state.

Ruling: the realtime answer's ``localTradedAt`` is the publisher's last update of the row. Its
``closePriceRaw`` is the regular session's last price, which is still the figure at 17:15 KST
(``overMarketPriceInfo.overPrice`` carries the after-market print). The stamp is therefore not
the time of ``last``; it is kept as the publisher's stamp and not reinterpreted.

Ruling: a listing the answer does not contain is absent. A 2xx body that is not the expected
document raises :class:`LivePayloadError`. An asking answer that fails for any reason other than a
wall leaves the snapshot without a book, and the realtime figures are still returned.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import LivePayloadError, LiveSource, decode_json, to_float
from crocodile.equity.universal.live.model import (
    LiveLevel,
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = [
    "ASKING_URL",
    "REALTIME_URL",
    "SOURCE",
    "NaverLive",
    "parse_asking",
    "parse_realtime",
]

SOURCE: Final = "naver"
REALTIME_URL: Final = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
ASKING_URL: Final = "https://m.stock.naver.com/api/stock/{code}/askingPrice"
_CURRENCY: Final = "KRW"
_NS_PER_SECOND: Final = 1_000_000_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

Book = tuple[tuple[LiveLevel, ...], tuple[LiveLevel, ...], float | None]


def _number(row: Mapping[str, Any], raw: str, plain: str) -> float | None:
    """The ``*Raw`` field when the publisher states it, else the comma-formatted one."""
    if raw in row:
        return to_float(row[raw])
    return to_float(row.get(plain))


def _stamp_ns(value: Any) -> int | None:
    """An ISO-8601 stamp with an explicit offset, as UTC epoch nanoseconds, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _NS_PER_SECOND + delta.microseconds * 1_000


def _delay_s(row: Mapping[str, Any]) -> int | None:
    exchange = row.get("stockExchangeType")
    minutes = to_float(exchange.get("delayTime")) if isinstance(exchange, dict) else None
    if minutes is None or minutes < 0:
        return None
    seconds = minutes * 60
    return int(seconds) if math.isfinite(seconds) else None


def _prev_close(last: float | None, change: float | None) -> float | None:
    """The previous close implied by the signed change, or ``None`` when it is not finite."""
    if last is None or change is None:
        return None
    previous = last - change
    return previous if math.isfinite(previous) else None


def _currency(row: Mapping[str, Any]) -> str:
    kind = row.get("currencyType")
    code = kind.get("code") if isinstance(kind, dict) else None
    return code if isinstance(code, str) and code != "" else _CURRENCY


def parse_realtime(body: bytes, listing: LiveListing, *, captured_ns: int) -> LiveSnapshot | None:
    """Read one realtime answer into the snapshot of ``listing``, or ``None`` if it is absent.

    The snapshot carries no book: the ladder comes from :func:`parse_asking`.
    """
    document = decode_json(SOURCE, body)
    rows = document.get("datas") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise LivePayloadError(SOURCE, "no datas in the realtime answer")
    row: dict[str, Any] | None = next(
        (
            item
            for item in rows
            if isinstance(item, dict) and item.get("itemCode") == listing.local_symbol
        ),
        None,
    )
    if row is None:
        return None
    last = _number(row, "closePriceRaw", "closePrice")
    change = _number(row, "compareToPreviousClosePriceRaw", "compareToPreviousClosePrice")
    name = row.get("stockName")
    return LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=str(row["itemCode"]),
        captured_ns=captured_ns,
        source_ts_ns=_stamp_ns(row.get("localTradedAt")),
        last=last,
        prev_close=_prev_close(last, change),
        open=_number(row, "openPriceRaw", "openPrice"),
        high=_number(row, "highPriceRaw", "highPrice"),
        low=_number(row, "lowPriceRaw", "lowPrice"),
        volume=_number(row, "accumulatedTradingVolumeRaw", "accumulatedTradingVolume"),
        turnover=_number(row, "accumulatedTradingValueRaw", "accumulatedTradingValue"),
        currency=_currency(row),
        price_scale=0,
        delay_s=_delay_s(row),
        name=name if isinstance(name, str) else None,
    )


def _levels(items: list[Any]) -> list[tuple[float | None, float | None]]:
    return [
        (to_float(item.get("price")), to_float(item.get("count")))
        for item in items
        if isinstance(item, dict)
    ]


def parse_asking(body: bytes) -> Book:
    """Read one asking answer into ``(bids, asks, prev_close)``, both sides best-first."""
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "asking answer is not an object")
    sells, buys = document.get("sellInfo"), document.get("buyInfos")
    if not isinstance(sells, list) or not isinstance(buys, list):
        raise LivePayloadError(SOURCE, "no sellInfo/buyInfos in the asking answer")
    bids = clean_levels(_levels(buys), descending=True)
    asks = clean_levels(_levels(sells)[::-1], descending=False)
    return bids, asks, to_float(document.get("lastClosePrice"))


class NaverLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    attribution: ClassVar[str | None] = None
    official: ClassVar[bool] = False
    supported_mics: ClassVar[frozenset[str]] = frozenset({"XKRX"})
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 5
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = False
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 7.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            if not self.serves(listing):
                continue
            code = listing.local_symbol
            if not code.isascii() or not code.isalnum():
                continue
            realtime = await self._get(REALTIME_URL.format(code=code))
            snapshot = parse_realtime(realtime.body, listing, captured_ns=captured_ns)
            if snapshot is None:
                continue
            try:
                asking = await self._get(ASKING_URL.format(code=code))
                bids, asks, asking_prev = parse_asking(asking.body)
            except LivePayloadError:
                bids, asks, asking_prev = (), (), None
            prev_close = asking_prev if asking_prev is not None else snapshot.prev_close
            merged = replace(snapshot, bids=bids, asks=asks, prev_close=prev_close)
            if not merged.is_empty:
                out.append(merged)
        return out
