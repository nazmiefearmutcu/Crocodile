"""Tencent's quote endpoint (``qt.gtimg.cn``): one GET for mainland China and Hong Kong listings.

An aggregator, not an exchange. One GET to ``QUOTE_URL`` with ``q=<comma-joined keys>`` returns
one statement per listing, ``v_<key>="f0~f1~f2~..."``, where the key is ``sh``/``sz``/``bj`` plus
the code for Shanghai, Shenzhen and Beijing (``XSHG``, ``XSHE``, ``BJSE``), and ``hk`` plus the
code zero-padded to five digits for Hong Kong (``XHKG``, local ``700`` is ``hk00700``). The
response is declared ``text/html`` but is plain text, so it is read with ``html_ok``.

Mainland rows (0-based field index): 1 name, 3 last, 4 previous close, 5 open, 6 cumulative
volume, 9/10 .. 17/18 the five best bids (price, size), 19/20 .. 27/28 the five best asks,
30 the stamp ``YYYYMMDDHHMMSS`` (exchange local), 33 high, 34 low, 37 turnover.

Hong Kong rows: only 1 name, 3 last, 4 previous close, 5 open, 6 volume, 30 the stamp
``YYYY/MM/DD HH:MM:SS`` (exchange local), 33 high and 34 low are mapped. The ladder fields of
this feed are zeros (fields 9 and 19 repeat the last price with size 0, which is not a book),
so Hong Kong snapshots carry no levels and tier ``LAST``. Sizes for Hong Kong are not published
here.

Units, with the fixture as evidence (``tests/equity/universal/fixtures/live/tencent``):

- Volume (field 6) and every ladder size are board lots of 100 shares; they are converted to
  shares (x100). For ``sz000001`` the field-35 triple ``11.79/1082721/1273354841`` gives an
  average price of 1273354841 / (1082721 x 100) = 11.76, against a last price of 11.79.
- Turnover (field 37) is in units of 10,000 CNY; it is converted to CNY (x10000). For
  ``sh600519`` field 37 is 453681, i.e. 4,536,810,000 CNY, which agrees with the exact amount
  4536814518 in field 35 to within the 10,000 CNY of rounding.
- Hong Kong volume (field 6) is already in shares and is not converted.
- Prices are in the exchange's quoted currency unit (CNY or HKD), ``price_scale`` 0.

Ruling: the publisher states no delay, so ``delay_s`` is ``None`` on every snapshot. Mainland
rows carry no size for their last print, so ``last_size`` is ``None`` and no trade is built.
Ruling: a size of zero is not a size; ``clean_levels`` turns it into ``None``, and a ladder
that is not monotone is refused whole. A row that fails to parse yields ``None`` for that field,
never a substitute from another field.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from types import MappingProxyType
from typing import ClassVar, Final
from zoneinfo import ZoneInfo

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    currency_of,
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

__all__ = ["LOT_SHARES", "QUOTE_URL", "SOURCE", "TencentLive", "parse_quotes", "quote_key"]

SOURCE: Final = "tencent"
QUOTE_URL: Final = "https://qt.gtimg.cn/"
LOT_SHARES: Final = 100.0
_TEN_THOUSAND: Final = 10_000.0
_PREFIX: Final[dict[str, str]] = {"XSHG": "sh", "XSHE": "sz", "BJSE": "bj", "XHKG": "hk"}
_STATEMENT: Final = re.compile(r'v_([A-Za-z0-9_]+)="(.*)"', re.DOTALL)
_MIN_FIELDS: Final = 35
_BID_SLOTS: Final = (9, 11, 13, 15, 17)
_ASK_SLOTS: Final = (19, 21, 23, 25, 27)


def quote_key(listing: LiveListing) -> str:
    """The key of one listing in the request and in the answer, e.g. ``sh600519``."""
    prefix = _PREFIX[listing.mic]
    code = listing.local_symbol.zfill(5) if listing.mic == "XHKG" else listing.local_symbol
    return f"{prefix}{code}"


def _statements(body: bytes) -> dict[str, list[str]]:
    text = body.decode("gbk", errors="replace")
    found: dict[str, list[str]] = {}
    for piece in text.split(";"):
        match = _STATEMENT.fullmatch(piece.strip())
        if match is not None:
            found[match.group(1)] = match.group(2).split("~")
    if not found:
        raise LivePayloadError(SOURCE, "no v_ statement in the answer")
    return found


def _number(fields: Sequence[str], index: int) -> float | None:
    return to_float(fields[index]) if index < len(fields) else None


def _price(fields: Sequence[str], index: int) -> float | None:
    """A quoted price. A zero is the publisher's "nothing here", not a price of zero."""
    value = _number(fields, index)
    return value if value is not None and value > 0.0 else None


