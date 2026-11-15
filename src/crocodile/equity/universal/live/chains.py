"""Whole option chains from publishers' own keyless endpoints.

The scanner chain in ``assets`` states a bid, an ask and one aggregator's model for each
contract, and nothing about how much rests or trades. Three publishers state more, each in one
plain keyless request for the whole chain of an underlying:

- Cboe's delayed quotes (``cdn-api.cboe.com``): every listed option of a US share, ETF or index,
  with bid and ask sizes, the last trade, the day's volume, open interest, implied volatility
  and greeks. Cboe publishes the file as delayed; the lag measured on 2026-11-12 was fifteen
  minutes, and :data:`CBOE_DELAY_S` records it.
- Bybit (``api.bybit.com``): every option of one base coin, with sizes, bid and ask
  volatility, mark price, greeks, 24 hour volume and open interest. Premiums are in the
  settlement coin.
- Deribit (``www.deribit.com``): the book summary of one currency's options, with bid, ask,
  mark, implied volatility, volume and open interest. It states no size and no greeks, so the
  records carry none. Premiums are in the base coin, as Deribit quotes them.

Ruling: a chain is read whole or not at all. The file is one publisher's one answer; nothing is
paged, and nothing is merged across publishers.

Ruling: a field the publisher fills with its placeholder is absent, not zero. Cboe writes an
implied volatility of ``0.0`` where it computed none, and a bid of ``0.0`` with a size of
``0.0`` where nothing is bid: both are read as no value. A last trade without a trade time is
no trade.

Ruling: the contract family is part of a contract's identity. ``SPX`` and ``SPXW`` list the
same strikes on the same day, so every contract keeps the root its symbol states.

The records are ``OptionsChain`` with the ``polled_book_snapshot`` basis: each is the
publisher's own statement of the top of its book, copied.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

from crocodile.core.ratelimit import BudgetSpec, budget
from crocodile.core.schema.enums import AssetClass, OptType
from crocodile.core.schema.provenance import provenance_fields
from crocodile.core.schema.records import OptionsChain
from crocodile.equity.universal.http import (
    AiohttpFetcher,
    Fetcher,
    HttpRequest,
    HttpResponse,
    raise_if_unservable,
)
from crocodile.equity.universal.live.assets import OptionSeries
from crocodile.equity.universal.live.base import LivePayloadError, decode_json, to_float

__all__ = [
    "BYBIT_SOURCE",
    "CBOE_DELAY_S",
    "CBOE_SOURCE",
    "DERIBIT_SOURCE",
    "ChainContract",
    "PublishedChain",
    "PublishedChains",
    "parse_bybit_chain",
    "parse_cboe_chain",
    "parse_deribit_chain",
    "parse_occ_symbol",
]

CBOE_SOURCE: Final = "cboe_delayed"
CBOE_HOST: Final = "cdn-api.cboe.com"
CBOE_URL: Final = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
CBOE_DELAY_S: Final = 900
"""The lag of Cboe's delayed file, measured on 2026-11-12 (file stamp against the underlying's
last trade): fifteen minutes."""

BYBIT_SOURCE: Final = "bybit"
BYBIT_HOST: Final = "api.bybit.com"
BYBIT_URL: Final = "https://api.bybit.com/v5/market/tickers"

DERIBIT_SOURCE: Final = "deribit"
DERIBIT_HOST: Final = "www.deribit.com"
DERIBIT_URL: Final = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"

_BUDGET: Final = BudgetSpec(capacity=2.0, refill_rate=1.0)
"""One request per second per publisher, with a burst of two: a chain is one request, and the
bucket only keeps a retry loop from hammering a host."""

_BASIS: Final = "polled_book_snapshot"
"""Each record is the publisher's own top of book, copied (``live.bases``). Named here as a
literal so the provenance gate can read it."""

_BYBIT_NO_SUCH_COIN: Final = 10001
_NS_PER_DAY: Final = 86_400_000_000_000
_NS_PER_MS: Final = 1_000_000
_EPOCH: Final = date(1970, 1, 1)
_CBOE_SYMBOL: Final = re.compile(r"_?[A-Z0-9]{1,6}(\.[A-Z]{1,2})?")
_COIN: Final = re.compile(r"[A-Z0-9]{2,10}")
_OCC: Final = re.compile(r"([A-Z0-9.]{1,6}?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})")
_CRYPTO: Final = re.compile(
    r"([A-Z0-9_]{2,16})-(\d{1,2})([A-Z]{3})(\d{2})-(\d+(?:d\d+)?(?:\.\d+)?)-([CP])(?:-([A-Z]{3,5}))?"
)
_MONTHS: Final[Mapping[str, int]] = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}
_RIGHTS: Final[Mapping[str, OptType]] = {"C": OptType.CALL, "P": OptType.PUT}


@dataclass(frozen=True, slots=True, kw_only=True)
class ChainContract:
    """One contract of a published chain, with the family its symbol states."""

    root: str
    record: OptionsChain


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishedChain:
    """One publisher's whole chain of one underlying, as it answered."""

    source: str
    underlying: str
    """The underlying as it was asked for: ``AAPL``, ``_SPX``, ``BTC``."""

    captured_ns: int
    stamped_ns: int | None
    """The publisher's own stamp of its answer, when it states one."""

    underlying_price: float | None
    premium_unit: str | None
    """What a premium is quoted in: ``USD``, a settlement coin, or the base coin."""

    delay_s: int | None
    """How far behind the market the publisher says (or was measured to be); ``0`` for a live
    venue, ``None`` when unknown."""

    contracts: tuple[ChainContract, ...]

    def series(self) -> list[OptionSeries]:
        """Every series of the chain (contract family and expiry) with its size, in expiry
        and family order."""
        counts: dict[tuple[date, str], int] = {}
        for item in self.contracts:
            key = (_day_of(item.record.expiry), item.root)
            counts[key] = counts.get(key, 0) + 1
        return [
            OptionSeries(underlying=self.underlying, root=root, expiry=expiry, contracts=count)
            for (expiry, root), count in sorted(counts.items())
        ]

    def of_series(self, expiry: date, root: str) -> list[OptionsChain]:
        """The contracts of one series, in strike and right order."""
        wanted = _expiry_ns(expiry)
        picked = [
            item.record
            for item in self.contracts
            if item.root == root and item.record.expiry == wanted
        ]
        picked.sort(key=lambda record: (record.strike, record.opt_type.value))
        return picked


