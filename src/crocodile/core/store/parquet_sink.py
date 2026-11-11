"""Buffered, hive-partitioned Parquet sink for canonical Records.

Partition layout (Appendix §4):
    data/source={S}/channel={C}/date=YYYY-MM-DD/bucket={0..127}/part-{uuid}.parquet

``source={S}`` is the unified partition key. Crypto lakes written before the
merge use ``exchange={S}`` and equity lakes ``provider={S}``; readers still
accept both, and ``crocodile.core.store.migrate.migrate_lake`` renames them.

Write policy:
  - Buffers rows per channel.
  - Auto-flushes when a channel buffer reaches ``max_buffer_rows`` rows.
  - Time-based flush is triggered on the next ``put`` after ``flush_interval_seconds``
    has elapsed, or explicitly via ``flush()`` / ``close()``.
  - A new ``part-{uuid}.parquet`` file is written on every flush; existing files are
    **never appended to** (Parquet footers are immutable).
  - Each part is written via temp file + same-directory rename so readers never
    observe a partial ``part-*.parquet`` if the process crashes mid-write.

Compression: ZSTD level 5 (streaming sweet spot per Appendix §4).
Row group size: 250 000 rows.

Per-family file schemas
-----------------------
One sink now writes both legacy record unions, and each keeps the file schema
its fork wrote. Two reasons, in order of force:

1. **The lake migration renames, it never rewrites.** ``migrate_lake`` moves
   ``exchange=`` / ``provider=`` directories to ``source=`` without reading a
   single Parquet byte, so the files already on disk keep the columns their
   fork gave them. Anything this sink appends into the same partition has to
   agree with them field for field. Widening one common set to cover both
   families would put ``provider``/``source_ts`` into every new crypto file and
   ``exchange_ts`` into every new equity file — new parts would stop matching
   the old parts beside them, and a single ``read_parquet`` over the partition
   would have to reconcile two schemas. Widening is therefore not available;
   the file schema is frozen per family.
2. **``channel`` alone cannot pick a schema anyway.** The two unions reuse nine
   tags for different structs — ``trade``, ``ohlcv``, ``book_snapshot``,
   ``book_delta``, ``liquidation_call``, ``reserve_data_updated``,
   ``limit_order_fill``, ``balance_correction``, ``por_update``. Crypto
   ``trade`` carries ``exchange``/``amount``/``side``; equity ``trade`` carries
   ``provider``/``size``/``tape``/``venue``. A per-channel table keyed on the
   tag alone can only ever be right for one of them.

Nullable widening of the canonical family
-----------------------------------------
Ruling (global equities, spec §3.7): adding a trailing nullable field to a canonical
record, and with it a nullable column to the canonical family's file schema, is not
the widening the two reasons above forbid. Both reasons are about the *legacy*
families: a column added to the crypto or equity table would make new parts in a
renamed-not-rewritten partition disagree with the files beside them, and a table keyed
on ``channel`` cannot serve two structs. Neither applies to the canonical family. Its
schema is derived from the structs (``_build_canonical_schema``), every Parquet column
is nullable, the catalog reads every group with ``union_by_name`` and the compactor
concatenates ``diagonal_relaxed``, so a pre-widening canonical part beside a
post-widening one reads NULL for the new columns on the old rows, and
``rows.from_row`` leaves an absent column at the field's ``None`` default. The two
legacy tables below stay frozen; nothing writes legacy-family rows any more.

NULL means "the source did not say": unknown, never USD and never scale 0. The fields
this ruling admitted are ``currency``/``price_scale`` on ``OHLCV``, ``Trade``, ``Quote``
and ``CorporateAction``, ``adjustment`` on ``OHLCV``, and ``mic``/``segment_mic``/
``isin``/``country``/``currency``/``price_scale`` on ``Instrument``.
``tests/store/test_stage_b_mixed_partitions.py`` reads an old part and a new part in
one query and pins the NULLs.

Cost if wrong: a lake written by 0.3.x reads these columns as NULL through ``from_row``
always, and through SQL whenever the query also covers a post-widening part. SQL over a
lake, or a date range, holding *no* post-widening part has no such column at all and
fails with a binder error naming it rather than inventing a value.

The family is read off the row's own fields, reusing the asymmetry
``crocodile.core.store.rows.to_row`` already depends on: only equity records
name their origin ``provider``, only crypto records name it ``exchange``, and
only the canonical records carry ``asset_class``. That is one discriminator for
the whole module rather than a second parallel notion of "which fork".

Book levels follow the same freeze: crypto lakes hold
``list[struct{price, amount}]`` and equity lakes ``list[struct{price, size}]``,
so the sink writes whichever name the family's existing files use. Coercion is
driven off the selected schema rather than a hard-coded channel list, so the
writer cannot disagree with the dtype it is about to declare.

Unknown fields are refused, not dropped. A row carrying a field its channel
schema does not know used to be silently truncated on the way to Parquet; it
now raises. Where the field was one the fork genuinely wrote, the schema was
widened to keep it (see the crypto ``limit_order_fill`` / ``balance_correction``
/ ``por_update`` entries, whose payloads this sink was discarding wholesale).
"""

