"""Strict gzip/NDJSON parsing and deterministic event-time minute reduction."""

from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from .intraday_models import (
    CONTRACTS,
    BarEnvelope,
    FileReference,
    Observation,
    file_minute_ns,
    parse_utc_ns,
)

MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024


def decode_gzip(
    raw: bytes,
    *,
    max_compressed_bytes: int = MAX_COMPRESSED_BYTES,
    max_expanded_bytes: int = MAX_EXPANDED_BYTES,
) -> bytes:
    """Bound allocation as well as output; verify trailer/CRC and reject extra members."""
    if len(raw) > max_compressed_bytes:
        raise ValueError("oversized_compressed")
    decompressor = zlib.decompressobj(31)
    expanded = bytearray()
    try:
        for offset in range(0, len(raw), 65536):
            chunk = raw[offset : offset + 65536]
            expanded.extend(decompressor.decompress(chunk, max_expanded_bytes - len(expanded) + 1))
            if len(expanded) > max_expanded_bytes or decompressor.unconsumed_tail:
                raise ValueError("oversized_expanded")
        if not decompressor.eof or decompressor.unused_data:
            raise ValueError("invalid_gzip")
    except zlib.error:
        raise ValueError("invalid_gzip") from None
    return bytes(expanded)


def json_rows(expanded: bytes) -> Iterable[tuple[int, dict[str, Any]]]:
    """Validate every row; omit only the observed instrument-less pretrade shapes."""
    try:
        for number, line in enumerate(expanded.decode("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line, parse_float=Decimal, parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise ValueError("invalid_json_row")
            if value.get("messageId") == "pretrade":
                if _known_pretrade_record(value):
                    continue
                raise ValueError(f"unsupported_pretrade_schema_row_{number}")
            _validate_structure(value, number)
            yield number, value
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid_json") from None


def _known_pretrade_record(row: dict[str, Any]) -> bool:
    """Exclude four publisher-labelled shapes evidenced in the 2026-11-13 files.

    These have no instrument ID. They cannot establish a target-contract trade
    or complete coverage; untouched records remain in the archived raw gzip.
    An explicit discriminator, exact key sets and strict values prevent this
    exception from accepting arbitrary error objects or malformed native trades.
    """
    if row.get("messageId") != "pretrade":
        return False
    fields = set(row)
    if fields == {"messageId", "sourceName"}:
        source_name = row["sourceName"]
        return isinstance(source_name, str) and source_name == "EUR"
    base = {"messageId", "tradingDateAndTime", "transactionIdentificationCode"}
    with_quantity = base | {"quantity", "notionalAmount"}
    with_price = with_quantity | {"price"}
    if fields not in (base, with_quantity, with_price):
        return False
    numeric = fields - base
    for field in numeric:
        if type(row[field]) not in (int, Decimal):
            return False
        value = _decimal(row[field])
        if value is None or value < 0:
            return False
    if row["tradingDateAndTime"] is None:
        return (fields == with_price and row["transactionIdentificationCode"] == ""
                and all(row[field] == 0 for field in numeric))
    event_time, identity = row["tradingDateAndTime"], row["transactionIdentificationCode"]
    if not isinstance(event_time, str) or not isinstance(identity, str) or not identity:
        return False
    parse_utc_ns(event_time)
    return True


def _validate_structure(row: dict[str, Any], number: int) -> None:
    """Unknown contracts still must be native trade-shaped records, not errors."""
    required = (
        "instrumentIdentificationCode",
        "tradingDateAndTime",
        "publicationDateAndTime",
        "venueOfExecution",
        "transactionIdentificationCode",
        "flags",
        "tradingSystem",
    )
    if any(not isinstance(row.get(field), str) for field in required):
        raise ValueError(f"unsupported_schema_row_{number}")
    if any(not row[field] for field in required if field != "flags"):
        raise ValueError(f"unsupported_schema_row_{number}")
    parse_utc_ns(row["tradingDateAndTime"])
    parse_utc_ns(row["publicationDateAndTime"])


def _reject_constant(value: str) -> None:
    raise ValueError("invalid_json_constant")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError("unsupported_decimal")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("unsupported_decimal") from None
    if not result.is_finite() or len(result.as_tuple().digits) > 30 or abs(result.adjusted()) > 30:
        raise ValueError("unsupported_decimal_range")
    return result


def parse_file(file_name: str, raw_gzip: bytes, *, captured_ns: int) -> tuple[Observation, ...]:
    """Keep unknown instruments in raw gzip; index only the two verified contracts."""
    file_minute_ns(file_name)
    file_sha = hashlib.sha256(raw_gzip).hexdigest()
    observations = []
    for number, row in json_rows(decode_gzip(raw_gzip)):
        isin = row.get("instrumentIdentificationCode")
        if not isinstance(isin, str) or isin not in CONTRACTS:
            continue
        try:
            event_ns = parse_utc_ns(row["tradingDateAndTime"])
            publication_ns = parse_utc_ns(row["publicationDateAndTime"])
            venue, txid = row["venueOfExecution"], row["transactionIdentificationCode"]
            flags, system = row["flags"], row["tradingSystem"]
            if not all(isinstance(s, str) for s in (venue, txid, flags, system)) or not txid:
                raise ValueError("unsupported_identity")
            price, quantity = _decimal(row.get("price")), _decimal(row.get("quantity"))
            notation = row.get("priceNotation")
            if not isinstance(notation, int) or isinstance(notation, bool):
                notation = None
            reasons = []
            if price is None:
                reasons.append("missing_price")
            if quantity is None or quantity <= 0:
                reasons.append("invalid_quantity")
            if flags:
                reasons.append("unknown_flags")
            if system != "CLOB":
                reasons.append("unsupported_trading_system")
            if venue != "XEUR":
                reasons.append("unsupported_venue")
            if notation != CONTRACTS[isin].price_notation or row.get("priceCurrency") != "EUR":
                reasons.append("unknown_price_unit")
            payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
            observations.append(
                Observation(
                    isin,
                    venue,
                    txid,
                    event_ns,
                    publication_ns,
                    captured_ns,
                    price,
                    quantity,
                    notation,
                    str(row.get("priceCurrency", "")),
                    system,
                    flags,
                    file_name,
                    file_sha,
                    number,
                    hashlib.sha256(payload.encode()).hexdigest(),
                    tuple(reasons),
                )
            )
        except (KeyError, TypeError, AttributeError):
            raise ValueError(f"unsupported_schema_row_{number}") from None
    return tuple(observations)


def reduce_minute(
    isin: str,
    minute_start_ns: int,
    observations: Iterable[Observation],
    *,
    ambiguous_keys: frozenset[tuple[str, str, str]] = frozenset(),
    revision: int = 1,
) -> BarEnvelope:
    """Tie breaks are event/publication/file/row, not a claimed exchange sequence."""
    if isin not in CONTRACTS:
        raise ValueError("unsupported contract")
    rows = sorted(
        observations,
        key=lambda r: (
            r.event_time_ns,
            r.publication_time_ns,
            r.file_name,
            r.row_number,
        ),
    )
    if not rows or any(r.isin != isin or r.minute_start_ns != minute_start_ns for r in rows):
        raise ValueError("observations do not belong to requested minute")
    reasons: set[str] = set()
    eligible = []
    # Detect intra-minute conflicts even when called outside the persistent store.
    hashes: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        hashes.setdefault(row.trade_key, set()).add(row.payload_sha256)
    conflicts = ambiguous_keys | frozenset(k for k, values in hashes.items() if len(values) > 1)
    # A key whose every copy carries the same payload is one trade the publisher stated more
    # than once (in two files, or twice in one): it counts once, the first copy in the sort
    # order above. Copies that differ are the conflicts above and count not at all.
    counted: set[tuple[str, str, str]] = set()
    repeats = 0
    for row in rows:
        reasons.update(row.quality_reasons)
        if row.trade_key in conflicts:
            reasons.add("ambiguous_trade_key")
        elif row.trade_key in counted:
            repeats += 1
        elif not row.quality_reasons:
            counted.add(row.trade_key)
            eligible.append(row)
    # Precision covers parser-supported 30 digit inputs and large file sums.
    with localcontext() as context:
        context.prec = 256
        volume = sum((r.quantity for r in eligible if r.quantity is not None), Decimal(0))
        weighted = sum(
            (
                r.price * r.quantity
                for r in eligible
                if r.price is not None and r.quantity is not None
            ),
            Decimal(0),
        )
        with localcontext() as vwap_context:
            vwap_context.prec = 28
            vwap = weighted / volume if volume else None
    prices = [r.price for r in eligible if r.price is not None]
    return BarEnvelope(
        isin,
        minute_start_ns,
        prices[0] if prices else None,
        max(prices) if prices else None,
        min(prices) if prices else None,
        prices[-1] if prices else None,
        volume,
        len(eligible),
        vwap,
        revision,
        "incomplete" if reasons else "observed",
        tuple(sorted(reasons)),
        len(rows) - len(eligible) - repeats,
        tuple(sorted({FileReference(r.file_name, r.file_sha256) for r in rows})),
        max(r.event_time_ns for r in rows),
        max(r.publication_time_ns for r in rows),
        max(r.captured_ns for r in rows),
        CONTRACTS[isin].price_unit,
    )