def _expiry_ns(day: date) -> int:
    return (day - _EPOCH).days * _NS_PER_DAY


def _day_of(expiry_ns: int) -> date:
    return date.fromordinal(_EPOCH.toordinal() + expiry_ns // _NS_PER_DAY)


def _stated(value: Any) -> float | None:
    """A published figure, or ``None`` when it is absent or negative where it cannot be."""
    number = to_float(value)
    return number if number is not None and number >= 0.0 else None


def _quote(price: Any, size: Any) -> tuple[float | None, float | None]:
    """A bid or an ask with its size. A price of zero with nothing resting is no quote."""
    px, sz = _stated(price), _stated(size)
    if px is not None and px == 0.0 and (sz is None or sz == 0.0):
        return None, None
    return px, sz


def _positive(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number > 0.0 else None


def parse_occ_symbol(text: str) -> tuple[str, date, OptType, float] | None:
    """``AAPL261113C00110000`` as (root, expiry, right, strike); ``None`` for anything else.

    The OCC spelling: the contract family, the expiry as ``YYMMDD``, ``C`` or ``P``, and the
    strike times one thousand in eight digits.
    """
    match = _OCC.fullmatch(text)
    if match is None:
        return None
    root, yy, mm, dd, right, strike = match.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return root, expiry, _RIGHTS[right], int(strike) / 1000.0


def _crypto_symbol(text: str) -> tuple[str, date, OptType, float, str | None] | None:
    """``BTC-13NOV26-98000-C-USDT`` as (base, expiry, right, strike, settlement coin)."""
    match = _CRYPTO.fullmatch(text)
    if match is None:
        return None
    base, dd, mon, yy, strike, right, settle = match.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        expiry = date(2000 + int(yy), month, int(dd))
        value = float(strike.replace("d", "."))
    except ValueError:
        return None
    if not value > 0.0:
        return None
    return base, expiry, _RIGHTS[right], value, settle


def _object(source: str, body: bytes) -> Mapping[str, Any]:
    document = decode_json(source, body)
    if not isinstance(document, dict):
        raise LivePayloadError(source, "the answer is not an object")
    return document


def _utc_stamp_ns(text: Any) -> int | None:
    """``2026-11-12 20:33:17`` (UTC, as Cboe writes it) as epoch nanoseconds."""
    if not isinstance(text, str):
        return None
    try:
        stamp = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(stamp.timestamp()) * 1_000_000_000


def _ms_ns(value: Any) -> int | None:
    number = to_float(value)
    return int(number) * _NS_PER_MS if number is not None and number > 0.0 else None


def parse_cboe_chain(body: bytes, symbol: str, *, captured_ns: int) -> PublishedChain:
    """Cboe's delayed chain of one underlying.

    A row whose symbol is not an OCC contract symbol is not a contract and is dropped.
    """
    document = _object(CBOE_SOURCE, body)
    data = document.get("data")
    rows = data.get("options") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise LivePayloadError(CBOE_SOURCE, "no options array in the answer")
    assert isinstance(data, dict)
    stamped = _utc_stamp_ns(document.get("timestamp"))
    spot = _positive(data.get("current_price"))
    tail = provenance_fields(_BASIS)
    contracts: list[ChainContract] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("option"), str):
            continue
        parsed = parse_occ_symbol(row["option"])
        if parsed is None:
            continue
        root, expiry, right, strike = parsed
        bid_px, bid_sz = _quote(row.get("bid"), row.get("bid_size"))
        ask_px, ask_sz = _quote(row.get("ask"), row.get("ask_size"))
        traded = isinstance(row.get("last_trade_time"), str)
        contracts.append(
            ChainContract(
                root=root,
                record=OptionsChain(
                    source=CBOE_SOURCE,
                    symbol=row["option"],
                    symbol_raw=row["option"],
                    local_ts=captured_ns,
                    asset_class=AssetClass.EQUITY,
                    source_ts=stamped,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    underlying=symbol,
                    underlying_price=spot,
                    strike=strike,
                    expiry=_expiry_ns(expiry),
                    opt_type=right,
                    mark_price=_stated(row.get("theo")),
                    mark_iv=_positive(row.get("iv")),
                    bid_px=bid_px,
                    bid_sz=bid_sz,
                    ask_px=ask_px,
                    ask_sz=ask_sz,
                    last_price=_positive(row.get("last_trade_price")) if traded else None,
                    volume=_stated(row.get("volume")),
                    open_interest=_stated(row.get("open_interest")),
                    delta=to_float(row.get("delta")),
                    gamma=to_float(row.get("gamma")),
                    vega=to_float(row.get("vega")),
                    theta=to_float(row.get("theta")),
                    rho=to_float(row.get("rho")),
                ),
            )
        )
    return PublishedChain(
        source=CBOE_SOURCE,
        underlying=symbol,
        captured_ns=captured_ns,
        stamped_ns=stamped,
        underlying_price=spot,
        premium_unit="USD",
        delay_s=CBOE_DELAY_S,
        contracts=tuple(contracts),
    )


def parse_bybit_chain(body: bytes, base: str, *, captured_ns: int) -> PublishedChain:
    """Bybit's option tickers of one base coin.

    The contract family is the base coin with its settlement coin (``BTC-USDT``); a symbol
    without a settlement suffix is the USDC-settled family and keeps the bare coin.
    """
    document = _object(BYBIT_SOURCE, body)
    code = document.get("retCode")
    if code == _BYBIT_NO_SUCH_COIN:
        # Bybit refuses a base coin it lists no options for: that is an empty chain.
        rows: Any = []
    elif code != 0:
        raise LivePayloadError(BYBIT_SOURCE, f"retCode {code!r}")
    else:
        result = document.get("result")
        rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise LivePayloadError(BYBIT_SOURCE, "no list in the answer")
    stamped = _ms_ns(document.get("time"))
    tail = provenance_fields(_BASIS)
    contracts: list[ChainContract] = []
    spot: float | None = None
    units: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            continue
        parsed = _crypto_symbol(row["symbol"])
        if parsed is None or parsed[0] != base:
            continue
        _, expiry, right, strike, settle = parsed
        unit = settle if settle is not None else "USDC"
        units.add(unit)
        bid_px, bid_sz = _quote(row.get("bid1Price"), row.get("bid1Size"))
        ask_px, ask_sz = _quote(row.get("ask1Price"), row.get("ask1Size"))
        underlying_price = _positive(row.get("underlyingPrice"))
        index = _positive(row.get("indexPrice"))
        spot = spot if spot is not None else index
        contracts.append(
            ChainContract(
                root=base if settle is None else f"{base}-{settle}",
                record=OptionsChain(
                    source=BYBIT_SOURCE,
                    symbol=row["symbol"],
                    symbol_raw=row["symbol"],
                    local_ts=captured_ns,
                    asset_class=AssetClass.CRYPTO,
                    source_ts=stamped,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    underlying=base,
                    underlying_price=underlying_price,
                    strike=strike,
                    expiry=_expiry_ns(expiry),
                    opt_type=right,
                    mark_price=_stated(row.get("markPrice")),
                    mark_iv=_positive(row.get("markIv")),
                    bid_px=bid_px,
                    bid_sz=bid_sz,
                    bid_iv=_positive(row.get("bid1Iv")) if bid_px is not None else None,
                    ask_px=ask_px,
                    ask_sz=ask_sz,
                    ask_iv=_positive(row.get("ask1Iv")) if ask_px is not None else None,
                    last_price=_positive(row.get("lastPrice")),
                    volume=_stated(row.get("volume24h")),
                    open_interest=_stated(row.get("openInterest")),
                    delta=to_float(row.get("delta")),
                    gamma=to_float(row.get("gamma")),
                    vega=to_float(row.get("vega")),
                    theta=to_float(row.get("theta")),
                ),
            )
        )
    return PublishedChain(
        source=BYBIT_SOURCE,
        underlying=base,
        captured_ns=captured_ns,
        stamped_ns=stamped,
        underlying_price=spot,
        premium_unit=next(iter(units)) if len(units) == 1 else None,
        delay_s=0,
        contracts=tuple(contracts),
    )


def parse_deribit_chain(body: bytes, currency: str, *, captured_ns: int) -> PublishedChain:
    """Deribit's option book summary of one currency.

    Deribit states its implied volatility in percent; the record carries the fraction, as every
    ``*_iv`` field does. It states no size and no greeks here, so the records have none.
    """
    document = _object(DERIBIT_SOURCE, body)
    rows = document.get("result")
    if not isinstance(rows, list):
        raise LivePayloadError(DERIBIT_SOURCE, "no result array in the answer")
    tail = provenance_fields(_BASIS)
    contracts: list[ChainContract] = []
    spot: float | None = None
    newest: int | None = None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("instrument_name"), str):
            continue
        parsed = _crypto_symbol(row["instrument_name"])
        if parsed is None or parsed[0] != currency:
            continue
        _, expiry, right, strike, _settle = parsed
        stamped = _ms_ns(row.get("creation_timestamp"))
        if stamped is not None and (newest is None or stamped > newest):
            newest = stamped
        spot = spot if spot is not None else _positive(row.get("estimated_delivery_price"))
        percent = _positive(row.get("mark_iv"))
        contracts.append(
            ChainContract(
                root=currency,
                record=OptionsChain(
                    source=DERIBIT_SOURCE,
                    symbol=row["instrument_name"],
                    symbol_raw=row["instrument_name"],
                    local_ts=captured_ns,
                    asset_class=AssetClass.CRYPTO,
                    source_ts=stamped,
                    prov=tail.prov,
                    prov_basis=tail.prov_basis,
                    prov_confidence=tail.prov_confidence,
                    prov_inputs=tail.prov_inputs,
                    underlying=currency,
                    underlying_price=_positive(row.get("underlying_price")),
                    strike=strike,
                    expiry=_expiry_ns(expiry),
                    opt_type=right,
                    mark_price=_stated(row.get("mark_price")),
                    mark_iv=percent / 100.0 if percent is not None else None,
                    bid_px=_positive(row.get("bid_price")),
                    ask_px=_positive(row.get("ask_price")),
                    last_price=_positive(row.get("last")),
                    volume=_stated(row.get("volume")),
                    open_interest=_stated(row.get("open_interest")),
                ),
            )
        )
    return PublishedChain(
        source=DERIBIT_SOURCE,
        underlying=currency,
        captured_ns=captured_ns,
        stamped_ns=newest,
        underlying_price=spot,
        premium_unit=currency,
        delay_s=0,
        contracts=tuple(contracts),
    )