from __future__ import annotations

import asyncio
import enum
import time
import types
import typing
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import msgspec.structs
import polars as pl

from crocodile.core.schema.enums import Channel
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import Record, _Header
from crocodile.core.sink.base import Sink
from crocodile.core.store.rows import (
    _DERIVED_PARTITION_COLS,
    _FAMILY_MARKERS,
    FAMILY_CANONICAL,
    FAMILY_CRYPTO,
    FAMILY_EQUITY,
    to_row,
)

# ---------------------------------------------------------------------------
# Record families
# ---------------------------------------------------------------------------
# Which legacy union a flattened row came from. See the module docstring for
# why this, and not ``channel``, selects the file schema.
#
# The names and the marker table are imported rather than declared: the same
# asymmetry decides the partition key in ``to_row``, the asset class in
# ``from_row`` and the file schema here, and it used to be written out three
# times. Two of the three then disagreed — ``rows._header`` matched ``exchange``
# before ``provider`` and stamped every equity row ``CRYPTO``. ``rows`` owns the
# table because it is the module both directions of the flattening go through;
# these re-exports keep ``parquet_sink.FAMILY_CRYPTO`` importable where it
# always was.

# ``source`` names the top-level partition directory and is deliberately kept
# out of every file schema (see ``crocodile.core.store.rows``), so it is the one
# row key that may be absent from the schema without that being data loss.
_PATH_ONLY_COLUMNS = frozenset({"source"})

# ---------------------------------------------------------------------------
# Polars schema definitions per channel
# ---------------------------------------------------------------------------
# Fields every crypto record carries.
_CRYPTO_COMMON_FIELDS: dict[str, Any] = {
    "exchange": pl.Utf8,
    "symbol": pl.Utf8,
    "symbol_raw": pl.Utf8,
    "exchange_ts": pl.Int64,
    "local_ts": pl.Int64,
    # Partition columns (written as path components, kept in the row for DuckDB
    # hive reads that include them).
    "channel": pl.Utf8,
    "date": pl.Utf8,
    "bucket": pl.Int32,
}

# Fields every equity record carries. ``exchange`` is vestigial — equity's own
# flattener moves ``Instrument.exchange`` to ``exchange_name`` and nothing else
# populates it — but legacy equity lakes have the column, and the migration
# renames directories without rewriting files, so dropping it would make new
# parts disagree with the parts already sitting beside them.
_EQUITY_COMMON_FIELDS: dict[str, Any] = {
    "provider": pl.Utf8,
    "symbol": pl.Utf8,
    "symbol_raw": pl.Utf8,
    "source_ts": pl.Int64,
    "local_ts": pl.Int64,
    "channel": pl.Utf8,
    "date": pl.Utf8,
    "bucket": pl.Int32,
    "exchange": pl.Utf8,
}

# A named-struct dtype for a single book level. The second field is the size,
# and the two forks spell it differently on disk: crypto ``amount``, equity
# ``size``. Both spellings are kept because neither lake gets rewritten.
_CRYPTO_LEVEL_STRUCT = pl.Struct({"price": pl.Float64, "amount": pl.Float64})
_EQUITY_LEVEL_STRUCT = pl.Struct({"price": pl.Float64, "size": pl.Float64})

