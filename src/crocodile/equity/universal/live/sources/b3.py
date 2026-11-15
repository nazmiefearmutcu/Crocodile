"""B3's own public quotation service (``cotacao.b3.com.br``): the delayed last price of one listing.

One GET to ``/mds/api/v1/instrumentQuotation/<local symbol>`` answers one listing (``batch_size``
is 1). The answer states the session's open, low, high and average, the current price
(``curPrc``), the percent change against the previous close (``prcFlcn``), the stamp of the
figures (``Msg.dtTm``, wall clock in Sao Paulo) and the instrument's description (``desc``).

Units. Prices are reais (BRL) per share, not cents: the fixture's PETR4 quotes ``43.55``,
``44.52`` and ``43.3`` with two decimals, and its ``prcFlcn`` of ``-3.1145717`` equals
``(43.55 - 44.95) / 44.95`` in the same unit. ``price_scale`` is 0, as the spec states.

Previous close. ``prcFlcn`` is the percent change against the previous close, so the previous
close is ``curPrc / (1 + prcFlcn / 100)``, rounded to 2 decimals. It is derived only when both
figures parse and the change is above -100 percent (at -100 the derivation divides by zero). The
fixture confirms the derivation: ``43.55 / (1 - 0.031145717)`` is ``44.95``.

Volume. ``ttlQty`` (96974 in the fixture) is not mapped to volume: the payload does not say
whether it counts trades or shares, so ``volume`` stays ``None``.

Delay. ``delay_s`` is 900: B3's public quotation service is delayed fifteen minutes, as the
spec states. The fixture itself does not carry the delay.

No book. The answer carries no bids or asks and no size of its last print, so the tier is
``LAST`` and no record is built from it.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Final
from urllib.parse import quote

from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.live.base import (
    LivePayloadError,
    LiveSource,
    decode_json,
    local_datetime_ns,
    timezone_of,
    to_float,
)
from crocodile.equity.universal.live.model import LiveListing, LiveSnapshot, LiveTier

__all__ = ["QUOTE_URL", "SOURCE", "B3Live", "parse_quote"]

SOURCE: Final = "b3"
QUOTE_URL: Final = "https://cotacao.b3.com.br/mds/api/v1/instrumentQuotation/"
"""The percent-encoded local symbol is appended: ``QUOTE_URL + "PETR4"``."""
_MIC: Final = "BVMF"
_CURRENCY: Final = "BRL"
_DELAY_S: Final = 900
_STAMP_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_NO_CHANGE_BASE: Final = -100.0


def _number(value: Any) -> float | None:
    """A published price or percent. A string with a decimal comma is refused: ``to_float`` would
    read ``"43,55"`` as the thousands-separated ``4355``, a price a hundred times too high."""
    if isinstance(value, str) and "," in value:
        return None
    return to_float(value)


def _price(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0.0 else None


def _stamp_ns(value: Any) -> int | None:
    """The publisher's ``dtTm`` as UTC epoch nanoseconds, read as Sao Paulo wall clock."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.strptime(value, _STAMP_FORMAT)
    except ValueError:
        return None
    return local_datetime_ns(moment.date(), moment.time(), timezone_of(_MIC))


def _prev_close(last: float | None, change: Any) -> float | None:
    """``last / (1 + change / 100)`` to 2 decimals, or ``None`` when either is not stated."""
    percent = _number(change)
    if last is None or percent is None or percent <= _NO_CHANGE_BASE:
        return None
    derived = round(last / (1.0 + percent / 100.0), 2)
    return derived if math.isfinite(derived) and derived > 0.0 else None


def _name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def parse_quote(body: bytes, listing: LiveListing, *, captured_ns: int) -> list[LiveSnapshot]:
    """Read one ``instrumentQuotation`` answer into the snapshot of the requested listing.

    Returns an empty list when the answer carries no row for the listing's local symbol. Raises
    :class:`LivePayloadError` for a status other than ``OK``, a missing or empty ``Trad`` list,
    more than one row for the listing (two conflicting quotes, not one), or a matching row without
    its ``SctyQtn`` figures.
    """
    document = decode_json(SOURCE, body)
    if not isinstance(document, dict):
        raise LivePayloadError(SOURCE, "answer is not a JSON object")
    status = document.get("BizSts")
    code = status.get("cd") if isinstance(status, dict) else None
    if code != "OK":
        raise LivePayloadError(SOURCE, f"status is {code!r}")
    rows = document.get("Trad")
    if not isinstance(rows, list) or not rows:
        raise LivePayloadError(SOURCE, "no Trad list in the answer")
    matches: list[dict[str, Any]] = []
    for row in rows:
        scty = row.get("scty") if isinstance(row, dict) else None
        if isinstance(scty, dict) and scty.get("symb") == listing.local_symbol:
            matches.append(scty)
    if not matches:
        return []
    if len(matches) > 1:
        raise LivePayloadError(SOURCE, f"{len(matches)} rows for {listing.local_symbol}")
    scty = matches[0]
    figures = scty.get("SctyQtn")
    if not isinstance(figures, dict):
        raise LivePayloadError(SOURCE, f"no SctyQtn figures for {listing.local_symbol}")
    message = document.get("Msg")
    stamp = message.get("dtTm") if isinstance(message, dict) else None
    last = _price(figures.get("curPrc"))
    return [
        LiveSnapshot(
            source=SOURCE,
            listing=listing,
            symbol_raw=listing.local_symbol,
            captured_ns=captured_ns,
            source_ts_ns=_stamp_ns(stamp),
            last=last,
            prev_close=_prev_close(last, figures.get("prcFlcn")),
            open=_price(figures.get("opngPric")),
            high=_price(figures.get("maxPric")),
            low=_price(figures.get("minPric")),
            currency=_CURRENCY,
            price_scale=0,
            delay_s=_DELAY_S,
            name=_name(scty.get("desc")),
        )
    ]


class B3Live(LiveSource):
    name: ClassVar[str] = SOURCE
    usage_class: ClassVar[UsageClass] = UsageClass.DISPLAY
    attribution: ClassVar[str | None] = "Source: B3 S.A. (cotacao.b3.com.br)"
    official: ClassVar[bool] = True
    supported_mics: ClassVar[frozenset[str]] = frozenset({_MIC})
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
            response = await self._get(QUOTE_URL + quote(listing.local_symbol, safe=""))
            out.extend(parse_quote(response.body, listing, captured_ns=captured_ns))
        return out
