import calendar
import logging
import re
import time as _time
from collections.abc import Iterable
from typing import Any

from crocodile.core.schema.enums import AssetClass, OptType, Side
from crocodile.core.schema.records import (
    BookDelta,
    BookSnapshot,
    DerivativeTicker,
    Funding,
    Liquidation,
    OptionsChain,
    Record,
    Trade,
)
from crocodile.core.util.time import ms_to_ns
from crocodile.crypto.instruments.registry import InstrumentRegistry

from .book import validate_levels

EXCHANGE = "deribit"

log = logging.getLogger(__name__)

# Month abbreviation -> numeric string used in option symbol parsing
_MONTH_MAP = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}

# Real Deribit option date token: D{1,2}MMMYY (1-or-2-digit day + 3-char month +
# 2-digit year), e.g. "8JUN26", "28JUN26", "30JUN25".
_OPT_DATE_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")


def _levels(rows: list[list[Any]]) -> list[tuple[float, float]]:
    return validate_levels(rows)


def _side(direction: str) -> Side:
    return Side.BUY if direction == "buy" else Side.SELL if direction == "sell" else Side.UNKNOWN


def _parse_option_symbol(sym: str) -> tuple[str, float, int, OptType]:
    """Parse Deribit option symbol BASE-DDMMM-STRIKE-C|P.

    Returns (underlying, strike, expiry_ns, opt_type).
    expiry_ns is a best-effort nanosecond timestamp derived from the date string;
    registry values are preferred when available.
    """
    parts = sym.split("-")
    if len(parts) < 4:
        raise ValueError(f"deribit: option symbol has < 4 parts: {sym!r}")
    # e.g. ["BTC", "30JUN", "50000", "C"]
    underlying = parts[0]
    date_str = parts[1]  # e.g. "30JUN"
    strike = float(parts[2])
    opt_type = OptType.CALL if parts[3] == "C" else OptType.PUT

    # Preferred: the real Deribit token D{1,2}MMMYY with an EXPLICIT 2-digit year
    # (e.g. "8JUN26", "28JUN26"). The legacy slice below mis-reads single-digit
    # days ("8JUN26" -> "8J") and ignores the year, so try the exact form first.
    m = _OPT_DATE_RE.match(date_str.upper())
    if m:
        day_i = int(m.group(1))
        month_x = _MONTH_MAP.get(m.group(2), "01")
        year_x = 2000 + int(m.group(3))
        struct_x = _time.strptime(f"{day_i:02d} {month_x} {year_x}", "%d %m %Y")
        return underlying, strike, int(calendar.timegm(struct_x)) * 1_000_000_000, opt_type

    # Legacy fall-through (DDMMM without a year, or numeric forms): best-effort guess.
    # Parse date: DD + MMM (3-char month abbreviation)
    day = date_str[:2]
    mon_abbr = date_str[2:5].upper()
    month = _MONTH_MAP.get(mon_abbr)
    if month is None:
        # Fail loud rather than silently defaulting to January (which would emit
        # an OptionsChain with a wrong expiry). Callers catch ValueError and skip
        # the malformed symbol with a warning — consistent with the < 4-parts guard.
        raise ValueError(
            f"deribit: invalid month abbreviation {mon_abbr!r} in option symbol {sym!r}"
        )
    # Use current year as best guess; advance by 1 if the resolved date is already in the past
    # (Deribit options are always future-expiring at subscription time).
    # Registry values are preferred and will override this.
    current_year = _time.gmtime().tm_year
    year = str(current_year)
    struct = _time.strptime(f"{day} {month} {year}", "%d %m %Y")
    expiry_epoch = int(calendar.timegm(struct))
    # If the resolved date has already passed, assume next year
    if expiry_epoch < _time.time():
        year = str(current_year + 1)
        struct = _time.strptime(f"{day} {month} {year}", "%d %m %Y")
        expiry_epoch = int(calendar.timegm(struct))
    expiry_ns = expiry_epoch * 1_000_000_000
    return underlying, strike, expiry_ns, opt_type


