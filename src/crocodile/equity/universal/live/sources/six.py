"""SIX Swiss Exchange's own public quote service (``six-group.com/fqs``): the whole equity market.

One GET to ``/fqs/ref.json`` answers the equity segment (``PortalSegment=EQ``) as a positional
table: ``colNames`` names the columns and ``rowData`` holds one list per security. Columns are
found by name, never by position. A security is matched to a listing by ``ValorSymbol``, which
is the local symbol. The same symbol can appear on more than one row when a security trades in
more than one currency (the fixture has ``DDD`` in CHF and in USD). Such a symbol is resolved to
the row whose ``TradingBaseCurrency`` is the operating MIC's currency; when that does not leave
exactly one row, the listing is absent rather than guessed.

Fields. ``ClosingPrice`` is the latest price of the session, not a closing price, so it is stored
as ``last``. ``LatestTradeVolume`` is the size of that last print and is stated only when it is
positive. ``BidPrice``/``BidVolume`` and ``AskPrice``/``AskVolume`` form the top of book; this
service publishes no deeper ladder, so the tier is at most ``L1``. ``PreviousClosingPrice``,
``OpeningPrice``, ``DailyHighPrice``, ``DailyLowPrice`` and ``TotalVolume`` map to the snapshot
fields of the same meaning. A cell that is ``""`` or not a number is ``None``; a numeric ``0``
volume is a real zero, and a ``0`` size on a level is an empty slot.

Units. Prices are stored as published, in the row's ``TradingBaseCurrency``, with no scale
applied. The fixture shows CHF prices on CHF rows (ABB LTD N at 70.38, 1&1 AG at 22.25) and no
price scale is stated, so ``price_scale`` is ``None``. Volumes and sizes are numbers of shares
with no conversion. The fixture supports this: ABB LTD N shows a ``TotalVolume`` of 206118 at a
price of about 70.38 CHF, a turnover of roughly 14.5 million CHF, which fits a share count and
not a value count.

Time. ``LatestTradeDate`` (``YYYYMMDD``) and ``LatestTradeTime`` are read as Europe/Zurich wall
clock when both are positive and form a valid date and time. The fixture shows the time as
``HHMMSS.mmm`` with no zero padding (``92020.829`` is 09:20:20.829), so the whole part is split as
``HHMMSS`` and the fraction is kept to the microsecond. A time of ``0`` means none. The
publisher states a delay of ``delayMinutes`` (15 in the fixture); it is carried as ``delay_s`` and
never used to shift a timestamp. Open point: the fixture's own ``delayedDateTime`` (09:05:31
Zurich) is earlier than the trade stamps it carries (09:20:20 and 09:20:20.795 Zurich). The fixture
therefore does not confirm the Zurich reading of ``LatestTradeTime``. The stamps are stored as
read and the disagreement is left visible here rather than resolved by a guess.

Page size. The fixture's ``requestURL`` and ``pageSize`` show that the captured request asked for
``pagesize=1000``, so the request uses 1000. ``totalRows`` (813) is below that page size, so one
page is meant to hold the whole segment. The fixture itself holds only 6 of those 813 rows, so it
does not show a full page. When ``totalRows`` is larger than the number of rows in the answer, the
answer was truncated: the parser still returns every listing it matched, and a listing whose row is
past the cut is absent.

Staleness. ``ClosingPrice`` is stored as ``last`` whatever its trade date. In the fixture, 1&1 AG
(``1U1``) and 3M (``MMM``) carry trade dates from 2019 and 2026-07-29 with no trade time. Their
``last`` is therefore a price from an earlier session, and the snapshot has no ``source_ts_ns`` to
say so. A consumer must not read ``last`` of such a row as a current print.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, time
from typing import Any, ClassVar, Final
from zoneinfo import ZoneInfo

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    currency_of,
    decode_json,
    local_datetime_ns,
    timezone_of,
    to_float,
)
from crocodile.equity.universal.live.model import (
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = ["DELAY_S", "PARAMS", "REF_URL", "SOURCE", "SixLive", "parse_quotes"]

SOURCE: Final = "six"
REF_URL: Final = "https://www.six-group.com/fqs/ref.json"
DELAY_S: Final = 900
SELECT: Final[tuple[str, ...]] = (
    "ShortName",
    "ISIN",
    "ValorSymbol",
    "ValorId",
    "ClosingPrice",
    "BidPrice",
    "AskPrice",
    "BidVolume",
    "AskVolume",
    "TotalVolume",
    "LatestTradeTime",
    "LatestTradeVolume",
    "LatestTradeDate",
    "DailyHighPrice",
    "DailyLowPrice",
    "OpeningPrice",
    "PreviousClosingPrice",
    "TradingBaseCurrency",
)
PARAMS: Final[tuple[tuple[str, str], ...]] = (
    ("select", ",".join(SELECT)),
    ("where", "PortalSegment=EQ"),
    ("orderby", "ShortName"),
    ("page", "1"),
    ("pagesize", "1000"),
)
_MIC: Final = "XSWX"
_REQUIRED: Final[tuple[str, ...]] = ("ValorSymbol", "ClosingPrice", "BidPrice", "AskPrice")


def _cell(row: list[Any], position: int) -> Any:
    return row[position] if position < len(row) else None


def _whole_positive(value: float | None) -> int | None:
    if value is None or value <= 0 or value != math.floor(value):
        return None
    return int(value)


def _source_ts(day: Any, clock: Any, tz: ZoneInfo) -> int | None:
    """``LatestTradeDate`` and ``LatestTradeTime`` as UTC epoch nanoseconds, or ``None``."""
    stamp_day = _whole_positive(to_float(day))
    clock_value = to_float(clock)
    if stamp_day is None or clock_value is None or clock_value <= 0:
        return None
    year, rest = divmod(stamp_day, 10_000)
    month, day_of_month = divmod(rest, 100)
    whole = math.floor(clock_value)
    hour, rest_clock = divmod(whole, 10_000)
    minute, second = divmod(rest_clock, 100)
    micro = min(round((clock_value - whole) * 1_000_000), 999_999)
    try:
        trade_day = date(year, month, day_of_month)
        trade_clock = time(hour, minute, second, micro)
    except ValueError:
        return None
    return local_datetime_ns(trade_day, trade_clock, tz)


def _delay_s(document: Mapping[str, Any]) -> int:
    minutes = to_float(document.get("delayMinutes"))
    if minutes is None or minutes < 0:
        return DELAY_S
    return int(minutes * 60)


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip() == "":
        return None
    return value.strip()


def _in_currency(rows: list[list[Any]], index: Mapping[str, int], currency: str) -> list[list[Any]]:
    """The rows of one symbol in the MIC's currency; none when the currency column is absent."""
    if "TradingBaseCurrency" not in index:
        return []
    return [row for row in rows if _text(_cell(row, index["TradingBaseCurrency"])) == currency]


