"""Singapore Exchange's own securities API (``api.sgx.com``): the best bid and ask of one listing.

One GET per listing to ``/securities/v1.1/stocks/code/<local symbol>`` with one ``params`` list of
the fields wanted. The answer carries the last price (``lt``), the session's open/high/low
(``o``/``h``/``l``), the previous close (``pv``), the cumulative volume (``vl``), the turnover in
the trading currency (``v``), the best bid and ask (``b``/``s``, quoted as strings) with their
sizes (``bv``/``sv``), and the publisher's stamp (``trading_time``). It publishes no tape and no
size for its last print, so a snapshot carries no trade record.

Units. ``vl``, ``bv`` and ``sv`` are in thousands of shares. The fixture shows it for the volume:
``v / (vl * 1000)`` is 71.33 SGD, against a last price of 71.25 and a session range of 71.00 to
71.60, so ``vl`` is thousands of shares. The sizes ``bv`` and ``sv`` are converted with the same
factor, which is the publisher's stated unit for them, not a separate fixture check. Sizes and
volume are converted to shares here (``round(x * 1000, 3)``). Turnover ``v`` is stated in SGD and
kept as published.

Ruling: a price of ``0``, a negative price or an unparseable one is no price. A ``lt`` of ``0`` or
``None`` means no trade yet: the last price is ``None``, never the previous close or the bid.
Cost if wrong: a consumer would read a print that did not happen.

Ruling: ``trading_time`` (``YYYYMMDD_HHMMSS``) is UTC. The fixture's ``20261112_081400`` is 16:14
in Singapore, inside the session. The publisher states no delay, so the snapshot states none.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar, Final
from urllib.parse import quote

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    currency_of,
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

__all__ = ["FIELDS", "LOT_SHARES", "QUOTE_URL", "SOURCE", "SgxLive", "parse_quotes"]

SOURCE: Final = "sgx"
QUOTE_URL: Final = "https://api.sgx.com/securities/v1.1/stocks/code/"
"""The listing's local symbol is appended: ``QUOTE_URL + "D05"``."""
FIELDS: Final = "nc,cn,n,lt,c,p,vl,v,b,bv,s,sv,o,h,l,pv,lf,trading_time,cur,type,ig"
LOT_SHARES: Final = 1000.0
_MIC: Final = "XSES"
_STAMP: Final = "%Y%m%d_%H%M%S"
_NS_PER_SECOND: Final = 1_000_000_000


def _price(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number > 0.0 else None


def _shares(value: Any) -> float | None:
    number = to_float(value)
    return None if number is None or number < 0.0 else round(number * LOT_SHARES, 3)


def _turnover(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number >= 0.0 else None


def _stamp_ns(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.strptime(value, _STAMP)
    except ValueError:
        return None
    return int(moment.replace(tzinfo=UTC).timestamp()) * _NS_PER_SECOND


def _top(price: Any, size: Any, *, descending: bool) -> tuple[LiveLevel, ...]:
    return clean_levels([(_price(price), _shares(size))], descending=descending)


def parse_quotes(
    body: bytes, listings: Sequence[LiveListing], *, captured_ns: int
) -> list[LiveSnapshot]:
    """Read one quote answer into snapshots for the requested listings."""
    document: Any = decode_json(SOURCE, body)
    meta = document.get("meta") if isinstance(document, dict) else None
    if not isinstance(meta, dict) or meta.get("code") != "200":
        raise LivePayloadError(SOURCE, "answer meta.code is not 200")
    data = document.get("data")
    prices = data.get("prices") if isinstance(data, dict) else None
    if not isinstance(prices, list):
        raise LivePayloadError(SOURCE, "no data.prices list in the answer")
    wanted = {listing.local_symbol: listing for listing in listings}
    out: list[LiveSnapshot] = []
    for row in prices:
        if not isinstance(row, dict):
            continue
        code = row.get("nc")
        if not isinstance(code, str) or code not in wanted:
            continue
        name = row.get("n")
        currency = row.get("cur")
        snapshot = LiveSnapshot(
            source=SOURCE,
            listing=wanted[code],
            symbol_raw=code,
            captured_ns=captured_ns,
            source_ts_ns=_stamp_ns(row.get("trading_time")),
            last=_price(row.get("lt")),
            prev_close=_price(row.get("pv")),
            open=_price(row.get("o")),
            high=_price(row.get("h")),
            low=_price(row.get("l")),
            volume=_shares(row.get("vl")),
            turnover=_turnover(row.get("v")),
            bids=_top(row.get("b"), row.get("bv"), descending=True),
            asks=_top(row.get("s"), row.get("sv"), descending=False),
            currency=currency if isinstance(currency, str) else currency_of(_MIC),
            price_scale=0,
            delay_s=None,
            name=name if isinstance(name, str) else None,
        )
        if not snapshot.is_empty:
            out.append(snapshot)
    return out


class SgxLive(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: Singapore Exchange (api.sgx.com)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({_MIC})
    max_tier: ClassVar[LiveTier] = LiveTier.L1
    depth_levels: ClassVar[int] = 1
    delay_s: ClassVar[int | None] = None
    has_tape: ClassVar[bool] = False
    batch_size: ClassVar[int] = 1
    min_interval_s: ClassVar[float] = 10.0

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        out: list[LiveSnapshot] = []
        for listing in listings:
            if not self.serves(listing):
                continue
            response = await self._get(
                QUOTE_URL + quote(listing.local_symbol, safe=""),
                params=(("params", FIELDS),),
            )
            out.extend(parse_quotes(response.body, [listing], captured_ns=captured_ns))
        return out