def normalize_message(
    msg: dict[str, Any], local_ts: int, registry: InstrumentRegistry | None = None
) -> Iterable[Record]:
    params: dict[str, Any] = msg.get("params") or {}
    channel: str = params.get("channel", "")
    data: Any = params.get("data")
    if channel.startswith("trades."):
        for t in data or []:
            sym = t["instrument_name"]
            side = _side(t["direction"])
            yield Trade(
                source=EXCHANGE,
                symbol=f"{EXCHANGE}:{sym}",
                symbol_raw=sym,
                source_ts=ms_to_ns(t["timestamp"]),
                local_ts=local_ts,
                asset_class=AssetClass.CRYPTO,
                id=str(t["trade_id"]),
                price=float(t["price"]),
                amount=float(t["amount"]),
                side=side,
                liquidation=t.get("liquidation"),
            )
            if t.get("liquidation"):
                yield Liquidation(
                    source=EXCHANGE,
                    symbol=f"{EXCHANGE}:{sym}",
                    symbol_raw=sym,
                    source_ts=ms_to_ns(t["timestamp"]),
                    local_ts=local_ts,
                    asset_class=AssetClass.CRYPTO,
                    price=float(t["price"]),
                    amount=float(t["amount"]),
                    side=side,
                    id=str(t["trade_id"]),
                )
    elif channel.startswith("book."):
        d: dict[str, Any] = data or {}
        sym = d["instrument_name"]
        common: dict[str, Any] = dict(
            source=EXCHANGE,
            symbol=f"{EXCHANGE}:{sym}",
            symbol_raw=sym,
            source_ts=ms_to_ns(d["timestamp"]),
            local_ts=local_ts,
            asset_class=AssetClass.CRYPTO,
            bids=_levels(d.get("bids", [])),
            asks=_levels(d.get("asks", [])),
        )
        if d.get("type") == "snapshot":
            yield BookSnapshot(
                **common,
                depth=len(d.get("bids", [])) + len(d.get("asks", [])),
                sequence_id=d.get("change_id"),
                is_snapshot=True,
            )
        else:
            yield BookDelta(
                **common,
                seq_id=d.get("change_id"),
                prev_seq_id=d.get("prev_change_id"),
                is_snapshot=False,
            )
    elif channel.startswith("ticker."):
        td: dict[str, Any] = data or {}
        sym = td["instrument_name"]
        exchange_ts = ms_to_ns(td["timestamp"])
        symbol_canonical = f"{EXCHANGE}:{sym}"

        # Determine if option or perp/future by presence of greeks or mark_iv
        is_option = td.get("greeks") is not None or td.get("mark_iv") is not None

        if is_option:
            # Prefer the registry, fall back to the symbol for whatever it does not hold,
            # and skip when neither answers. The opt_type arm already worked this way; the
            # other three fell through to `strike=0.0` / `expiry=0` at the constructor, so
            # a registry row with strike=None wrote a zero strike and a 1970 expiry as
            # venue-reported facts. WHERE expiry > now() then dropped a live contract, and
            # log(strike / forward) on a vol-surface fit went to -inf. This function
            # already states the rule 130 lines up — "fail loud rather than silently
            # defaulting to January" — and then stopped applying it.
            inst = registry.get_raw(EXCHANGE, sym) if registry is not None else None
            strike = inst.strike if inst is not None else None
            expiry = inst.expiry if inst is not None else None
            underlying = inst.base if inst is not None else None
            opt_type = (
                OptType(inst.opt_type) if inst is not None and inst.opt_type is not None else None
            )
            if strike is None or expiry is None or underlying is None or opt_type is None:
                try:
                    from_sym = _parse_option_symbol(sym)
                except (ValueError, IndexError):
                    log.warning(
                        "deribit: cannot resolve option metadata for %r from the registry or "
                        "the symbol — skipping",
                        sym,
                    )
                    return
                underlying = underlying if underlying is not None else from_sym[0]
                strike = strike if strike is not None else from_sym[1]
                expiry = expiry if expiry is not None else from_sym[2]
                opt_type = opt_type if opt_type is not None else from_sym[3]

            # IV fields: Deribit sends percentage (e.g. 65.0 = 65%); convert to decimal fraction
            def _iv(val: float | None) -> float | None:
                return val / 100.0 if val is not None else None

            greeks: dict[str, Any] = td.get("greeks") or {}
            yield OptionsChain(
                source=EXCHANGE,
                symbol=symbol_canonical,
                symbol_raw=sym,
                source_ts=exchange_ts,
                local_ts=local_ts,
                asset_class=AssetClass.CRYPTO,
                underlying=underlying,
                underlying_price=td.get("underlying_price"),
                strike=strike,
                expiry=expiry,
                opt_type=opt_type,
                mark_price=td.get("mark_price"),
                mark_iv=_iv(td.get("mark_iv")),
                bid_px=td.get("best_bid_price"),
                bid_sz=td.get("best_bid_amount"),
                bid_iv=_iv(td.get("bid_iv")),
                ask_px=td.get("best_ask_price"),
                ask_sz=td.get("best_ask_amount"),
                ask_iv=_iv(td.get("ask_iv")),
                last_price=td.get("last_price"),
                open_interest=td.get("open_interest"),
                delta=greeks.get("delta"),
                gamma=greeks.get("gamma"),
                vega=greeks.get("vega"),
                theta=greeks.get("theta"),
                rho=greeks.get("rho"),
            )
        else:
            # Perp / future: emit DerivativeTicker + Funding.
            #
            # Deribit's ticker publishes two funding numbers and they are about different
            # spans. ``current_funding`` is the rate in force; ``funding_8h`` describes
            # the eight hours that have already happened. Both used to be written out of
            # this one payload as ``funding_rate`` and ``predicted_funding_rate``, which
            # put a realized figure into a field whose entire purpose is to say what is
            # coming — the one substitution that field cannot survive, because a consumer
            # reads it to decide whether to hold the position through the next
            # settlement. It annualizes cleanly either way, which is what made it
            # invisible: 0.0001 * 1095 is a plausible APR whichever eight hours it is
            # about. Deribit publishes no forward estimate at all, so the forward field
            # is simply not set.
            yield DerivativeTicker(
                source=EXCHANGE,
                symbol=symbol_canonical,
                symbol_raw=sym,
                source_ts=exchange_ts,
                local_ts=local_ts,
                asset_class=AssetClass.CRYPTO,
                last_price=td.get("last_price"),
                mark_price=td.get("mark_price"),
                index_price=td.get("index_price"),
                funding_rate=td.get("current_funding"),
                realized_funding_rate=td.get("funding_8h"),
                open_interest=td.get("open_interest"),
            )
            # canonical: current_funding -> funding_rate; funding_8h -> realized_funding_rate
            if td.get("current_funding") is not None:
                yield Funding(
                    source=EXCHANGE,
                    symbol=symbol_canonical,
                    symbol_raw=sym,
                    source_ts=exchange_ts,
                    local_ts=local_ts,
                    asset_class=AssetClass.CRYPTO,
                    funding_rate=float(td["current_funding"]),
                    realized_funding_rate=td.get("funding_8h"),
                    interval_hours=8,  # Deribit perpetual funding settles every 8 hours
                )
    else:
        if channel:
            log.debug("deribit: unrecognized channel %r", channel)
