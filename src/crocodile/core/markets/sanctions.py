"""Default-closed sanctions policy for market venues (spec section 3.1 and section 5).

Ruling. A market is closed by default when its country is on ``sanctions.csv``.
Comprehensive-programme countries (RU including MOEX and SPB Exchange, BY, IR, SY and
KP) are closed. Venezuela, Myanmar, Sudan and Libya appear in ``exchanges.csv`` notes
only (``sanctions:targeted``) and are not closed. The list is data, not legal advice.
Changing it is the user's decision.

The country of a market is the country of its ISO 10383 row, read with ``get``, not
``require_active``. An expired Russian MIC is still Russian: the policy must not open
it because the registry retired the code. A segment carries its own country, so
``RTSX`` (a segment of ``MISX``) is ``RU``. An unknown MIC raises
``UnknownMarketError``: an unknown market is never assumed clean.

Opt-in. ``CROCODILE_ALLOW_SANCTIONED_MARKETS`` names the countries to open. It is read
once per run by :func:`allowed_countries` through :class:`~crocodile.core.config.Settings`,
never from ``os.environ`` here. :func:`parse_allowed_countries` refuses a malformed code
and a code that is not on the list, so a typo such as ``RS`` for ``RU`` fails loudly
instead of keeping a market closed while the operator believes it is open.
:func:`is_sanctioned` and :func:`require_not_sanctioned` are pure: the default
``allow=()`` is the closed policy, and they read no environment and no clock.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Final

import msgspec

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.markets._data import read_csv
from crocodile.core.markets.errors import ReferenceDataError, SanctionedMarketError
from crocodile.core.markets.mic import MicRegistry

__all__ = [
    "ALLOW_VARIABLE",
    "SANCTIONS_COLUMNS",
    "SANCTIONS_FILE",
    "SanctionEntry",
    "allowed_countries",
    "is_sanctioned",
    "parse_allowed_countries",
    "require_not_sanctioned",
    "sanctioned_countries",
]

SANCTIONS_FILE: Final = "sanctions.csv"
SANCTIONS_COLUMNS: Final = ("country", "source", "note")
ALLOW_VARIABLE: Final = "CROCODILE_ALLOW_SANCTIONED_MARKETS"
_COUNTRY_RE: Final = re.compile(r"[A-Z]{2}")
_ALPHA2_RE: Final = re.compile(r"[A-Za-z]{2}")


class SanctionEntry(msgspec.Struct, frozen=True, kw_only=True):
    """One default-closed country, with the programme page and the reason it is closed."""

    country: str
    """ISO 3166 alpha-2 code."""

    source: str
    """Programme page URL."""

    note: str


@functools.cache
def sanctioned_countries() -> Mapping[str, SanctionEntry]:
    """Return the packaged sanctions list keyed by country. Loaded once.

    Raises:
        ReferenceDataError: A country is duplicated or is not an alpha-2 code.
    """
    table: dict[str, SanctionEntry] = {}
    for line, row in enumerate(read_csv(SANCTIONS_FILE, SANCTIONS_COLUMNS), start=2):
        country = row["country"]
        if not _COUNTRY_RE.fullmatch(country):
            raise ReferenceDataError(
                SANCTIONS_FILE,
                f"line {line}: country {country!r} is not an ISO 3166 alpha-2 code",
            )
        if country in table:
            raise ReferenceDataError(SANCTIONS_FILE, f"line {line}: duplicate country {country}")
        table[country] = SanctionEntry(country=country, source=row["source"], note=row["note"])
    return MappingProxyType(table)


def parse_allowed_countries(raw: str, *, spelling: str = ALLOW_VARIABLE) -> frozenset[str]:
    """Parse a comma-separated allow-list into upper-case country codes.

    Tokens are stripped, empty tokens are skipped and repeats collapse, so ``""``,
    ``","`` and ``"RU,"`` are all valid. A token that is not two letters, or a code that
    is not on the sanctions list, raises :class:`ConfigError` naming ``spelling``.

    Raises:
        ConfigError: A token is malformed or names a country that is not sanctioned.
    """
    known = sanctioned_countries()
    codes: set[str] = set()
    for raw_token in raw.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if _ALPHA2_RE.fullmatch(token) is None:
            raise ConfigError(f"{spelling}: {token!r} is not an ISO 3166 alpha-2 country code")
        code = token.upper()
        if code not in known:
            listed = ", ".join(sorted(known))
            raise ConfigError(
                f"{spelling}: {code} is not on the sanctions list ({listed}); nothing to allow"
            )
        codes.add(code)
    return frozenset(codes)


def allowed_countries(settings: Settings | None = None) -> frozenset[str]:
    """Return the countries the operator opted in through ``Settings``.

    Call it once per run and pass the result down. ``settings`` defaults to
    :meth:`Settings.from_env`.

    Raises:
        ConfigError: The configured value is malformed or names an unlisted country.
    """
    source = settings if settings is not None else Settings.from_env()
    return parse_allowed_countries(source.allow_sanctioned_markets)


def _refuse_str_allow(allow: object) -> None:
    """Refuse a ``str`` allow-list: a string is a collection of characters, not of codes."""
    if isinstance(allow, str):
        raise TypeError(
            "allow must be a collection of country codes, not a str (a str would match "
            "substrings, so allow='IRU' opens RU and IR); wrap the value in "
            "parse_allowed_countries(...)"
        )


def _country_of(mic: str, mics: MicRegistry | None) -> str:
    registry = mics if mics is not None else MicRegistry()
    return registry.get(mic).country


def is_sanctioned(
    mic: str, *, allow: Collection[str] = (), mics: MicRegistry | None = None
) -> bool:
    """True when ``mic`` sits in a default-closed country that ``allow`` does not open.

    ``allow`` holds upper-case alpha-2 codes, as :func:`parse_allowed_countries` returns
    them. There is no normalisation here. A ``str`` is refused, not read as a set of codes.

    Raises:
        TypeError: ``allow`` is a ``str``; pass :func:`parse_allowed_countries` output.
        UnknownMarketError: ``mic`` is not in ISO 10383. An unknown market is refused.
    """
    _refuse_str_allow(allow)
    country = _country_of(mic, mics)
    return country in sanctioned_countries() and country not in allow


def require_not_sanctioned(
    mic: str, *, allow: Collection[str] = (), mics: MicRegistry | None = None
) -> None:
    """Return ``None`` for an open market, otherwise raise :class:`SanctionedMarketError`.

    The error names the country, the programme page and the note from ``sanctions.csv``,
    and the variable that opts in. A ``str`` ``allow`` is refused, as in
    :func:`is_sanctioned`.

    Raises:
        TypeError: ``allow`` is a ``str``; pass :func:`parse_allowed_countries` output.
        SanctionedMarketError: ``mic`` is in a closed country that ``allow`` does not open.
        UnknownMarketError: ``mic`` is not in ISO 10383.
    """
    _refuse_str_allow(allow)
    country = _country_of(mic, mics)
    entry = sanctioned_countries().get(country)
    if entry is None or country in allow:
        return None
    reason = (
        f"{country} is on the default-closed sanctions list ({entry.source}). "
        f"{entry.note} Set {ALLOW_VARIABLE}={country} to opt in."
    )
    raise SanctionedMarketError(mic, country, reason)