def _snapshot(
    row: list[Any],
    index: Mapping[str, int],
    listing: LiveListing,
    *,
    captured_ns: int,
    delay_s: int,
) -> LiveSnapshot:
    def cell(name: str) -> Any:
        return _cell(row, index[name]) if name in index else None

    last = to_float(cell("ClosingPrice"))
    last_volume = to_float(cell("LatestTradeVolume"))
    last_size = last_volume if last_volume is not None and last_volume > 0 else None
    bid_price = to_float(cell("BidPrice"))
    ask_price = to_float(cell("AskPrice"))
    source_ts = _source_ts(cell("LatestTradeDate"), cell("LatestTradeTime"), timezone_of(_MIC))
    return LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=_text(cell("ValorSymbol")) or listing.local_symbol,
        captured_ns=captured_ns,
        source_ts_ns=source_ts,
        last=last,
        last_size=last_size if last is not None else None,
        prev_close=to_float(cell("PreviousClosingPrice")),
        open=to_float(cell("OpeningPrice")),
        high=to_float(cell("DailyHighPrice")),
        low=to_float(cell("DailyLowPrice")),
        volume=to_float(cell("TotalVolume")),
        bids=clean_levels([(bid_price, to_float(cell("BidVolume")))], descending=True),
        asks=clean_levels([(ask_price, to_float(cell("AskVolume")))], descending=False),
        currency=_text(cell("TradingBaseCurrency")),
        delay_s=delay_s,
        name=_text(cell("ShortName")),
    )


def parse_quotes(
    body: bytes, listings: Sequence[LiveListing], *, captured_ns: int
) -> list[LiveSnapshot]:
    """Read one ``ref.json`` answer into snapshots for the requested listings."""
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "the answer is not a JSON object")
    names = document.get("colNames")
    rows = document.get("rowData")
    if not isinstance(names, list) or not isinstance(rows, list):
        raise LivePayloadError(SOURCE, "no colNames or rowData in the answer")
    index = {name: position for position, name in enumerate(names) if isinstance(name, str)}
    missing = [name for name in _REQUIRED if name not in index]
    if missing:
        raise LivePayloadError(SOURCE, f"no column {', '.join(missing)} in the answer")
    served = [listing for listing in dict.fromkeys(listings) if listing.mic == _MIC]
    wanted = {listing.local_symbol for listing in served}
    matches: dict[str, list[list[Any]]] = {}
    for row in rows:
        if not isinstance(row, list):
            continue
        symbol = _text(_cell(row, index["ValorSymbol"]))
        if symbol is not None and symbol in wanted:
            matches.setdefault(symbol, []).append(row)
    delay_s = _delay_s(document)
    out: list[LiveSnapshot] = []
    for listing in served:
        candidates = matches.get(listing.local_symbol, [])
        if len(candidates) > 1:
            candidates = _in_currency(candidates, index, currency_of(listing.mic))
        if len(candidates) != 1:
            continue
        snapshot = _snapshot(
            candidates[0], index, listing, captured_ns=captured_ns, delay_s=delay_s
        )
        if not snapshot.is_empty:
            out.append(snapshot)
    return out


class SixLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: SIX Swiss Exchange (six-group.com)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({_MIC})
    max_tier: ClassVar[LiveTier] = LiveTier.L1
    depth_levels: ClassVar[int] = 1
    delay_s: ClassVar[int | None] = DELAY_S
    has_tape: ClassVar[bool] = False
    batch_size: ClassVar[int] = 2000
    min_interval_s: ClassVar[float] = 60.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        served = [listing for listing in listings if self.serves(listing)]
        if not served:
            return []
        response = await self._get(REF_URL, params=PARAMS)
        return parse_quotes(response.body, served, captured_ns=captured_ns)
