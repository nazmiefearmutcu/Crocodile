"""The enumerations both asset classes share.

Crypto and equity arrived with two vocabularies for one set of ideas. These are
the reconciled forms: :class:`Side` and :class:`OptType` were already common,
:class:`Tape`, :class:`SecurityType` and :class:`FundPeriod` came from equity,
and :class:`CorpActionType` and :class:`Channel` are unions of both. :class:`PriceAdjustment`
came with the global-equities schema.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "CHANNEL_SUCCESSORS",
    "AssetClass",
    "Channel",
    "CorpActionType",
    "FundPeriod",
    "OptType",
    "PriceAdjustment",
    "SecurityType",
    "Side",
    "Tape",
    "channel_predecessors",
    "successor_channel",
]


class AssetClass(StrEnum):
    """Which market a record came from.

    One struct serves both asset classes, so a consumer holding a record needs the record
    itself to say which of its fields are meaningfully absent: an unset ``tape`` is normal
    on a crypto trade and a defect on an equity one. ``source`` is a free-form venue or
    provider string and cannot answer that without an out-of-band registry.

    It also makes ``GROUP BY asset_class`` the natural way to ask the symmetry question of
    the lake. One low-cardinality string column is close to free under dictionary encoding.
    """

    CRYPTO = "crypto"
    EQUITY = "equity"


class Side(StrEnum):
    """Aggressor side of a trade."""

    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


class OptType(StrEnum):
    """Option right.

    Crypto spelled these ``CALL``/``PUT`` and equity spelled them ``C``/``P``, over
    identical values, so persisted data is compatible with either spelling. The
    crypto member names win because they read as English at a call site.
    """

    CALL = "C"
    PUT = "P"


class Tape(StrEnum):
    """US consolidated-tape identifier: NYSE (A), NYSE Arca/regional (B), Nasdaq (C).

    Equity-only. A crypto record leaves it unset.
    """

    A = "A"
    B = "B"
    C = "C"
    UNKNOWN = "unknown"


class SecurityType(StrEnum):
    """Instrument class of a listed equity-market instrument.

    The first eight members are the US reference vocabulary; ``ETN`` through ``INDEX`` came
    with the global universe (spec §3.7): exchange-traded notes, global depositary receipts,
    listed funds that are not ETFs, bonds, certificates, structured products and indices.
    ``FUND`` names an *exchange-listed* fund. ``UNKNOWN`` is a source saying something this
    enum does not model, distinct from a ``None`` field, which is a source saying nothing.
    """

    CS = "CS"
    ETF = "ETF"
    ADR = "ADR"
    REIT = "REIT"
    PFD = "PFD"
    WARRANT = "WARRANT"
    UNIT = "UNIT"
    RIGHT = "RIGHT"
    ETN = "ETN"
    GDR = "GDR"
    FUND = "FUND"
    BOND = "BOND"
    CERTIFICATE = "CERTIFICATE"
    STRUCTURED = "STRUCTURED"
    INDEX = "INDEX"
    UNKNOWN = "unknown"


class CorpActionType(StrEnum):
    """An event that rebases a price or quantity series.

    The last two members are the crypto forms. A token redenomination and a chain
    migration change the units a series is quoted in exactly as a split does, and
    obey the same cumulative-adjustment-factor arithmetic — which is why one
    CRSP-style adjustment calculator can serve both asset classes.
    """

    SPLIT = "split"
    DIVIDEND_CASH = "dividend_cash"
    DIVIDEND_STOCK = "dividend_stock"
    SPINOFF = "spinoff"
    MERGER = "merger"
    TICKER_CHANGE = "ticker_change"
    TOKEN_SPLIT = "token_split"
    CHAIN_MIGRATION = "chain_migration"


class FundPeriod(StrEnum):
    """Reporting period a fundamental figure covers. ``H1``/``H2`` are half-years, which issuers
    outside the US commonly report (spec §3.7)."""

    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    H1 = "H1"
    H2 = "H2"
    FY = "FY"
    TTM = "TTM"


class PriceAdjustment(StrEnum):
    """Which corporate actions a source had already applied to the prices it published.

    Prices are stored as the source published them (spec §3.3, §3.7), so a reader needs the
    record to say whether a past split or dividend is already baked in; otherwise the CRSP-style
    calculator in :mod:`crocodile.core.corpactions.calculator` adjusts an adjusted series twice.
    ``OHLCV.adjustment`` is ``None`` when nobody measured it, which is a different claim from
    ``NONE``: that one is a measured statement that the prices are as traded.
    """

    NONE = "none"
    SPLIT = "split"
    SPLIT_DIVIDEND = "split_dividend"


class Channel(StrEnum):
    """Every record tag, from both asset classes.

    Both source enums were narrower than their own record unions — crypto listed 10
    of 16 tags and equity 20 of 25 — so a channel name could exist as a struct tag
    and not as an enum member. This is the union of every tag both record modules
    define; Task 5 adds the gate that keeps it that way.
    """

    # Shared between the asset classes.
    TRADE = "trade"
    QUOTE = "quote"
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    BOOK_TICKER = "book_ticker"
    DEPTH = "depth"
    OHLCV = "ohlcv"
    BAR = "bar"

    # Crypto derivatives.
    DERIVATIVE_TICKER = "derivative_ticker"
    OPTIONS_CHAIN = "options_chain"
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    LIQUIDATION = "liquidation"

    # Equity reference, corporate and regulatory data.
    AUCTION = "auction"
    TRADING_STATUS = "trading_status"
    INSTRUMENT = "instrument"
    CORP_ACTION = "corp_action"
    FUNDAMENTAL = "fundamental"
    INSIDER = "insider"
    HOLDING_13F = "holding_13f"
    SHORT_INTEREST = "short_interest"
    SHORT_VOLUME = "short_volume"
    FILING = "filing"
    OPTION_QUOTE = "option_quote"
    INDEX_VALUE = "index_value"
    MACRO_SERIES = "macro_series"

    # On-chain and social, carried by both forks already.
    FARCASTER_CORRELATION = "farcaster_correlation"
    RESERVE_DATA_UPDATED = "reserve_data_updated"
    LIQUIDATION_CALL = "liquidation_call"
    LIMIT_ORDER_FILL = "limit_order_fill"
    BALANCE_CORRECTION = "balance_correction"
    POR_UPDATE = "por_update"


CHANNEL_SUCCESSORS: Final[dict[str, str]] = {
    Channel.BAR.value: Channel.OHLCV.value,
    Channel.OPTION_QUOTE.value: Channel.OPTIONS_CHAIN.value,
}
"""Retired tag → the tag whose *record* absorbed it.