# Per-channel extra columns, crypto union.
_CRYPTO_CHANNEL_EXTRA: dict[str, dict[str, Any]] = {
    "trade": {
        "id": pl.Utf8,
        "price": pl.Float64,
        "amount": pl.Float64,
        "side": pl.Utf8,
        "liquidation": pl.Utf8,
        "l1_gas_fee": pl.Float64,
        "l2_gas_fee": pl.Float64,
        "gas_price": pl.Float64,
        "sender": pl.Utf8,
        "is_smart_wallet": pl.Boolean,
    },
    "farcaster_correlation": {
        "mentions_24h": pl.Int64,
        "dev_activity_score": pl.Float64,
        "trending_rank": pl.Int64,
    },
    "reserve_data_updated": {
        "reserve": pl.Utf8,
        "liquidity_rate": pl.Float64,
        "stable_borrow_rate": pl.Float64,
        "variable_borrow_rate": pl.Float64,
        "liquidity_index": pl.Int64,
        "variable_borrow_index": pl.Int64,
    },
    "liquidation_call": {
        "collateral_asset": pl.Utf8,
        "debt_asset": pl.Utf8,
        "user": pl.Utf8,
        "debt_to_cover": pl.Float64,
        "liquidated_collateral_amount": pl.Float64,
        "liquidator": pl.Utf8,
        "receive_a_token": pl.Boolean,
    },
    "book_snapshot": {
        "bids": pl.List(_CRYPTO_LEVEL_STRUCT),
        "asks": pl.List(_CRYPTO_LEVEL_STRUCT),
        "depth": pl.Int64,
        "sequence_id": pl.Int64,
        "is_snapshot": pl.Boolean,
    },
    "book_delta": {
        "bids": pl.List(_CRYPTO_LEVEL_STRUCT),
        "asks": pl.List(_CRYPTO_LEVEL_STRUCT),
        "seq_id": pl.Int64,
        "prev_seq_id": pl.Int64,
        "is_snapshot": pl.Boolean,
    },
    "book_ticker": {
        "bid_px": pl.Float64,
        "bid_sz": pl.Float64,
        "ask_px": pl.Float64,
        "ask_sz": pl.Float64,
        "update_id": pl.Int64,
    },
    "derivative_ticker": {
        "last_price": pl.Float64,
        "mark_price": pl.Float64,
        "index_price": pl.Float64,
        "funding_rate": pl.Float64,
        "predicted_funding_rate": pl.Float64,
        "realized_funding_rate": pl.Float64,
        "funding_timestamp": pl.Int64,
        "open_interest": pl.Float64,
    },
    "options_chain": {
        "underlying": pl.Utf8,
        "underlying_price": pl.Float64,
        "strike": pl.Float64,
        "expiry": pl.Int64,
        "opt_type": pl.Utf8,
        "mark_price": pl.Float64,
        "mark_iv": pl.Float64,
        "bid_px": pl.Float64,
        "bid_sz": pl.Float64,
        "bid_iv": pl.Float64,
        "ask_px": pl.Float64,
        "ask_sz": pl.Float64,
        "ask_iv": pl.Float64,
        "last_price": pl.Float64,
        "open_interest": pl.Float64,
        "delta": pl.Float64,
        "gamma": pl.Float64,
        "vega": pl.Float64,
        "theta": pl.Float64,
        "rho": pl.Float64,
    },
    "funding": {
        "funding_rate": pl.Float64,
        "funding_timestamp": pl.Int64,
        "predicted_funding_rate": pl.Float64,
        "realized_funding_rate": pl.Float64,
        "interval_hours": pl.Int64,
    },
    "open_interest": {
        "open_interest": pl.Float64,
        "open_interest_value": pl.Float64,
    },
    "liquidation": {
        "price": pl.Float64,
        "amount": pl.Float64,
        "side": pl.Utf8,
        "id": pl.Utf8,
    },
    "ohlcv": {
        "interval": pl.Utf8,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "buy_volume": pl.Float64,
        "sell_volume": pl.Float64,
        "num_trades": pl.Int64,
    },
    # The three below are in the legacy crypto union but were missing from this
    # table, so every one of their fields was silently filtered out on the way
    # to Parquet and the parts held nothing but the common header. The equity
    # fork's sink had all three; the shapes here are the same minus
    # ``exchange_ts``, which crypto already carries as a common field.
    "limit_order_fill": {
        "tx_hash": pl.Utf8,
        "log_index": pl.Int64,
        "protocol": pl.Utf8,
        "maker": pl.Utf8,
        "taker": pl.Utf8,
        "maker_token": pl.Utf8,
        "taker_token": pl.Utf8,
        "maker_amount": pl.Float64,
        "taker_amount": pl.Float64,
        "order_hash": pl.Utf8,
    },
    "balance_correction": {
        "holder_address": pl.Utf8,
        "token_address": pl.Utf8,
        "local_balance": pl.Float64,
        "onchain_balance": pl.Float64,
        "correction_amount": pl.Float64,
    },
    "por_update": {
        "feed_address": pl.Utf8,
        "token_address": pl.Utf8,
        "reserves": pl.Float64,
        "total_supply": pl.Float64,
        "backing_ratio": pl.Float64,
        "is_backed": pl.Boolean,
    },
}