class PublishedChains:
    """Whole option chains, one keyless request each."""

    def __init__(
        self, *, fetch: Fetcher | None = None, clock_ns: Callable[[], int] | None = None
    ) -> None:
        self._owned: AiohttpFetcher | None = None
        if fetch is None:
            self._owned = AiohttpFetcher()
            fetch = self._owned
        self._fetch: Fetcher = fetch
        self._clock: Callable[[], int] = clock_ns if clock_ns is not None else time.time_ns

    async def close(self) -> None:
        if self._owned is not None:
            await self._owned.close()

    async def _get(
        self, source: str, host: str, url: str, params: tuple[tuple[str, str], ...] = ()
    ) -> HttpResponse:
        await budget(source, host, spec=_BUDGET).acquire()
        return await self._fetch(HttpRequest(source=source, method="GET", url=url, params=params))

    async def cboe(self, symbol: str) -> PublishedChain:
        """The delayed chain of a US share, ETF or index (``AAPL``, ``BRK.B``, ``_SPX``).

        An underlying Cboe has no file for answers with an empty chain, not an error: the
        store says "access denied" for a key that does not exist.
        """
        if _CBOE_SYMBOL.fullmatch(symbol) is None:
            raise ValueError(f"not a Cboe option underlying: {symbol!r}")
        response = await self._get(CBOE_SOURCE, CBOE_HOST, CBOE_URL.format(symbol=symbol))
        captured = self._clock()
        if response.status in (403, 404) and b"AccessDenied" in response.body[:400]:
            return PublishedChain(
                source=CBOE_SOURCE,
                underlying=symbol,
                captured_ns=captured,
                stamped_ns=None,
                underlying_price=None,
                premium_unit="USD",
                delay_s=CBOE_DELAY_S,
                contracts=(),
            )
        raise_if_unservable(response, source=CBOE_SOURCE, expect="json")
        if not 200 <= response.status <= 299 or not response.body:
            raise LivePayloadError(CBOE_SOURCE, f"HTTP {response.status}")
        return parse_cboe_chain(response.body, symbol, captured_ns=captured)

    async def bybit(self, base: str) -> PublishedChain:
        """Every Bybit option of one base coin (``BTC``, ``ETH``, ``SOL``)."""
        if _COIN.fullmatch(base) is None:
            raise ValueError(f"not a base coin: {base!r}")
        response = await self._get(
            BYBIT_SOURCE, BYBIT_HOST, BYBIT_URL, (("category", "option"), ("baseCoin", base))
        )
        raise_if_unservable(response, source=BYBIT_SOURCE, expect="json")
        if not 200 <= response.status <= 299 or not response.body:
            raise LivePayloadError(BYBIT_SOURCE, f"HTTP {response.status}")
        return parse_bybit_chain(response.body, base, captured_ns=self._clock())

    async def deribit(self, currency: str) -> PublishedChain:
        """Deribit's option book summary of one currency (``BTC``, ``ETH``)."""
        if _COIN.fullmatch(currency) is None:
            raise ValueError(f"not a currency: {currency!r}")
        response = await self._get(
            DERIBIT_SOURCE, DERIBIT_HOST, DERIBIT_URL, (("currency", currency), ("kind", "option"))
        )
        if response.status == 400:
            # Deribit answers 400 for a currency it lists nothing in.
            return PublishedChain(
                source=DERIBIT_SOURCE,
                underlying=currency,
                captured_ns=self._clock(),
                stamped_ns=None,
                underlying_price=None,
                premium_unit=currency,
                delay_s=0,
                contracts=(),
            )
        raise_if_unservable(response, source=DERIBIT_SOURCE, expect="json")
        if not 200 <= response.status <= 299 or not response.body:
            raise LivePayloadError(DERIBIT_SOURCE, f"HTTP {response.status}")
        return parse_deribit_chain(response.body, currency, captured_ns=self._clock())