def _shares(fields: Sequence[str], index: int) -> float | None:
    lots = _number(fields, index)
    return None if lots is None else lots * LOT_SHARES


def _turnover_cny(fields: Sequence[str]) -> float | None:
    tens_of_thousands = _number(fields, 37)
    return None if tens_of_thousands is None else tens_of_thousands * _TEN_THOUSAND


def _level(fields: Sequence[str], index: int) -> tuple[float | None, float | None]:
    """A ladder slot: the price at ``index`` and the size in shares at ``index + 1``."""
    return _number(fields, index), _shares(fields, index + 1)


def _mainland_stamp(text: str, tz: ZoneInfo) -> int | None:
    if len(text) != 14 or not text.isascii() or not text.isdigit():
        return None
    try:
        day = date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        clock = time(int(text[8:10]), int(text[10:12]), int(text[12:14]))
    except ValueError:
        return None
    return local_datetime_ns(day, clock, tz)


def _hong_kong_stamp(text: str, tz: ZoneInfo) -> int | None:
    try:
        moment = datetime.strptime(text, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return local_datetime_ns(moment.date(), moment.time(), tz)


def _mainland(listing: LiveListing, key: str, fields: list[str], captured_ns: int) -> LiveSnapshot:
    tz = timezone_of(listing.mic)
    return LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=key,
        captured_ns=captured_ns,
        source_ts_ns=_mainland_stamp(fields[30], tz),
        last=_price(fields, 3),
        prev_close=_price(fields, 4),
        open=_price(fields, 5),
        high=_price(fields, 33),
        low=_price(fields, 34),
        volume=_shares(fields, 6),
        turnover=_turnover_cny(fields),
        bids=clean_levels([_level(fields, index) for index in _BID_SLOTS], descending=True),
        asks=clean_levels([_level(fields, index) for index in _ASK_SLOTS], descending=False),
        currency=currency_of(listing.mic),
        price_scale=0,
        name=fields[1] or None,
    )


def _hong_kong(listing: LiveListing, key: str, fields: list[str], captured_ns: int) -> LiveSnapshot:
    return LiveSnapshot(
        source=SOURCE,
        listing=listing,
        symbol_raw=key,
        captured_ns=captured_ns,
        source_ts_ns=_hong_kong_stamp(fields[30], timezone_of(listing.mic)),
        last=_price(fields, 3),
        prev_close=_price(fields, 4),
        open=_price(fields, 5),
        high=_price(fields, 33),
        low=_price(fields, 34),
        volume=_number(fields, 6),
        currency=currency_of(listing.mic),
        price_scale=0,
        name=fields[1] or None,
    )


def parse_quotes(
    body: bytes, listings: Sequence[LiveListing], *, captured_ns: int
) -> list[LiveSnapshot]:
    """Read one quote answer into snapshots for the requested listings.

    A statement whose key matches no requested listing is ignored. A row with fewer than 35
    fields is skipped. An answer with no ``v_`` statement at all is a :class:`LivePayloadError`.
    """
    statements = _statements(body)
    wanted = {quote_key(listing): listing for listing in listings if listing.mic in _PREFIX}
    out: list[LiveSnapshot] = []
    for key, fields in statements.items():
        listing = wanted.get(key)
        if listing is None or len(fields) < _MIN_FIELDS:
            continue
        if listing.mic == "XHKG":
            snapshot = _hong_kong(listing, key, fields, captured_ns)
        else:
            snapshot = _mainland(listing, key, fields, captured_ns)
        if not snapshot.is_empty:
            out.append(snapshot)
    return out


class TencentLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    attribution: ClassVar[str | None] = None
    official: ClassVar[bool] = False
    supported_mics: ClassVar[frozenset[str]] = frozenset(_PREFIX)
    max_tier: ClassVar[LiveTier] = LiveTier.L2
    depth_levels: ClassVar[int] = 5
    tier_overrides: ClassVar[Mapping[str, tuple[LiveTier, int]]] = MappingProxyType(
        {"XHKG": (LiveTier.LAST, 0)}
    )
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = False
    batch_size: ClassVar[int] = 50
    min_interval_s: ClassVar[float] = 5.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        served = [listing for listing in listings if self.serves(listing)]
        if not served:
            return []
        keys = ",".join(quote_key(listing) for listing in served)
        response = await self._get(QUOTE_URL, params=(("q", keys),), html_ok=True)
        return parse_quotes(response.body, served, captured_ns=captured_ns)
