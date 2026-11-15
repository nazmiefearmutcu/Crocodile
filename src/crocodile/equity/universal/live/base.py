"""The live-source contract and the parsing helpers every live source shares.

A live source answers one question, for the listings of the markets it declares: what is the
publisher showing for this listing now. It makes plain keyless requests through the one socket
path of the universal sources (:mod:`crocodile.equity.universal.http`): a generic User-Agent, no
cookies, no Referer or Origin, no redirects followed. A consent page, a bot challenge, a 401/403
or a geo block is a wall, and a wall is recorded and never worked around: the fetcher raises
:class:`~crocodile.equity.universal.base.SourceUnservableError` and the feed moves to the next
source of the market's chain.

Ruling: a live source declares the tier it can reach (``max_tier``) and the publisher's stated
delay, and both are evidence, not marketing: each is pinned by a fixture of a real response in
``tests/equity/universal/fixtures/live``. The tier a poll actually delivered is measured on the
snapshot (:attr:`LiveSnapshot.tier`).

Ruling: a source never invents a listing's price scale or currency. It states them only when the
publisher states them or when the market has exactly one (``currency_of``).

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from types import MappingProxyType
from typing import Any, ClassVar, Final
from zoneinfo import ZoneInfo

from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.http import (
    AiohttpFetcher,
    Fetcher,
    HttpRequest,
    HttpResponse,
    raise_if_unservable,
)
from crocodile.equity.universal.live.model import LiveListing, LiveSnapshot, LiveTier

__all__ = [
    "LivePayloadError",
    "LiveSource",
    "currency_of",
    "decode_json",
    "local_datetime_ns",
    "millis_ns",
    "seconds_ns",
    "timezone_of",
    "to_float",
]

_NS_PER_SECOND: Final = 1_000_000_000
_NS_PER_MILLI: Final = 1_000_000
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class LivePayloadError(ValueError):
    """A 2xx answer that is not the document the source's parser expects.

    Distinct from a wall: the request was served, and what came back cannot be read as this
    source's payload (a changed schema, an error envelope, an empty body). The feed treats it
    as "this source has nothing for this listing now" and tries the next one.
    """

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


def to_float(value: Any) -> float | None:
    """A published number as a float, or ``None`` for anything that is not one.

    Accepts ints, floats and the string spellings publishers use: thousands separators
    (``"208,000"``), surrounding whitespace, and the placeholders for "nothing here" (``""``,
    ``"-"``, ``"--"``, ``"N/A"``, ``"null"``). A non-finite result is ``None``: ``NaN`` is not a
    price. ``bool`` is refused, because ``True`` is not the number one.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            number = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if text in {"", "-", "--", "N/A", "n/a", "null", "None"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def decode_json(source: str, body: bytes) -> Any:
    """Parse a UTF-8 JSON body, or raise :class:`LivePayloadError`."""
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LivePayloadError(source, f"body is not UTF-8 JSON: {exc}") from exc


def seconds_ns(value: Any) -> int | None:
    """Epoch seconds as epoch nanoseconds, or ``None``."""
    number = to_float(value)
    return None if number is None or number <= 0 else int(number * _NS_PER_SECOND)


def millis_ns(value: Any) -> int | None:
    """Epoch milliseconds as epoch nanoseconds, or ``None``."""
    number = to_float(value)
    return None if number is None or number <= 0 else int(number) * _NS_PER_MILLI


def local_datetime_ns(day: date, clock: time, tz: ZoneInfo) -> int:
    """An exchange-local wall-clock instant as UTC epoch nanoseconds."""
    moment = datetime.combine(day, clock, tzinfo=tz)
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _NS_PER_SECOND + delta.microseconds * 1_000


def timezone_of(mic: str) -> ZoneInfo:
    """The civil time zone of an allow-listed operating MIC."""
    return ExchangeRegistry().get(mic).tz


def currency_of(mic: str) -> str:
    """The primary trading currency of an allow-listed operating MIC."""
    return ExchangeRegistry().get(mic).currency


class LiveSource(ABC):
    """One publisher's live view of the listings of the markets it declares."""

    name: ClassVar[str]
    """Registered source name; also the ``source`` of every record built from its snapshots."""

    usage_class: ClassVar[UsageClass] = UsageClass.INTERNAL
    attribution: ClassVar[str | None] = None
    official: ClassVar[bool] = False
    """True when the publisher is the exchange (or its operator) itself."""

    supported_mics: ClassVar[frozenset[str]]
    max_tier: ClassVar[LiveTier]
    """The deepest tier a fixture of this source's real answer shows."""

    depth_levels: ClassVar[int] = 0
    """Price levels per side the publisher exposes at ``max_tier`` (0 for ``LAST``)."""

    tier_overrides: ClassVar[Mapping[str, tuple[LiveTier, int]]] = MappingProxyType({})
    """Markets where the publisher shows less than ``max_tier``: MIC to ``(tier, levels)``.

    One publisher can serve several markets at different depths. The chain order and the
    coverage table read :meth:`tier_for`, so a market is never listed deeper than a fixture of
    that market shows.
    """

    delay_s: ClassVar[int | None] = None
    """The publisher's stated delay in seconds; ``None`` when it states none."""

    has_tape: ClassVar[bool] = False
    """True when a snapshot can carry the publisher's own prints (or its last print's size)."""

    needs_isin: ClassVar[bool] = False
    batch_size: ClassVar[int] = 1
    """Listings one request can carry."""

    min_interval_s: ClassVar[float] = 5.0
    """The fastest cadence this source is polled at, per request."""

    enumerates: ClassVar[bool] = False
    """True when :meth:`board` can list a whole market without being told its symbols."""

    def __init__(self, fetch: Fetcher | None = None) -> None:
        self._owned: AiohttpFetcher | None = None
        if fetch is None:
            self._owned = AiohttpFetcher()
            fetch = self._owned
        self._fetch: Fetcher = fetch

    async def close(self) -> None:
        """Close the HTTP session this source opened. A caller-supplied fetcher is left alone."""
        if self._owned is not None:
            await self._owned.close()

    @classmethod
    def tier_for(cls, mic: str) -> LiveTier:
        """The deepest tier this source declares for one market."""
        override = cls.tier_overrides.get(mic)
        return override[0] if override is not None else cls.max_tier

    @classmethod
    def depth_for(cls, mic: str) -> int:
        """Price levels per side this source declares for one market."""
        override = cls.tier_overrides.get(mic)
        return override[1] if override is not None else cls.depth_levels

    def serves(self, listing: LiveListing) -> bool:
        if listing.mic not in self.supported_mics:
            return False
        return not self.needs_isin or listing.isin is not None

    @abstractmethod
    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        """One snapshot per listing the publisher answered for, in any order.

        A listing the publisher does not know is simply absent from the result. Walls raise
        :class:`~crocodile.equity.universal.base.SourceUnservableError`; an unreadable 2xx body
        raises :class:`LivePayloadError`.
        """

    async def board(
        self, mic: str, *, captured_ns: int, limit: int | None = None
    ) -> list[LiveSnapshot]:
        """Every listing of ``mic`` the publisher shows, as snapshots (``enumerates`` sources).

        ``limit`` bounds the answer for a source that ranks its board; a source that returns
        the board whole ignores it. The default refuses: most sources answer only for the
        listings they are asked about.
        """
        raise NotImplementedError(f"{self.name} cannot enumerate a market")

    async def _get(
        self,
        url: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
        html_ok: bool = False,
    ) -> HttpResponse:
        response = await self._fetch(
            HttpRequest(source=self.name, method="GET", url=url, params=params)
        )
        self._check(response, html_ok=html_ok)
        return response

    async def _post(self, url: str, body: dict[str, Any]) -> HttpResponse:
        response = await self._fetch(
            HttpRequest(source=self.name, method="POST", url=url, json_body=body)
        )
        self._check(response, html_ok=False)
        return response

    def _check(self, response: HttpResponse, *, html_ok: bool) -> None:
        if not html_ok:
            raise_if_unservable(response, source=self.name, expect="json")
        elif response.status in (401, 403, 451) or 300 <= response.status <= 399:
            raise_if_unservable(response, source=self.name, expect="text")
        if not 200 <= response.status <= 299:
            raise LivePayloadError(self.name, f"HTTP {response.status}")
        if not response.body:
            raise LivePayloadError(self.name, "empty body")