This maps structs, not directories. ``Bar`` collapsed into ``OHLCV`` and ``OptionQuote``
into ``OptionsChain``, so a row on disk carrying ``channel='bar'`` has to decode into
something, and :mod:`crocodile.core.store.rows` uses this table to decide what. Without
it ``replay(["bar"])`` raised ``ValueError: Unknown channel tag: 'bar'`` and every row in
a ``channel=bar/`` directory was unreachable through the record API — which is the loss
this exists to prevent.

**A retired tag stays its own channel on disk.** ``channel=bar/`` is read by asking for
``bar``; ``channel=ohlcv/`` by asking for ``ohlcv``. Three review rounds tried instead to
make ``ohlcv`` silently cover both directories, and every one of them broke: first
duplicate rows and an inventory that reported eight of five, then a
``(symbol, local_ts, interval)`` dedup key that discarded 249 bars of a 250-bar Yahoo
history — that provider stamps a whole fetched history with one ``local_ts``, and the key
carried no ``source``. Transparency is what forces the deduplication, and deduplication
needs a cross-provider row identity this data does not support.

Rewriting the directory name is :mod:`crocodile.core.store.migrate`'s job — ``crocodile
migrate-lake`` renames partitions and rewrites no Parquet byte, and it is where the
design puts the tag rename (Phase 4). Reads report the tags that are on disk, honestly:
a lake holding both directories holds both channels, with the true row count for each.
"""


def successor_channel(channel: str) -> str:
    """Return the surviving tag for ``channel``, or ``channel`` if it is not retired.

    Answers "which record decodes a row under this tag", which is the only question
    :data:`CHANNEL_SUCCESSORS` is for. It is emphatically not "which directories does a
    read of this channel cover"; see that table for what happened when it was.
    """
    return CHANNEL_SUCCESSORS.get(channel, channel)


def channel_predecessors(channel: str) -> tuple[str, ...]:
    """Return the retired tags whose rows decode into ``channel``'s record.

    Used to widen a *vocabulary* — the channel names a CLI accepts from a user, where
    ``bar`` and ``ohlcv`` are two spellings a connector answers to. Never to widen a
    glob: partitions under a retired tag are read by naming that tag.
    """
    return tuple(old for old, new in CHANNEL_SUCCESSORS.items() if new == channel)
