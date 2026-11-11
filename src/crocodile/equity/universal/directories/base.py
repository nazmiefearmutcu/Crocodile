"""Shared machinery of the official listing directories (spec section 3.10c, task 12).

A directory row is a publisher's own statement that a listing existed on the directory's as-of
date. :func:`snapshot_from_parses` is the only place a directory builds an
:class:`~crocodile.core.schema.records.Instrument`; the four providers hand it parsed rows and
never construct records themselves. :class:`DirectoryProvider` fetches each document once, in
``urls`` order, through the shared budget, and refuses a snapshot when any document fails, so a
half-read directory can never drive ``mark_unseen``.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from abc import abstractmethod
from collections import Counter
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Final

from crocodile.core.markets.errors import InvalidSymbolError, NoGrammarForMarket
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.grammar import (
    GrammarError,
    PrefixGrammar,
    SuffixGrammar,
    load_grammar,
)
from crocodile.core.markets.master import ListingMaster, ListingObservation
from crocodile.core.markets.sanctions import allowed_countries, is_sanctioned
from crocodile.core.markets.symbols import listing_symbol
from crocodile.core.markets.usage import UsageClass
from crocodile.core.ratelimit import budget
from crocodile.core.ratelimit.token_bucket import TokenBucket
from crocodile.core.schema.enums import AssetClass, SecurityType
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import Instrument, Record
from crocodile.equity.providers.base import (
    ProviderError,
    PullProvider,
    TransientProviderError,
)
from crocodile.equity.universal.base import (
    SourceUnservableError,
    UniverseSnapshot,
    UnservableReason,
    apply_universe,
)
from crocodile.equity.universal.http import (
    AiohttpFetcher,
    Fetcher,
    HttpRequest,
    HttpResponse,
    raise_if_unservable,
)

if TYPE_CHECKING:
    from crocodile.core.config import Settings
    from crocodile.core.sink.base import Sink
    from crocodile.equity.reference.registry import InstrumentRegistry

__all__ = [
    "DirectoryFormatError",
    "DirectoryParse",
    "DirectoryProvider",
    "DirectoryRow",
    "bytes_document",
    "directory_records",
    "snapshot_from_parses",
    "text_document",
    "utc_ns",
]

log = logging.getLogger(__name__)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_SECOND: Final = 1_000_000_000
_DEFAULT_BACKOFF_S: Final = 5.0
_YAHOO_LOCAL: Final = r"[A-Za-z0-9][A-Za-z0-9.]*"
"""A local symbol Yahoo's grammar can spell without loss (``BRK.B`` yes, ``ABR$D`` no)."""

UNSERVABLE_TEXT: Final[Mapping[UnservableReason, str]] = MappingProxyType(
    {
        UnservableReason.BOT_CHALLENGE: "a consent, bot or redirect wall was served",
        UnservableReason.FORBIDDEN: "the directory answered 401 or 403 to this client",
        UnservableReason.GEO_BLOCKED: "the directory answered 451 for this location",
        UnservableReason.TIMEOUT: "the directory request timed out on the client side",
        UnservableReason.UNREACHABLE: "the directory host could not be reached or the file is gone",
    }
)


class DirectoryFormatError(ProviderError):
    """A directory document is not in the layout it documents: refused, never misparsed."""


@dataclass(frozen=True, slots=True)
class DirectoryRow:
    symbol_raw: str  # exactly as the file spells it
    local_symbol: str  # stripped; the exchange's own local symbol
    mic: str  # operating MIC
    segment_mic: str | None
    name: str | None
    security_type: SecurityType  # ETF only where the file says so, else UNKNOWN (never inferred)
    isin: str | None = None
    listing_date: str | None = None  # ISO YYYY-MM-DD
    series: str | None = None  # NSE trading series; validated and counted, not persisted


@dataclass(frozen=True, slots=True)
class DirectoryParse:
    source: str
    document: str  # URL the rows came from
    rows: tuple[DirectoryRow, ...]
    as_of_ns: int | None  # the file's own creation/as-of instant, None if it states none
    skipped: Mapping[str, int]  # whole rows not turned into DirectoryRow, by reason
    dropped_fields: Mapping[str, int]  # field values discarded (e.g. "isin"), by field


def utc_ns(moment: datetime) -> int:
    """Epoch nanoseconds of an aware datetime, exact (no float round-trip)."""
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _NS_PER_SECOND + delta.microseconds * 1_000


def text_document(documents: Mapping[str, bytes], url: str) -> str:
    """The UTF-8 text of one fetched document (a leading BOM is dropped); refused if absent."""
    body = documents.get(url)
    if body is None:
        raise DirectoryFormatError(f"no document was read for {url}")
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DirectoryFormatError(f"{url} is not UTF-8 text") from exc


def bytes_document(documents: Mapping[str, bytes], url: str) -> bytes:
    """The raw bytes of one fetched document; refused if absent."""
    body = documents.get(url)
    if body is None:
        raise DirectoryFormatError(f"no document was read for {url}")
    return body


