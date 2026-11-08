"""The core/markets error family.

Every class takes positional parameters, stores them as attributes and calls
``MarketError.__init__`` with all of them, so ``self.args`` equals the
constructor arguments. ``pickle`` and ``copy`` rebuild an error with
``type(self)(*self.args)``, so they work without a custom ``__reduce__``. This
follows ``CapabilityUnavailable`` in ``crocodile.core.errors``.

These errors are not exported from ``crocodile.core.errors``; that module
imports nothing from the package. ``ExpiredMarketError`` is a subclass of
``UnknownMarketError``: ``except UnknownMarketError`` means "this MIC cannot be
used".
"""

from __future__ import annotations

from datetime import date

from crocodile.core.errors import CrocodileError, StoreError

__all__ = [
    "CalendarRangeError",
    "ExpiredMarketError",
    "InvalidSymbolError",
    "ListingConflictError",
    "MarketError",
    "NoGrammarForMarket",
    "NotASessionError",
    "ReferenceDataError",
    "SanctionedMarketError",
    "UnknownCurrencyError",
    "UnknownMarketError",
]


class MarketError(CrocodileError):
    """Root of every core.markets error."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)


class ReferenceDataError(MarketError):
    """Packaged reference data is malformed: bad header, bad value or missing file."""

    def __init__(self, resource: str, detail: str) -> None:
        self.resource = resource
        self.detail = detail
        MarketError.__init__(self, resource, detail)

    def __str__(self) -> str:
        return f"reference data {self.resource!r} is malformed: {self.detail}"


class UnknownMarketError(MarketError, LookupError):
    """The MIC is not in the snapshot, or it is not on the exchange allow-list."""

    def __init__(self, mic: str, detail: str = "") -> None:
        self.mic = mic
        self.detail = detail
        MarketError.__init__(self, mic, detail)

    def __str__(self) -> str:
        message = f"unknown market code {self.mic!r}"
        if self.detail:
            message = f"{message}: {self.detail}"
        return message


class ExpiredMarketError(UnknownMarketError):
    """The MIC exists in ISO 10383 but its status is EXPIRED.

    ``comments`` is the registry's COMMENTS text, verbatim. ``replacement`` is the
    MIC the comments name, when the registry states one.
    """

    def __init__(
        self,
        mic: str,
        comments: str,
        replacement: str | None = None,
        expiry_date: date | None = None,
    ) -> None:
        self.mic = mic
        self.comments = comments
        self.replacement = replacement
        self.expiry_date = expiry_date
        self.detail = self._detail()
        MarketError.__init__(self, mic, comments, replacement, expiry_date)

    def _detail(self) -> str:
        parts = [f"market {self.mic} is EXPIRED"]
        if self.expiry_date is not None:
            parts.append(f"since {self.expiry_date.isoformat()}")
        parts.append(f"registry note: {self.comments!r}")
        if self.replacement:
            parts.append(f"use {self.replacement}")
        return "; ".join(parts)

    def __str__(self) -> str:
        return f"unknown market code {self.mic!r}: {self.detail}"


class UnknownCurrencyError(MarketError, ValueError):
    """The currency code or quote-unit spelling is not accepted."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        MarketError.__init__(self, code, detail)

    def __str__(self) -> str:
        message = f"unknown or unsupported currency {self.code!r}"
        if self.detail:
            message = f"{message}: {self.detail}"
        return message


class NoGrammarForMarket(MarketError, LookupError):
    """A source has no symbol grammar for this market, so it cannot be addressed there."""

    def __init__(self, source: str, mic: str) -> None:
        self.source = source
        self.mic = mic
        MarketError.__init__(self, source, mic)

    def __str__(self) -> str:
        return f"source {self.source!r} has no symbol grammar for market {self.mic!r}"


class SanctionedMarketError(MarketError):
    """The market sits in a default-closed sanctioned country."""

    def __init__(self, mic: str, country: str, reason: str) -> None:
        self.mic = mic
        self.country = country
        self.reason = reason
        MarketError.__init__(self, mic, country, reason)

    def __str__(self) -> str:
        return f"market {self.mic} ({self.country}) is sanctioned and default-closed: {self.reason}"


class ListingConflictError(MarketError, StoreError):
    """A canonical symbol would be pointed at a second listing.

    Also a ``StoreError``: the listing master refuses a write that would corrupt
    identity.
    """

    def __init__(self, symbol: str, existing: str, incoming: str) -> None:
        self.symbol = symbol
        self.existing = existing
        self.incoming = incoming
        MarketError.__init__(self, symbol, existing, incoming)

    def __str__(self) -> str:
        return (
            f"canonical symbol {self.symbol!r} already identifies {self.existing}; "
            f"refusing to point it at {self.incoming}"
        )


class CalendarRangeError(MarketError, ValueError):
    """A date lies before the earliest date a library calendar can answer."""

    def __init__(self, mic: str, requested: date, first: date | None, last: date | None) -> None:
        self.mic = mic
        self.requested = requested
        self.first = first
        self.last = last
        MarketError.__init__(self, mic, requested, first, last)

    def __str__(self) -> str:
        first = self.first.isoformat() if self.first is not None else "?"
        last = self.last.isoformat() if self.last is not None else "?"
        return (
            f"{self.mic}: {self.requested.isoformat()} is outside the calendar bounds "
            f"[{first}, {last}]"
        )


class InvalidSymbolError(MarketError, ValueError):
    """A symbol string does not satisfy the canonical symbol grammar."""

    def __init__(self, symbol: str, reason: str) -> None:
        self.symbol = symbol
        self.reason = reason
        MarketError.__init__(self, symbol, reason)

    def __str__(self) -> str:
        return f"invalid symbol {self.symbol!r}: {self.reason}"


class NotASessionError(MarketError, ValueError):
    """The requested day is not a trading session of this market."""

    def __init__(self, mic: str, day: date) -> None:
        self.mic = mic
        self.day = day
        MarketError.__init__(self, mic, day)

    def __str__(self) -> str:
        return f"{self.day.isoformat()} is not a trading session for {self.mic}"
