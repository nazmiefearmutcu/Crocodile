"""Canonical listing symbols: ``local@MIC`` with frozen US bare tickers.

Ruling (spec section 3.4). A listing is identified by its local symbol and its
operating MIC, written ``{local}@{MIC}`` for every market outside the United States.
A US listing keeps its bare ticker (``AAPL``, ``BRK.B``), because that form is
already how every US caller, file and history names it. The operating MIC is used
rather than a segment: ``7203@XTKS`` is written ``7203@XJPX``, and ``ITX@XMAD`` is
written ``ITX@BMEX``. ``parse_listing_symbol`` accepts only these canonical forms.

Cost of the ruling. The canonical string feeds ``mmh3(symbol) % 128`` bucketing
(``src/crocodile/core/store/rows.py:127``, codemap section 3.2). Changing the format
therefore moves rows between buckets and needs a lake migration. Sanctions are not
part of identity: ``SBER@MISX`` names the listing whether or not the policy opens it.

Local symbols are Unicode NFC-normalised and keep their case. ``@`` and ``:`` are
refused: ``@`` is the canonical separator, and ``:`` marks ``source:RAW`` in
``surfaces/dispatch.py``. Whitespace (``Zs``, ``Zl``, ``Zp`` and ``str.isspace``),
control characters (``Cc``) and format characters (``Cf``) are refused as well. The
``Cf`` rule goes beyond the spec's "control characters": zero-width spaces, byte
order marks and bidi marks are invisible, so two different symbols could look
identical. Surrogate code points (``Cs``) are refused for a harder reason: they are
not characters, yet ``json.loads`` (aiohttp's default decoder) returns one for an
escaped lone surrogate, and a string holding one cannot be encoded as UTF-8. The
listing master's SQLite insert would raise ``UnicodeEncodeError`` and ``mmh3``
bucketing would crash the interpreter (mmh3 5.2.1), where callers expect
:class:`InvalidSymbolError`. ``.`` and ``-`` stay legal (``CERA.N0000``, ``NOVO-B``).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from crocodile.core.markets.errors import InvalidSymbolError, MarketError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.mic import MicRegistry

__all__ = [
    "US_COUNTRY",
    "is_listing_symbol",
    "listing_symbol",
    "parse_listing_symbol",
    "validate_local_symbol",
]

US_COUNTRY: Final = "US"
_MIC_RE: Final = re.compile(r"[A-Z0-9]{4}")
"""An operating MIC: four upper-case letters or digits. Matched with ``fullmatch``."""

_SEPARATOR: Final = "@"
_REFUSED_SEPARATORS: Final = frozenset({"@", ":"})
_REFUSED_SPACE_CATEGORIES: Final = frozenset({"Zs", "Zl", "Zp"})
_REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf"})
_SURROGATE_CATEGORY: Final = "Cs"


def validate_local_symbol(local: str) -> str:
    """Return the NFC form of a local symbol, or raise :class:`InvalidSymbolError`.

    Raises:
        TypeError: ``local`` is not a ``str``.
        InvalidSymbolError: The symbol is empty, or holds ``@``, ``:``, whitespace,
            a control character, a format character or a surrogate code point.
    """
    if not isinstance(local, str):
        raise TypeError(f"local symbol must be a str, got {type(local).__name__}")
    normalised = unicodedata.normalize("NFC", local)
    if not normalised:
        raise InvalidSymbolError(local, "the local symbol is empty")
    for char in normalised:
        if char in _REFUSED_SEPARATORS:
            reason = (
                "'@' is the canonical separator"
                if char == _SEPARATOR
                else "':' marks source:RAW in dispatch"
            )
            raise InvalidSymbolError(local, reason)
        category = unicodedata.category(char)
        if char.isspace() or category in _REFUSED_SPACE_CATEGORIES:
            raise InvalidSymbolError(local, f"whitespace U+{ord(char):04X} is not allowed")
        if category in _REFUSED_CATEGORIES:
            raise InvalidSymbolError(
                local,
                f"control or format character U+{ord(char):04X} is not allowed",
            )
        if category == _SURROGATE_CATEGORY:
            raise InvalidSymbolError(
                local,
                f"surrogate code point U+{ord(char):04X} is not a Unicode character",
            )
    return normalised


def listing_symbol(
    local: str,
    mic: str,
    *,
    mics: MicRegistry | None = None,
    exchanges: ExchangeRegistry | None = None,
) -> str:
    """Return the canonical symbol of ``local`` listed on ``mic``.

    A segment MIC maps to its operating MIC first, so ``7203`` on ``XTKS`` becomes
    ``7203@XJPX``. A US operating MIC (or a US segment) keeps the bare ticker.

    Raises:
        InvalidSymbolError: ``local`` is not a valid local symbol.
        UnknownMarketError: ``mic`` is not in ISO 10383, or it is not on the exchange
            allow-list.
        ExpiredMarketError: ``mic`` is an expired ISO 10383 code.
    """
    normalised = validate_local_symbol(local)
    registry = mics if mics is not None else MicRegistry()
    catalogue = exchanges if exchanges is not None else ExchangeRegistry()
    operating = registry.operating(mic)
    info = catalogue.get(operating)
    if info.country == US_COUNTRY:
        return normalised
    return f"{normalised}{_SEPARATOR}{operating}"


def parse_listing_symbol(
    symbol: str,
    *,
    mics: MicRegistry | None = None,
    exchanges: ExchangeRegistry | None = None,
) -> tuple[str, str | None]:
    """Split a canonical symbol into ``(local, mic)``. ``mic`` is ``None`` for US tickers.

    Only canonical forms are accepted. A segment MIC, a US listing written with
    ``@``, a lower-case MIC, an unknown or expired MIC, and a MIC outside the
    allow-list are all refused. Nothing is upper-cased or mapped to an operating
    MIC here.

    Raises:
        InvalidSymbolError: The string is not a canonical symbol.
        UnknownMarketError: The MIC is unknown or not on the allow-list.
        ExpiredMarketError: The MIC is expired.
    """
    registry = mics if mics is not None else MicRegistry()
    catalogue = exchanges if exchanges is not None else ExchangeRegistry()
    if _SEPARATOR not in symbol:
        if validate_local_symbol(symbol) != symbol:
            raise InvalidSymbolError(symbol, "not NFC-normalised")
        return symbol, None

    parts = symbol.split(_SEPARATOR)
    if len(parts) != 2:
        raise InvalidSymbolError(symbol, "exactly one '@' separates the local symbol and the MIC")
    raw_local, mic = parts
    if not raw_local:
        raise InvalidSymbolError(symbol, "the local symbol before '@' is empty")
    local = validate_local_symbol(raw_local)
    if local != raw_local:
        raise InvalidSymbolError(symbol, "the local symbol is not NFC-normalised")
    if _MIC_RE.fullmatch(mic) is None:
        raise InvalidSymbolError(
            symbol,
            f"MIC {mic!r} must be four upper-case letters or digits; MICs are never upper-cased",
        )
    entry = registry.get(mic)
    if not entry.is_active:
        registry.require_active(mic)
    if entry.is_segment:
        raise InvalidSymbolError(
            symbol,
            f"{mic} is a segment of {entry.operating_mic}; canonical symbols use the operating MIC",
        )
    info = catalogue.get(mic)
    if info.country == US_COUNTRY:
        raise InvalidSymbolError(symbol, f"US listings use the bare ticker {local!r}")
    return local, mic


def is_listing_symbol(
    symbol: str,
    *,
    mics: MicRegistry | None = None,
    exchanges: ExchangeRegistry | None = None,
) -> bool:
    """True only for a canonical non-US ``local@MIC`` symbol. Never raises a market error.

    This is the strict canonical-form check, for callers that need one. It answers whether
    the string parses as a listing symbol whose MIC is known to the registries. Task 08's
    dispatch does not call it: dispatch uses lenient ``MicRegistry`` membership instead
    (interfaces section 5.7), so a symbol that this check rejects can still resolve there.
    """
    try:
        _, mic = parse_listing_symbol(symbol, mics=mics, exchanges=exchanges)
    except MarketError:
        return False
    return mic is not None