# Per-channel extra columns, equity union. Ported from the equity fork's sink;
# the column names and dtypes are what legacy equity lakes already hold, so they
# are copied rather than harmonised with their crypto namesakes.
_EQUITY_CHANNEL_EXTRA: dict[str, dict[str, Any]] = {
    "trade": {
        "id": pl.Utf8,
        "price": pl.Float64,
        "size": pl.Float64,
        "conditions": pl.List(pl.Utf8),
        "tape": pl.Utf8,
        "venue": pl.Utf8,
    },
    "quote": {
        "bid_px": pl.Float64,
        "bid_sz": pl.Float64,
        "ask_px": pl.Float64,
        "ask_sz": pl.Float64,
        "is_nbbo": pl.Boolean,
        "is_consolidated": pl.Boolean,
        "conditions": pl.List(pl.Utf8),
        "tape": pl.Utf8,
    },
    "book_snapshot": {
        "bids": pl.List(_EQUITY_LEVEL_STRUCT),
        "asks": pl.List(_EQUITY_LEVEL_STRUCT),
        "depth": pl.Int64,
        "sequence_id": pl.Int64,
        "is_snapshot": pl.Boolean,
    },
    "book_delta": {
        "bids": pl.List(_EQUITY_LEVEL_STRUCT),
        "asks": pl.List(_EQUITY_LEVEL_STRUCT),
        "seq_id": pl.Int64,
        "prev_seq_id": pl.Int64,
        "is_snapshot": pl.Boolean,
    },
    "depth": {
        "bids": pl.List(_EQUITY_LEVEL_STRUCT),
        "asks": pl.List(_EQUITY_LEVEL_STRUCT),
        "reference_price": pl.Float64,
        "basis": pl.Utf8,
        "is_synthetic": pl.Boolean,
        "depth": pl.Int64,
    },
    "bar": {
        "interval": pl.Utf8,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "vwap": pl.Float64,
        "trade_count": pl.Int64,
    },
    "corp_action": {
        "ex_date": pl.Utf8,
        "type": pl.Utf8,
        "value": pl.Float64,
    },
    "fundamental": {
        "taxonomy": pl.Utf8,
        "tag": pl.Utf8,
        "unit": pl.Utf8,
        "val": pl.Float64,
        "end": pl.Utf8,
        "start": pl.Utf8,
        "fy": pl.Int64,
        "fp": pl.Utf8,
        "form": pl.Utf8,
        "filed": pl.Utf8,
        "accn": pl.Utf8,
        "frame": pl.Utf8,
    },
    "filing": {
        "accession_number": pl.Utf8,
        "form": pl.Utf8,
        "filing_date": pl.Utf8,
        "primary_document": pl.Utf8,
        "document_url": pl.Utf8,
        "report_date": pl.Utf8,
        "is_xbrl": pl.Boolean,
    },
    "ohlcv": {
        "interval": pl.Utf8,
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "vwap": pl.Float64,
        "trade_count": pl.Int64,
    },
    "index_value": {
        "value": pl.Float64,
    },
    "auction": {
        "paired_shares": pl.Float64,
        "imbalance_shares": pl.Float64,
        "imbalance_side": pl.Utf8,
        "reference_price": pl.Float64,
        "indicative_price": pl.Float64,
        "auction_type": pl.Utf8,
    },
    "trading_status": {
        "status": pl.Utf8,
        "reason": pl.Utf8,
        "limit_up_price": pl.Float64,
        "limit_down_price": pl.Float64,
        "indicator": pl.Utf8,
    },
    "instrument": {
        "name": pl.Utf8,
        "cik": pl.Utf8,
        "figi": pl.Utf8,
        "composite_figi": pl.Utf8,
        "share_class_figi": pl.Utf8,
        "cusip": pl.Utf8,
        "exchange_name": pl.Utf8,
        "security_type": pl.Utf8,
        "sic": pl.Utf8,
        "shares_outstanding": pl.Int64,
        "listing_date": pl.Utf8,
        "status": pl.Utf8,
    },
    "insider": {
        "insider_name": pl.Utf8,
        "position": pl.Utf8,
        "transaction_type": pl.Utf8,
        "transaction_date": pl.Utf8,
        "shares": pl.Float64,
        "price": pl.Float64,
        "value": pl.Float64,
        "ownership": pl.Utf8,
        "acquired_disposed": pl.Utf8,
        "insider_cik": pl.Utf8,
    },
    "holding_13f": {
        "manager_name": pl.Utf8,
        "issuer_name": pl.Utf8,
        "cusip": pl.Utf8,
        "value": pl.Float64,
        "shares": pl.Float64,
        "shares_type": pl.Utf8,
        "discretion": pl.Utf8,
        "voting_sole": pl.Float64,
        "voting_shared": pl.Float64,
        "voting_none": pl.Float64,
        "report_date": pl.Utf8,
        "accession_number": pl.Utf8,
        "manager_cik": pl.Utf8,
    },
    "short_interest": {
        "settlement_date": pl.Utf8,
        "short_interest": pl.Float64,
        "prev_short_interest": pl.Float64,
        "days_to_cover": pl.Float64,
        "change_pct": pl.Float64,
    },
    "short_volume": {
        "date_val": pl.Utf8,
        "short_volume": pl.Float64,
        "short_exempt_volume": pl.Float64,
        "total_volume": pl.Float64,
    },
    "option_quote": {
        "underlying": pl.Utf8,
        "expiry": pl.Utf8,
        "strike": pl.Float64,
        "type": pl.Utf8,
        "bid": pl.Float64,
        "ask": pl.Float64,
        "last": pl.Float64,
        "volume": pl.Float64,
        "open_interest": pl.Float64,
        "implied_volatility": pl.Float64,
        "delta": pl.Float64,
        "gamma": pl.Float64,
        "vega": pl.Float64,
        "theta": pl.Float64,
        "rho": pl.Float64,
    },
    "macro_series": {
        "date_val": pl.Utf8,
        "value": pl.Float64,
        "realtime_start": pl.Utf8,
        "realtime_end": pl.Utf8,
    },
    "reserve_data_updated": {
        "exchange_ts": pl.Int64,
        "reserve": pl.Utf8,
        "liquidity_rate": pl.Float64,
        "stable_borrow_rate": pl.Float64,
        "variable_borrow_rate": pl.Float64,
        "liquidity_index": pl.Int64,
        "variable_borrow_index": pl.Int64,
    },
    "liquidation_call": {
        "exchange_ts": pl.Int64,
        "collateral_asset": pl.Utf8,
        "debt_asset": pl.Utf8,
        "user": pl.Utf8,
        "debt_to_cover": pl.Float64,
        "liquidated_collateral_amount": pl.Float64,
        "liquidator": pl.Utf8,
        "receive_a_token": pl.Boolean,
    },
    "limit_order_fill": {
        "exchange_ts": pl.Int64,
        "tx_hash": pl.Utf8,
        "log_index": pl.Int64,
        "protocol": pl.Utf8,
        "maker": pl.Utf8,
        "taker": pl.Utf8,
        "maker_token": pl.Utf8,
        "taker_token": pl.Utf8,
        "maker_amount": pl.Float64,
        "taker_amount": pl.Float64,
        "order_hash": pl.Utf8,
    },
    "balance_correction": {
        "exchange_ts": pl.Int64,
        "holder_address": pl.Utf8,
        "token_address": pl.Utf8,
        "local_balance": pl.Float64,
        "onchain_balance": pl.Float64,
        "correction_amount": pl.Float64,
    },
    "por_update": {
        "exchange_ts": pl.Int64,
        "feed_address": pl.Utf8,
        "token_address": pl.Utf8,
        "reserves": pl.Float64,
        "total_supply": pl.Float64,
        "backing_ratio": pl.Float64,
        "is_backed": pl.Boolean,
    },
}

