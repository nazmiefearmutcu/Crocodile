from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

import aiohttp

from crocodile.core.schema.enums import AssetClass, CorpActionType, SecurityType
from crocodile.core.schema.records import OHLCV, CorporateAction, Record
from crocodile.core.sink.base import Sink
from crocodile.equity.providers.base import US_EQUITY_MICS, Provider
from crocodile.equity.reference.identity import InstrumentIdentity
from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/125.0.0.0 Safari/537.36",  # noqa: E501
]


def get_spoofed_headers() -> dict[str, str]:
    muid = "".join(random.choices("0123456789ABCDEF", k=32))
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.msn.com/",
        "Cookie": f"MUID={muid};",
    }


def safe_float(v: Any) -> float | None:
    """Parse one MSN chart entry, answering ``None`` where the page published no number.

    It used to take a ``default`` and answer ``0.0``, which collapsed "the page said zero"
    into "the page said nothing" — and the callers below write required measurement fields,
    where those two are different claims. A caller that genuinely has a default can still
    write ``or`` at its own call site; none of the ones here does, because a price of zero
    is not a missing price, it is a false one.
    """
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.strip().replace(",", "")
            if v in ("", "N/A", "null", "None"):
                return None
        return float(v)
    except (ValueError, TypeError):
        return None


_BAR_SERIES: Final[tuple[tuple[str, str], ...]] = (
    ("open", "openPrices"),
    ("high", "pricesHigh"),
    ("low", "pricesLow"),
    ("close", "prices"),
    ("volume", "volumes"),
)
"""``OHLCV`` field ← the MSN chart series that carries it.

MSN ships these as five independent arrays beside ``timeStamps`` and guarantees nothing
about their relative lengths, which is why they are read through one guard rather than
five.
"""


def _bar_measurements(series: Mapping[str, Any], index: int) -> dict[str, float] | None:
    """Read one bar out of MSN's parallel chart arrays, or ``None`` if the page is short.

    Entries inside the arrays are ``null`` or ``"N/A"`` across halts and holidays, and the
    arrays themselves run shorter than ``timeStamps``. The code this replaced substituted
    ``0.0`` for both cases — ``safe_float(close_p[idx]) if idx < len(close_p) else 0.0`` —
    and the bar it built carried the header's default ``prov=NATIVE``, so a short ``prices``
    array put a close of ``0.0`` into the lake as a venue-reported price:
    ``SELECT min(close) FROM ohlcv WHERE source='msn_money'`` returned a number no share
    ever traded at, with nothing on the row to tell it from a real one. The provider's own
    suite pinned that as behaviour, asserting ``bar.open == 0.0`` beside ``bar.low ==
    150.0`` — a bar whose high is below its low.

    A bar *is* its five numbers. Where the page did not publish one of them there is no bar
    to report, so the caller skips it rather than inventing the missing quantity.

    ``volume`` is guarded exactly like the four prices, deliberately. A structural zero
    would be honest only if MSN's omission were structural — the way a quote-derived bar
    genuinely has no traded volume — and nothing in the payload separates "this instrument
    does not trade" from "this array is short". Declaring a structural zero on that guess
    is the same fabrication with a certificate attached.
    """
    measured: dict[str, float] = {}
    for field, key in _BAR_SERIES:
        column = series.get(key)
        if not isinstance(column, list) or index >= len(column):
            return None
        value = safe_float(column[index])
        if value is None:
            return None
        measured[field] = value
    return measured


