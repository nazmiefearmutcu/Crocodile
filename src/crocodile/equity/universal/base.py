"""Shared contract and helpers of the universal sources (spec section 3.10, "Ortak sozlesme").

Every universal source defines the same seams: the ``unservable`` refusal vocabulary, the
daily-bar timestamp rule, the ISIN shape check, the universe snapshot that writes a complete
read back into the listing master, and the declaration check a source class must pass.
Nothing here opens a socket; the one HTTP path is :mod:`crocodile.equity.universal.http`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from crocodile.core.markets.errors import ListingConflictError
from crocodile.core.markets.master import ListingMaster, ListingObservation
from crocodile.core.markets.usage import UsageClass
from crocodile.core.schema.enums import Channel
from crocodile.equity.providers.base import ProviderError

if TYPE_CHECKING:
    from crocodile.core.schema.records import Instrument

__all__ = [
    "SourceUnservableError",
    "UniverseApplyReport",
    "UniverseSnapshot",
    "UnservableReason",
    "apply_universe",
    "contract_violations",
    "daily_bar_ts_ns",
    "is_valid_isin",
    "session_date_of",
]

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_SECOND: Final = 1_000_000_000
_NS_PER_DAY: Final = 86_400 * _NS_PER_SECOND
_ISIN_SHAPE: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_CHANNEL_VALUES: Final[frozenset[str]] = frozenset(channel.value for channel in Channel)


class UnservableReason(StrEnum):
    """Why a source refused to answer; each one is a wall, never a retry."""

    BOT_CHALLENGE = "bot_challenge"  # HTML/bot/consent/proof-of-work page or any 3xx redirect wall
    FORBIDDEN = "forbidden"  # 401/403
    GEO_BLOCKED = "geo_blocked"  # 451
    TIMEOUT = "timeout"  # client-side timeout
    UNREACHABLE = "unreachable"  # DNS/connection/TLS failure


class SourceUnservableError(ProviderError):
    """A universal source refused the request: the wall is recorded, never bypassed."""

    def __init__(self, source: str, reason: UnservableReason, detail: str) -> None:
        super().__init__(source, reason, detail)
        self.source = source
        self.reason = reason
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.source} is unservable ({self.reason.value}): {self.detail}"


def daily_bar_ts_ns(session: date) -> int:
    """UTC midnight of an exchange-local session date, in epoch ns.

    This is the daily-bar `source_ts` rule.
    """
    return (session - _EPOCH.date()).days * _NS_PER_DAY


def session_date_of(ts_seconds: int, tz: ZoneInfo) -> date:
    """The exchange-local calendar date of an epoch-seconds instant."""
    return (_EPOCH + timedelta(seconds=ts_seconds)).astimezone(tz).date()


def is_valid_isin(value: str) -> bool:
    """ISO 6166 shape and check digit.

    Shape `[A-Z]{2}[A-Z0-9]{9}[0-9]`. Every character but the last is expanded to base-36
    digits (`str(int(c, 36))`), Luhn runs over that digit string (double every second digit
    from the right, subtract 9 above 9), and the check digit must equal
    (10 - sum % 10) % 10. Used by tasks 11 (TradingView `isin` column) and 12 (NSE
    `ISIN NUMBER`).
    """
    if _ISIN_SHAPE.fullmatch(value) is None:
        return False
    body = "".join(str(int(char, 36)) for char in value[:-1])
    total = 0
    for position, char in enumerate(reversed(body)):
        digit = int(char)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return int(value[-1]) == (10 - total % 10) % 10


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    source: str
    captured_at_ns: int
    observations: tuple[ListingObservation, ...]
    records: tuple[Instrument, ...]
    seen: Mapping[str, frozenset[str]]  # operating MIC -> local symbols this read observed
    complete: bool  # every page/row of the source was read
    skipped: Mapping[str, int]  # reason -> count (reasons are short snake_case strings)


@dataclass(frozen=True, slots=True)
class UniverseApplyReport:
    source: str
    upserted: int
    conflicts: tuple[str, ...]  # one str(ListingConflictError) per refused observation
    unseen_marked_mics: tuple[str, ...]  # sorted; empty when snapshot.complete is False


def apply_universe(master: ListingMaster, snapshot: UniverseSnapshot) -> UniverseApplyReport:
    """Upsert every observation (a ListingConflictError is recorded, not raised); when
    `snapshot.complete`, call `master.mark_unseen(mic, snapshot.source, seen,
    snapshot.captured_at_ns)` for every MIC in `snapshot.seen`, in sorted MIC order. Never marks
    anything when incomplete."""
    upserted = 0
    conflicts: list[str] = []
    for observation in snapshot.observations:
        try:
            master.upsert(observation)
        except ListingConflictError as exc:
            conflicts.append(str(exc))
            continue
        upserted += 1
    marked: list[str] = []
    if snapshot.complete:
        for mic in sorted(snapshot.seen):
            locals_seen = snapshot.seen[mic]
            if not locals_seen:
                # An empty sweep is an outage, not a market with no listings: mark_unseen refuses
                # it, so an empty set is skipped here instead of failing the whole apply.
                continue
            master.mark_unseen(mic, snapshot.source, locals_seen, snapshot.captured_at_ns)
            marked.append(mic)
    return UniverseApplyReport(
        source=snapshot.source,
        upserted=upserted,
        conflicts=tuple(conflicts),
        unseen_marked_mics=tuple(marked),
    )


def contract_violations(cls: type) -> list[str]:
    """Everything wrong with `cls` as a universal source.

    Returns [] when it declares the spec 3.10 contract: non-empty `name: str`;
    `usage_class: UsageClass`; `supported_channels`: non-empty frozenset of `Channel` values;
    `supported_mics`: frozenset[str] (not None for universal sources); `unservable`:
    Mapping[UnservableReason, str] with non-blank strings; `attribution: str | None`; a
    classmethod `grammar() -> SymbolGrammar | None`.
    """
    problems: list[str] = []

    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name.strip():
        problems.append("name must be a non-empty str")

    if not isinstance(getattr(cls, "usage_class", None), UsageClass):
        problems.append("usage_class must be a UsageClass member")

    channels = getattr(cls, "supported_channels", None)
    if (
        not isinstance(channels, frozenset)
        or not channels
        or not all(isinstance(item, str) and item in _CHANNEL_VALUES for item in channels)
    ):
        problems.append("supported_channels must be a non-empty frozenset of Channel values")

    mics = getattr(cls, "supported_mics", None)
    if not isinstance(mics, frozenset) or not all(isinstance(mic, str) for mic in mics):
        problems.append("supported_mics must be a frozenset[str], not None, for a universal source")

    unservable = getattr(cls, "unservable", None)
    if not isinstance(unservable, Mapping) or not all(
        isinstance(key, UnservableReason) and isinstance(text, str) and text.strip()
        for key, text in unservable.items()
    ):
        problems.append("unservable must map UnservableReason members to non-blank strings")

    if not hasattr(cls, "attribution"):
        problems.append("attribution must be declared (a str or None)")
    elif (attribution := cls.attribution) is not None and not isinstance(attribution, str):
        problems.append("attribution must be a str or None")

    if not callable(getattr(cls, "grammar", None)):
        problems.append("grammar must be a classmethod returning SymbolGrammar or None")

    return problems