# ---------------------------------------------------------------------------
# The canonical family, derived rather than declared
# ---------------------------------------------------------------------------
# The two tables above are frozen literals because the files they describe are
# already on disk and the migration renames rather than rewrites them: a column
# copied wrong there is a lake that no longer reads as one schema. The canonical
# lake has no such files yet, and its structs are the thing this whole merge is
# still moving surfaces onto. So this family's schema is read off the structs.
# It is also what makes widening this family a one-place change: a trailing nullable record
# field becomes a nullable column here in the same commit, by construction (see the module
# docstring's "Nullable widening of the canonical family" ruling).
#
# The point is not brevity. This sink *refuses* a row carrying a field its
# channel schema does not declare, which means a field added to a record and
# forgotten here would not be dropped quietly — it would take the channel's
# whole write down at runtime. A second hand-written copy of 30 structs is a
# second place for that to happen; there is no second copy.
_CANONICAL_LEVEL_STRUCT = pl.Struct({"price": pl.Float64, "amount": pl.Float64})

_SCALAR_DTYPES: dict[type, Any] = {
    str: pl.Utf8,
    int: pl.Int64,
    float: pl.Float64,
    bool: pl.Boolean,
}


def _polars_dtype(annotation: Any) -> Any:
    """Return the Polars dtype that stores one record field.

    Optionality carries no information here — every Parquet column is nullable —
    so ``X | None`` and ``X`` resolve to the same dtype.

    Raises:
        TypeError: for an annotation with no mapping. A record field whose type
            this does not understand must stop the build rather than be assigned
            a plausible dtype; guessing ``Utf8`` for an unrecognised field is how
            a structured value becomes a string in the file and nothing says so.
    """
    if typing.get_origin(annotation) in (types.UnionType, typing.Union):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            annotation = members[0]

    if isinstance(annotation, type):
        # Enum before the scalar table: a StrEnum *is* a str subclass, and its
        # members reach the row as their values via ``rows._convert_value``.
        if issubclass(annotation, enum.Enum):
            return pl.Utf8
        dtype = _SCALAR_DTYPES.get(annotation)
        if dtype is not None:
            return dtype

    if typing.get_origin(annotation) is list:
        (inner,) = typing.get_args(annotation)
        # ``Level`` is ``tuple[float, float]``; the struct field names are the
        # crypto lake's spelling, which is what every reader in the tree expects.
        # The arity and element types are checked rather than assumed: this
        # branch is the one place the function names columns the annotation does
        # not, so an unchecked ``tuple[...]`` would quietly file a three-element
        # or a ``tuple[str, float]`` field as a price/amount pair. Everything
        # else here raises for an annotation it does not recognise; so does this.
        if typing.get_origin(inner) is tuple:
            if typing.get_args(inner) != (float, float):
                raise TypeError(
                    f"only tuple[float, float] is stored as a "
                    f"{{price, amount}} book level; {annotation!r} declares "
                    f"{typing.get_args(inner)!r}"
                )
            return pl.List(_CANONICAL_LEVEL_STRUCT)
        return pl.List(_polars_dtype(inner))

    raise TypeError(f"no Parquet dtype for canonical record field annotation {annotation!r}")


# Columns the canonical ``depth`` file carries that are not record fields.
#
# ``DepthProfile.is_synthetic`` is a Python property, and ``basis`` is spelled
# ``prov_basis`` on the canonical header — so ``msgspec.structs.asdict`` emits
# neither name and ``to_row`` writes no such key. Legacy equity ``channel=depth``
# files have both columns, and ``migrate_lake`` renames directories rather than
# rewriting files, so those columns are on disk forever. A canonical file without
# them does not make ``WHERE is_synthetic`` fail — it makes it match nothing, so
# the query answers with the pre-merge half of the lake as if it were all of it.
#
# Deriving them from the provenance tail at write time is what the design said
# and what nothing implemented; ``_derive_depth_columns`` is that derivation.
_DEPTH_DERIVED_COLUMNS: dict[str, Any] = {"basis": pl.Utf8, "is_synthetic": pl.Boolean}