class MsnMoneyProvider(Provider):
    name = "msn_money"
    ws_url = ""
    rest_url = "https://assets.msn.com"

    supported_channels: ClassVar[frozenset[str]] = frozenset({"ohlcv", "corp_action"})
    """Daily bars, and the splits and dividends the same endpoint carries alongside them.

    ``corp_action`` was already written by :meth:`backfill` and appeared on no channel menu,
    because the menu was a hand-written list of four market-data names. It is on the derived
    vocabulary now, which matters beyond tidiness: ``corp_action`` is one of the two channels
    an equity lake could actually be filled with before this change, and a user reading the
    menu had no way to learn that.

    ``bar`` is absent and still accepted; see
    :meth:`~crocodile.equity.providers.base.Provider._reject_unservable_channels`.
    """

    supported_mics: ClassVar[frozenset[str]] = US_EQUITY_MICS
    """US-only in this package (spec §3.12 ruling); multi-market support is Package 5."""

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        apikey: str | None = None,
        ocid: str = "finance-utils-peregrine",
    ) -> None:
        super().__init__(symbols, channels, out, registry)
        # Prefer explicit arg; otherwise require MSN_MONEY_APIKEY (never ship a default key).
        self.apikey = apikey if apikey is not None else os.environ.get("MSN_MONEY_APIKEY", "")
        self.ocid = ocid
        self.session: aiohttp.ClientSession | None = None

    async def list_instruments(self) -> list[InstrumentIdentity]:
        insts = []
        for sym in self.symbols:
            sec_type = (
                SecurityType.UNKNOWN
                if (sym.startswith("^") or sym.startswith("."))
                else SecurityType.CS
            )
            insts.append(
                InstrumentIdentity(
                    symbol=sym,
                    source=self.name,
                    symbol_raw=sym,
                    security_type=sec_type,
                )
            )
        return insts

    async def _subscribe(self, transport: Any) -> None:
        pass

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        return ()

    async def run(self, max_reconnects: int = -1) -> None:
        raise NotImplementedError(
            "MSN Money provider is strictly a batch/backfill provider and "
            "does not support streaming run loop."
        )

    async def backfill(
        self,
        channel: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> AsyncIterator[Record]:
        if not self.apikey:
            raise ValueError(
                "MSN Money API key required. Set MSN_MONEY_APIKEY or pass apikey=..."
            )
        if self.session is None:
            self.session = aiohttp.ClientSession()

        try:
            sec_id = await self._resolve_sec_id(symbol)
            local_ts = time.time_ns()

            # One struct now, so both request spellings get the same record. They used
            # to take two arms that both built a `Bar`: asking for `ohlcv` wrote records
            # tagged `bar`, into the wrong partition, with no error.
            if channel in ("bar", "ohlcv"):
                duration_ns = end_ns - start_ns
                one_day_ns = 24 * 60 * 60 * 1_000_000_000
                five_days_ns = 5 * one_day_ns
                one_month_ns = 31 * one_day_ns
                three_months_ns = 92 * one_day_ns
                six_months_ns = 184 * one_day_ns
                one_year_ns = 365 * one_day_ns
                five_years_ns = 5 * one_year_ns

                if duration_ns <= five_days_ns:
                    chart_type = "5D"
                elif duration_ns <= one_month_ns:
                    chart_type = "1M"
                elif duration_ns <= three_months_ns:
                    chart_type = "3M"
                elif duration_ns <= six_months_ns:
                    chart_type = "6M"
                elif duration_ns <= one_year_ns:
                    chart_type = "1Y"
                elif duration_ns <= five_years_ns:
                    chart_type = "5Y"
                else:
                    chart_type = "All"

                url = f"{self.rest_url}/service/Finance/Charts"
                params = {
                    "apikey": self.apikey,
                    "ocid": self.ocid,
                    "ids": sec_id,
                    "type": chart_type,
                    "wrapodata": "false",
                    "cm": "en-us",
                }
                headers = get_spoofed_headers()

                async with self.session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        is_valid = (
                            data
                            and isinstance(data, list)
                            and isinstance(data[0], dict)
                            and "series" in data[0]
                        )
                        if is_valid:
                            series = data[0]["series"]
                            timestamps = series.get("timeStamps", [])

                            # Prefer chart type for interval (weekend gaps break first-pair heuristics)
                            if chart_type in ("5D", "1M") or chart_type.endswith("D"):
                                computed_interval = "15m" if chart_type == "5D" else "1h"
                            elif "Y" in chart_type or chart_type in ("All", "5Y", "1Y"):
                                computed_interval = "1d"
                            else:
                                computed_interval = None
                            if computed_interval is None and len(timestamps) >= 2:
                                try:
                                    deltas: list[float] = []
                                    for i in range(1, min(len(timestamps), 12)):
                                        ts0 = timestamps[i - 1].replace("Z", "+00:00")
                                        ts1 = timestamps[i].replace("Z", "+00:00")
                                        delta_sec = abs(
                                            (
                                                datetime.fromisoformat(ts1)
                                                - datetime.fromisoformat(ts0)
                                            ).total_seconds()
                                        )
                                        # Ignore weekend/holiday gaps when classifying daily bars
                                        if delta_sec <= 4.5 * 86400:
                                            deltas.append(delta_sec)
                                    if deltas:
                                        deltas.sort()
                                        med = deltas[len(deltas) // 2]
                                        if med <= 90:
                                            computed_interval = "1m"
                                        elif med <= 350:
                                            computed_interval = "5m"
                                        elif med <= 1000:
                                            computed_interval = "15m"
                                        elif med <= 2000:
                                            computed_interval = "30m"
                                        elif med <= 4000:
                                            computed_interval = "1h"
                                        elif med <= 2.5 * 86400:
                                            computed_interval = "1d"
                                        elif med <= 8 * 86400:
                                            computed_interval = "1w"
                                        else:
                                            computed_interval = "1mo"
                                except Exception:
                                    pass

                            if computed_interval is None:
                                computed_interval = "1d"

                            n = len(timestamps)
                            for idx in range(n):
                                ts_str = timestamps[idx]
                                try:
                                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                    source_ts = int(dt.timestamp() * 1e9)
                                except Exception:
                                    # An unreadable stamp used to become source_ts=None, and
                                    # the window filter below skipped itself for those — so a
                                    # backfill for one June week returned bars it could not
                                    # place in time and had not asked for.
                                    log.debug(
                                        "MSN Money: unreadable chart timestamp %r for %s",
                                        ts_str,
                                        symbol,
                                    )
                                    continue

                                if not (start_ns <= source_ts <= end_ns):
                                    continue

                                measured = _bar_measurements(series, idx)
                                if measured is None:
                                    log.debug(
                                        "MSN Money: incomplete chart bar at index %d for %s; "
                                        "skipping rather than zero-filling",
                                        idx,
                                        symbol,
                                    )
                                    continue

                                bar = OHLCV(
                                    source=self.name,
                                    symbol=symbol.upper(),
                                    symbol_raw=symbol,
                                    source_ts=source_ts,
                                    local_ts=local_ts,
                                    interval=computed_interval,
                                    open=measured["open"],
                                    high=measured["high"],
                                    low=measured["low"],
                                    close=measured["close"],
                                    volume=measured["volume"],
                                    asset_class=AssetClass.EQUITY,
                                )
                                yield bar

            elif channel in ("corp_action", "corp_actions"):
                url = f"{self.rest_url}/service/Finance/QuoteSummary"
                params = {
                    "apikey": self.apikey,
                    "ocid": self.ocid,
                    "cm": "en-us",
                    "it": "web",
                    "ids": sec_id,
                    "intents": "Quotes,Exchanges,QuoteDetails",
                }
                headers = get_spoofed_headers()

                async with self.session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and isinstance(data, list) and isinstance(data[0], dict):
                            equity = data[0].get("equity", {})
                            share_stats = equity.get("shareStatistics", {})

                            # Parse Dividend
                            ex_div_amt = share_stats.get("exDividendAmount")
                            ex_div_date = share_stats.get("exDividendDate")
                            if ex_div_amt and ex_div_date:
                                ex_date = ex_div_date.split("T")[0]
                                try:
                                    dt = datetime.strptime(ex_date, "%Y-%m-%d").replace(tzinfo=UTC)
                                    ts = int(dt.timestamp() * 1e9)
                                    amount = safe_float(ex_div_amt)
                                    # "N/A" is truthy, so the guard above passes it through and
                                    # the old 0.0 default made it a declared $0.00 dividend.
                                    if amount is None:
                                        log.debug(
                                            "MSN Money: unreadable dividend amount %r for %s",
                                            ex_div_amt,
                                            symbol,
                                        )
                                    elif start_ns <= ts <= end_ns:
                                        yield CorporateAction(
                                            source=self.name,
                                            symbol=symbol.upper(),
                                            symbol_raw=symbol,
                                            source_ts=ts,
                                            local_ts=local_ts,
                                            ex_date=ex_date,
                                            type=CorpActionType.DIVIDEND_CASH,
                                            value=amount,
                                            asset_class=AssetClass.EQUITY,
                                        )
                                except Exception as e:
                                    log.debug(
                                        "Error parsing dividend ex_date %s: %s",
                                        ex_div_date,
                                        e,
                                    )

                            # Parse Split
                            last_split_factor = share_stats.get("lastSplitFactor")
                            last_split_date = share_stats.get("lastSplitDate")
                            if last_split_factor and last_split_date:
                                ex_date = last_split_date.split("T")[0]
                                try:
                                    dt = datetime.strptime(ex_date, "%Y-%m-%d").replace(tzinfo=UTC)
                                    ts = int(dt.timestamp() * 1e9)
                                    if start_ns <= ts <= end_ns:
                                        split_str = str(last_split_factor)
                                        if ":" in split_str:
                                            numerator, _, denominator = split_str.partition(":")
                                            # Falling back to the numerator alone turned a
                                            # "3:for-2" into a 3.0 split factor rather than
                                            # 1.5, and a price series back-adjusted by it
                                            # carries a permanent 2x step no consumer can
                                            # see, because the row still said prov=native.
                                            val = float(numerator) / float(denominator)
                                        else:
                                            val = float(split_str)
                                        yield CorporateAction(
                                            source=self.name,
                                            symbol=symbol.upper(),
                                            symbol_raw=symbol,
                                            source_ts=ts,
                                            local_ts=local_ts,
                                            ex_date=ex_date,
                                            type=CorpActionType.SPLIT,
                                            value=val,
                                            asset_class=AssetClass.EQUITY,
                                        )
                                except Exception as e:
                                    log.debug(
                                        "Error parsing split factor/date %s/%s: %s",
                                        last_split_factor,
                                        last_split_date,
                                        e,
                                    )

        except Exception as e:
            log.error("MSN Money backfill error for %s: %s", symbol, e)

    async def _resolve_sec_id(self, symbol: str) -> str:
        if not self.session:
            self.session = aiohttp.ClientSession()

        query_sym = symbol.split(".")[0].split(":")[0]
        url = "https://services.bingapis.com/contentservices-finance.csautosuggest/api/v1/Query"
        params = {"query": query_sym, "market": "en-us", "count": "3"}
        headers = get_spoofed_headers()

        def normalize_ticker(t: str) -> str:
            return t.upper().replace(".", "").replace("-", "").replace("/", "").strip()

        norm_orig = normalize_ticker(symbol)
        norm_query = normalize_ticker(query_sym)

        try:
            async with self.session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    stocks = data.get("data", {}).get("stocks", [])
                    exact: str | None = None
                    for stock_str in stocks:
                        try:
                            stock_data = json.loads(stock_str)
                            rt00s = stock_data.get("RT00S", "")
                            # Prefer exact full-symbol match only (avoid BRK vs BRK.B)
                            if normalize_ticker(rt00s) == norm_orig:
                                return str(stock_data.get("SecId"))
                            if exact is None and normalize_ticker(rt00s) == norm_query:
                                exact = str(stock_data.get("SecId"))
                        except Exception:
                            continue
                    if exact is not None and norm_orig == norm_query:
                        return exact
                    log.warning(
                        "MSN Money: no exact SecId match for %r among %d suggestions",
                        symbol,
                        len(stocks),
                    )
        except Exception as e:
            log.debug("Error resolving ticker symbol suggestions: %s", e)

        raise ValueError(f"Could not resolve MSN Money SecId for symbol {symbol!r}")

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None