@functools.cache
def _exchanges() -> ExchangeRegistry:
    return ExchangeRegistry()


@functools.cache
def _yahoo_grammar() -> SuffixGrammar | PrefixGrammar:
    return load_grammar("yahoo_chart")


def _yahoo_alias(local: str, mic: str, segment_mic: str | None) -> tuple[tuple[str, str], ...]:
    """The Yahoo spelling of the listing when its grammar covers the MIC and can spell the local
    symbol without loss; no alias rather than a wrong one."""
    yahoo = _yahoo_grammar()
    if mic not in yahoo.supported_mics() or re.fullmatch(_YAHOO_LOCAL, local) is None:
        return ()
    try:
        spelled = yahoo.to_source(local, mic, segment_mic=segment_mic)
    except (NoGrammarForMarket, GrammarError):
        return ()
    return (("yahoo_chart", spelled),)


def snapshot_from_parses(
    source: str,
    parses: Sequence[DirectoryParse],
    *,
    captured_at_ns: int,
    allow: Collection[str] = (),
) -> UniverseSnapshot:
    """Observations for the listing master and one NATIVE Instrument per accepted row.

    A row is refused, with a counted reason, when its MIC is not in the exchange allow-list, when
    it is sanctioned and not opened by ``allow``, when it repeats a (MIC, local) pair already
    accepted, or when its local symbol is not valid for the market. Currency and price scale stay
    ``None``: no directory publishes them, and the exchange's default would flip-flop against the
    per-row unit of the quote sources. The status stays ACTIVE: a directory asserts existence,
    never delisting.
    """
    catalogue = _exchanges()
    skipped: Counter[str] = Counter()
    observations: list[ListingObservation] = []
    records: list[Instrument] = []
    seen: dict[str, set[str]] = {}
    accepted: set[tuple[str, str]] = set()
    tail = provenance_fields("listing_directory")
    for parse in parses:
        for reason, count in parse.skipped.items():
            skipped[reason] += count
        for row in parse.rows:
            if row.mic not in catalogue:
                skipped[f"market_not_listed:{row.mic}"] += 1
                continue
            if is_sanctioned(row.mic, allow=allow):
                skipped[f"sanctioned:{row.mic}"] += 1
                continue
            key = (row.mic, row.local_symbol)
            if key in accepted:
                skipped["duplicate_listing"] += 1
                continue
            try:
                symbol = listing_symbol(row.local_symbol, row.mic)
            except InvalidSymbolError:
                skipped["invalid_local_symbol"] += 1
                continue
            accepted.add(key)
            seen.setdefault(row.mic, set()).add(row.local_symbol)
            observations.append(
                ListingObservation(
                    source=source,
                    mic=row.mic,
                    segment_mic=row.segment_mic,
                    local_symbol=row.local_symbol,
                    symbol_raw=row.symbol_raw,
                    observed_ns=captured_at_ns,
                    name=row.name,
                    security_type=row.security_type,
                    isin=row.isin,
                    listed_date=row.listing_date,
                    extra_aliases=_yahoo_alias(row.local_symbol, row.mic, row.segment_mic),
                )
            )
            records.append(
                Instrument(
                    source=source,
                    symbol=symbol,
                    symbol_raw=row.symbol_raw,
                    local_ts=captured_at_ns,
                    source_ts=parse.as_of_ns,
                    asset_class=AssetClass.EQUITY,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    name=row.name,
                    exchange=row.segment_mic or row.mic,
                    security_type=row.security_type,
                    listing_date=row.listing_date,
                    mic=row.mic,
                    segment_mic=row.segment_mic,
                    isin=row.isin,
                    country=catalogue.get(row.mic).country,
                    currency=None,
                    price_scale=None,
                )
            )
    observations.sort(key=lambda item: (item.mic, item.local_symbol))
    records.sort(key=lambda item: item.symbol)
    return UniverseSnapshot(
        source=source,
        captured_at_ns=captured_at_ns,
        observations=tuple(observations),
        records=tuple(records),
        seen={mic: frozenset(locals_) for mic, locals_ in sorted(seen.items())},
        complete=True,
        skipped=dict(sorted(skipped.items())),
    )


def directory_records(snapshot: UniverseSnapshot, symbols: Sequence[str]) -> tuple[Instrument, ...]:
    """The snapshot's records for ``symbols``; ``["*"]`` is the whole directory."""
    if "*" in symbols:
        return snapshot.records
    wanted = frozenset(symbols)
    return tuple(record for record in snapshot.records if record.symbol in wanted)


def _retry_after(response: HttpResponse) -> float:
    raw = response.headers.get("retry-after")
    if raw is None:
        return _DEFAULT_BACKOFF_S
    try:
        seconds = float(raw)
    except ValueError:
        return _DEFAULT_BACKOFF_S
    return seconds if seconds >= 0 else _DEFAULT_BACKOFF_S