def _derive_depth_columns(row: dict[str, Any]) -> None:
    """Write ``basis`` and ``is_synthetic`` onto one canonical ``depth`` row, in place.

    The predicate is :meth:`DepthProfile.is_synthetic`'s, restated against the flattened
    row: ``prov`` reaches here as its string value, since ``to_row`` converts enums. It is
    rebuilt through :class:`Provenance` rather than string-compared so an unrecognised
    level stops the write instead of quietly persisting ``is_synthetic = False``.

    Raises:
        ValueError: if the row carries no provenance tail. Every canonical record has one
            by construction; a depth row without it cannot say what the column means, and
            defaulting the column is how a synthetic profile passes for a real book.
    """
    try:
        prov, basis = row["prov"], row["prov_basis"]
    except KeyError as exc:
        raise ValueError(
            f"canonical depth row carries no {exc.args[0]!r}; the persisted "
            f"'basis'/'is_synthetic' columns are derived from the provenance tail"
        ) from exc
    row["basis"] = basis
    row["is_synthetic"] = Provenance(prov) is Provenance.SYNTHETIC


def _build_canonical_schema() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return ``(common fields, per-channel extras)`` for the canonical union."""
    header = msgspec.structs.fields(_Header)
    header_names = {f.name for f in header}
    common: dict[str, Any] = {
        f.name: _polars_dtype(f.type) for f in header if f.name not in _PATH_ONLY_COLUMNS
    }
    common |= {"channel": pl.Utf8, "date": pl.Utf8, "bucket": pl.Int32}

    extra: dict[str, dict[str, Any]] = {}
    for struct in typing.get_args(Record):
        tag = struct.__struct_config__.tag
        extra[tag] = {
            # ``to_row`` moves a record field that collides with a partition
            # column aside — ``ShortVolume.date`` is a settlement day, the
            # partition ``date`` is the capture day — so the column it writes is
            # the moved name, and that is the name the schema has to declare.
            f"{f.name}_val" if f.name in _DERIVED_PARTITION_COLS else f.name: _polars_dtype(f.type)
            for f in msgspec.structs.fields(struct)
            if f.name not in header_names
        }
    extra[Channel.DEPTH.value] |= _DEPTH_DERIVED_COLUMNS
    return common, extra


_CANONICAL_COMMON_FIELDS, _CANONICAL_CHANNEL_EXTRA = _build_canonical_schema()

_COMMON_FIELDS_BY_FAMILY: dict[str, dict[str, Any]] = {
    FAMILY_CRYPTO: _CRYPTO_COMMON_FIELDS,
    FAMILY_EQUITY: _EQUITY_COMMON_FIELDS,
    FAMILY_CANONICAL: _CANONICAL_COMMON_FIELDS,
}

_CHANNEL_EXTRA_BY_FAMILY: dict[str, dict[str, dict[str, Any]]] = {
    FAMILY_CRYPTO: _CRYPTO_CHANNEL_EXTRA,
    FAMILY_EQUITY: _EQUITY_CHANNEL_EXTRA,
    FAMILY_CANONICAL: _CANONICAL_CHANNEL_EXTRA,
}


def _row_family(row: dict[str, Any]) -> str:
    """Return which record union a flattened row came from.

    Raises:
        ValueError: if the row names its origin under none of the known
            markers. Picking a family by guessing would write the row against
            the wrong schema and drop every field the guess did not cover.
    """
    for marker, family in _FAMILY_MARKERS:
        if marker in row:
            return family
    raise ValueError(
        f"row names its family under none of {[m for m, _ in _FAMILY_MARKERS]}; "
        f"it cannot be matched to a Parquet schema (channel={row.get('channel')!r})"
    )


def _channel_schema(channel: str, family: str = FAMILY_CRYPTO) -> dict[str, Any]:
    """Return the full Polars schema for the given channel within a family.

    ``family`` defaults to crypto so callers that predate the merge keep
    resolving the schema they always did.

    Raises:
        ValueError: if the family has no schema table, or the family has one and
            the channel is not in it. Falling back to another family's table
            would drop every field the two do not share — a canonical record
            written through the crypto table loses ``asset_class`` and the whole
            ``prov*`` tail without a word.
    """
    common = _COMMON_FIELDS_BY_FAMILY.get(family)
    extras = _CHANNEL_EXTRA_BY_FAMILY.get(family)
    if common is None or extras is None:
        raise ValueError(
            f"no Parquet schema for record family {family!r} (channel={channel!r}); "
            f"known families are {sorted(_COMMON_FIELDS_BY_FAMILY)}"
        )
    return {**common, **extras.get(channel, {})}


def _sanitize_path_segment(value: str, *, field: str) -> str:
    """Sanitize a hive-partition path segment before joining under data_dir.

    Rejects empty values, path separators, null bytes, and ``.`` / ``..`` so a
    hostile exchange/channel/date cannot escape the lake root via path
    traversal when building ``part_dir``.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {field} path segment: {value!r}")
    if "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"Invalid {field} path segment (contains separator or null): {value!r}")
    if value in (".", ".."):
        raise ValueError(f"Invalid {field} path segment: {value!r}")
    return value


def _level_columns(schema: dict[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    """Return ``(column, struct field names)`` for every book-level column.

    Read off the schema rather than a hard-coded channel list, so the sink
    cannot coerce a level to ``{"price", "amount"}`` while declaring
    ``{"price", "size"}`` (or miss a channel — the equity fork's copy listed
    ``book_snapshot``/``book_delta``/``depth`` and the crypto copy listed only
    the first two).
    """
    out: list[tuple[str, tuple[str, ...]]] = []
    for name, dtype in schema.items():
        inner = getattr(dtype, "inner", None)
        if isinstance(dtype, pl.List) and isinstance(inner, pl.Struct):
            out.append((name, tuple(f.name for f in inner.fields)))
    return out


def _coerce_levels(rows: list[dict[str, Any]], field: str, keys: tuple[str, ...]) -> None:
    """Convert list-of-tuples book levels to list-of-dicts in-place.

    Polars ``pl.List(pl.Struct(...))`` requires dicts, not tuples. ``keys`` is
    the struct's field names in order, taken from the schema about to be
    declared — crypto lakes spell a level ``(price, amount)`` and equity lakes
    ``(price, size)``.

    Idempotent: already-coerced dict levels are left unchanged so a retry
    after a failed write (which may have partially coerced rows) is safe.
    ``strict=True`` refuses a level whose arity does not match the struct
    instead of silently truncating or padding it.
    """
    for row in rows:
        levels = row.get(field)
        if levels and not isinstance(levels[0], dict):
            row[field] = [dict(zip(keys, level, strict=True)) for level in levels]


def _group_key(row: dict[str, Any]) -> tuple[str, str, int]:
    """Return the ``(source, date, bucket)`` partition a row belongs to.

    One place owns the partition identity so the flush path and its re-buffer
    path cannot compute it two different ways.

    Raises:
        KeyError: naming the missing partition column, rather than a bare
            ``KeyError('source')`` from a tuple literal buried in the flush.
    """
    try:
        return (row["source"], row["date"], row["bucket"])
    except KeyError as exc:
        missing = exc.args[0]
        raise KeyError(
            f"row is missing partition column {missing!r}; it cannot be assigned "
            f"to a partition (channel={row.get('channel')!r})"
        ) from exc


# ---------------------------------------------------------------------------
# ParquetSink
# ---------------------------------------------------------------------------


class ParquetSink(Sink):
    """Buffered async sink that writes hive-partitioned Parquet files.

    Args:
        data_dir: Root directory for the data lake.
        max_buffer_rows: Flush a channel buffer when it reaches this many rows.
        flush_interval_seconds: Maximum seconds before a time-triggered flush.
            Pass a large number (e.g. 9999) to disable time-based flushing in
            tests.
    """

    def __init__(
        self,
        data_dir: Path | str,
        max_buffer_rows: int = 100_000,
        flush_interval_seconds: float = 5.0,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._max_buffer_rows = max_buffer_rows
        self._flush_interval = flush_interval_seconds
        # channel → list[row dicts]
        self._buffers: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        self._last_flush: float = time.monotonic()

    # ------------------------------------------------------------------
    # Sink interface
    # ------------------------------------------------------------------

    async def put(self, record: Record) -> None:
        """Buffer a record; auto-flush if thresholds are exceeded."""
        row = to_row(record)
        channel: str = row["channel"]
        self._buffers[channel].append(row)

        # Flush on row-count threshold
        if len(self._buffers[channel]) >= self._max_buffer_rows:
            await self._flush_channel(channel)
            self._last_flush = time.monotonic()
            return

        # Flush on time threshold (checked lazily on the next put)
        elapsed = time.monotonic() - self._last_flush
        if elapsed >= self._flush_interval:
            await self.flush()

    async def flush(self) -> None:
        """Flush all buffered channels to Parquet."""
        channels = list(self._buffers.keys())
        for channel in channels:
            if self._buffers[channel]:
                await self._flush_channel(channel)
        self._last_flush = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _flush_channel(self, channel: str) -> None:
        """Write a channel's buffer to one or more Parquet files.

        Rows are detached for the write attempt.  Each partition group is
        tracked in ``written`` after a successful write.  On any non-success
        path (write failure, ``CancelledError``, or other ``BaseException``)
        only rows whose partition key is **not** in ``written`` are
        re-buffered (prepended ahead of any rows concurrent ``put()`` may
        have added while the write was in flight).  Already-durable
        partitions are not re-queued, avoiding duplicates on retry.
        """
        # Detach current buffer so concurrent put() appends to a fresh list
        # via defaultdict, while we own the snapshot for this flush.
        rows = self._buffers.pop(channel, [])
        if not rows:
            return

        # Use try/finally + success flag so unwritten rows are re-buffered on
        # ANY non-success path, including CancelledError / BaseException
        # (which ``except Exception`` does not catch).
        ok = False
        written: set[tuple[str, str, int]] = set()
        # Partition key per row, kept so the re-buffer path never re-derives it.
        keyed: list[tuple[tuple[str, str, int], dict[str, Any]]] = []
        try:
            # Group rows by (source, date, bucket) — each group → one file
            groups: defaultdict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                key = _group_key(row)
                keyed.append((key, row))
                groups[key].append(row)

            for (source, date, bucket), group_rows in groups.items():
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self._write_parquet_sync,
                    channel,
                    source,
                    date,
                    bucket,
                    group_rows,
                )
                written.add((source, date, bucket))
            ok = True
        finally:
            if not ok:
                # Partial or total failure: re-buffer only partitions that
                # never completed a durable write.  Prepend remaining ahead of
                # concurrent put() rows so order is remaining + pending.
                #
                # Re-derive nothing here. Recomputing the key would re-raise the
                # very KeyError that aborted grouping, from inside ``finally``,
                # which both replaces the original exception and skips the
                # assignment below — one malformed row would silently take the
                # whole channel buffer with it. If keying did not finish, no
                # group can have been written, so every row is still owed a retry.
                remaining = (
                    [r for key, r in keyed if key not in written]
                    if len(keyed) == len(rows)
                    else list(rows)
                )
                pending = self._buffers.get(channel, [])
                self._buffers[channel] = remaining + pending

    def _write_parquet_sync(
        self,
        channel: str,
        source: str,
        date: str,
        bucket: int,
        rows: list[dict[str, Any]],
    ) -> None:
        """Synchronous Parquet write (runs in executor to avoid blocking the loop).

        Raises:
            ValueError: if the rows in a group do not agree on a record family,
                if the family has no schema table, or if any row carries a field
                the channel schema does not declare. The last one is the point:
                a field with nowhere to go used to be dropped on the floor here.
        """
        # Sanitize path components from record fields before joining under data_dir.
        source = _sanitize_path_segment(source, field="source")
        channel = _sanitize_path_segment(channel, field="channel")
        date = _sanitize_path_segment(date, field="date")

        data_dir = self._data_dir.resolve()
        part_dir = (
            data_dir
            / f"source={source}"
            / f"channel={channel}"
            / f"date={date}"
            / f"bucket={bucket}"
        )
        part_dir = part_dir.resolve()
        if not part_dir.is_relative_to(data_dir):
            raise ValueError(f"Partition path escapes data_dir: {part_dir} not under {data_dir}")
        part_dir.mkdir(parents=True, exist_ok=True)
        part_id = uuid.uuid4().hex
        # Temp name is not part-* so catalog / hive readers ignore in-progress writes.
        temp_path = part_dir / f"temp-part-{part_id}.parquet"
        out_path = part_dir / f"part-{part_id}.parquet"

        # A group shares one channel and one source, so it must share one
        # family; a mixed group would silently write half its rows against the
        # other fork's schema.
        families = {_row_family(row) for row in rows}
        if len(families) > 1:
            raise ValueError(
                f"partition source={source} channel={channel} mixes record families "
                f"{sorted(families)}; one Parquet file cannot hold both schemas"
            )
        family = families.pop()
        schema = _channel_schema(channel, family)

        # Only the canonical family needs this: a legacy equity depth row already
        # carries both columns as record fields, and no crypto record is a depth
        # profile at all.
        if family == FAMILY_CANONICAL and channel == Channel.DEPTH.value:
            for row in rows:
                _derive_depth_columns(row)

        # Coerce book levels (list-of-tuples → list-of-dicts) using the struct
        # field names the schema is about to declare.
        for level_col, level_keys in _level_columns(schema):
            _coerce_levels(rows, level_col, level_keys)

        # Refuse rows carrying a field the schema does not know rather than
        # dropping it. Dropping is indistinguishable from the field never
        # having been collected, which is how equity records lost ``provider``,
        # ``size``, ``tape``, ``venue`` and ``source_ts`` in silence.
        known = schema.keys() | _PATH_ONLY_COLUMNS
        unknown: set[str] = set()
        filtered_rows: list[dict[str, Any]] = []
        for row in rows:
            unknown |= row.keys() - known
            filtered_rows.append({k: row.get(k) for k in schema})
        if unknown:
            raise ValueError(
                f"channel {channel!r} schema has no column for {sorted(unknown)}; "
                f"writing would drop {'it' if len(unknown) == 1 else 'them'} silently. "
                f"Widen the schema for this channel or stop emitting the field."
            )

        # Build DataFrame with explicit schema to ensure type consistency
        df = pl.DataFrame(filtered_rows, schema=schema)

        # Crash safety: write temp in the same directory, then rename to the
        # final part-*.parquet. Same-directory rename is atomic on POSIX so
        # readers never see a truncated part file if the process dies mid-write.
        try:
            df.write_parquet(
                temp_path,
                compression="zstd",
                compression_level=5,
                row_group_size=250_000,
            )
            temp_path.replace(out_path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