class DirectoryProvider(PullProvider):
    """An official listing directory: today's snapshot of the listings it publishes.

    Subclasses declare the host, the document URLs, the usage class and the operating MICs they
    cover, and implement :meth:`parse_documents`. Everything else (the shared bucket, the
    refusals, the snapshot, the single pass that writes the listing master) is here once.
    """

    host: ClassVar[str]
    urls: ClassVar[tuple[str, ...]]
    usage_class: ClassVar[UsageClass]
    supported_channels: ClassVar[frozenset[str] | None] = frozenset({"instrument"})
    supported_mics: ClassVar[frozenset[str] | None]
    unservable: ClassVar[Mapping[UnservableReason, str]] = UNSERVABLE_TEXT
    attribution: ClassVar[str | None] = None
    wants_settings: ClassVar[bool] = True

    @classmethod
    def grammar(cls) -> None:
        """A directory publishes local symbols, so there is nothing to translate."""
        return None

    @classmethod
    @abstractmethod
    def parse_documents(cls, documents: Mapping[str, bytes]) -> tuple[DirectoryParse, ...]:
        """Parse every document in ``urls`` order; raise DirectoryFormatError on any drift."""

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        *,
        fetch: Fetcher | None = None,
        bucket: TokenBucket | None = None,
        settings: Settings | None = None,
        master: ListingMaster | None = None,
        clock: Callable[[], int] = time.time_ns,
        **_: Any,
    ) -> None:
        # The opt-in is read before the base constructor, whose market-scope guard runs there.
        self._allow = allowed_countries(settings)
        self._owned: AiohttpFetcher | None = None
        if fetch is None:
            self._owned = AiohttpFetcher()
            fetch = self._owned
        self._fetch: Fetcher = fetch
        self._bucket = bucket
        self._settings = settings
        self._master = master
        self._clock = clock
        super().__init__(symbols, channels, out, registry)

    def _reject_unservable_mics(self) -> None:
        """``["*"]`` means the whole directory, which is its own market by definition; any other
        symbol list goes through the base scope guard."""
        if self.symbols == ["*"]:
            return
        super()._reject_unservable_mics()

    async def fetch_documents(self) -> dict[str, bytes]:
        """One GET per URL, in ``urls`` order, through the shared bucket. Any failure raises."""
        bucket = self._bucket if self._bucket is not None else budget(self.name, self.host)
        documents: dict[str, bytes] = {}
        for url in self.urls:
            await bucket.acquire()
            response = await self._fetch(HttpRequest(source=self.name, method="GET", url=url))
            raise_if_unservable(response, source=self.name, expect="text")
            if response.status == 200:
                documents[url] = response.body
                continue
            if response.status in (404, 410):
                raise SourceUnservableError(
                    self.name, UnservableReason.UNREACHABLE, f"HTTP {response.status} for {url}"
                )
            if response.status == 429:
                bucket.update_backoff(_retry_after(response))
                raise TransientProviderError(f"{self.name} rate-limited (429) for {url}")
            if 500 <= response.status <= 599:
                raise TransientProviderError(f"{self.name} answered {response.status} for {url}")
            raise ProviderError(f"{self.name} answered unexpected HTTP {response.status} for {url}")
        return documents

    async def snapshot(self) -> UniverseSnapshot:
        captured_at_ns = self._clock()
        documents = await self.fetch_documents()
        parses = self.parse_documents(documents)
        return snapshot_from_parses(
            self.name, parses, captured_at_ns=captured_at_ns, allow=self._allow
        )

    async def run(self, max_reconnects: int = -1) -> None:
        """One pass, then return. ``["*"]`` writes every record and applies the snapshot to the
        listing master; named symbols write only their own records and never touch the master."""
        star = self.symbols == ["*"]
        snapshot = await self.snapshot()
        for record in directory_records(snapshot, self.symbols):
            await self.out.put(record)
        if not star:
            return
        master = self._master
        owned: ListingMaster | None = None
        if master is None and self._settings is not None:
            owned = ListingMaster.for_data_dir(self._settings.data_dir)
            master = owned
        if master is None:
            return
        try:
            report = apply_universe(master, snapshot)
            log.info(
                "%s: %d upserted, %d conflicts, unseen marked %s",
                self.name,
                report.upserted,
                len(report.conflicts),
                list(report.unseen_marked_mics),
            )
        finally:
            if owned is not None:
                owned.close()

    async def backfill(
        self, channel: str, symbol: str, start_ns: int, end_ns: int
    ) -> AsyncIterator[Record]:
        raise ValueError(
            "a listing directory is today's snapshot and keeps no history; take one with "
            f"`collect --sources {self.name} --channels instrument --symbols '*'` "
            "(or `universe-sync`)"
        )
        yield  # pragma: no cover - unreachable; makes this an async generator

    async def close(self) -> None:
        if self._owned is not None:
            await self._owned.close()
