"""Yahoo Finance provider client implementation."""

import asyncio
import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Self

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from yfinance.exceptions import YFRateLimitError

from crocodile.core.ratelimit.token_bucket import TokenBucket
from crocodile.core.schema.enums import AssetClass, CorpActionType, FundPeriod, OptType
from crocodile.core.schema.records import (
    OHLCV,
    CorporateAction,
    Fundamental,
    InsiderTransaction,
    OptionsChain,
    Record,
)

logger = logging.getLogger(__name__)

# YfData is a process-wide singleton, even for Tickers with distinct sessions.
# Lock order: async client request -> shared worker -> client session -> yfinance
# singleton/cookie locks. Synchronous reset starts at the shared worker lock.
# Direct yfinance users must cooperate through run_yfinance_default_call; this
# does not coordinate arbitrary external threads that bypass this module.
_YF_WORKER_LOCK = threading.RLock()
_YF_DEFAULT_SESSION: Any = None


def _default_yfinance_session() -> Any:
    """Return the process-owned default session while holding the worker lock."""
    global _YF_DEFAULT_SESSION
    if _YF_DEFAULT_SESSION is None:
        from yfinance.data import new_session

        _YF_DEFAULT_SESSION = new_session()
    return _YF_DEFAULT_SESSION


def _detach_yfinance_session(session: requests.Session) -> None:
    """Keep a no-session Ticker from inheriting a client session we are closing."""
    from yfinance.data import YfData

    data = YfData()
    lock = getattr(data, "_cookie_lock", None)

    def detach() -> None:
        if getattr(data, "_session", None) is session:
            data._session = _default_yfinance_session()

    if lock is not None:
        with lock:
            detach()
    else:
        detach()


async def _run_yfinance_worker(
    func: Callable[[], Any], *, cancel_before_start: bool = False
) -> Any:
    """Own shared state until a worker finishes, including cancellation drain."""
    cancelled = threading.Event()

    def run() -> Any:
        with _YF_WORKER_LOCK:
            # A cancelled request waiting at the shared gate must not start I/O.
            # Once started, mutations/close and network work must finish safely.
            if cancel_before_start and cancelled.is_set():
                return None
            return func()

    future = asyncio.get_running_loop().run_in_executor(None, run)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancelled.set()
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        if not future.cancelled():
            future.exception()  # Retrieve a worker error; cancellation wins.
        raise


async def run_yfinance_default_call(func: Callable[[Any], Any]) -> Any:
    """Run a complete blocking yfinance call with shared session ownership.

    Construct Tickers inside ``func`` with the supplied session. The default
    session belongs to this module for the process lifetime; callers borrow it
    and must not close it. Cancellation drains running work and skips I/O that
    is still waiting for the shared gate. Uncoordinated direct yfinance threads
    are outside this ownership boundary.
    """
    return await _run_yfinance_worker(
        lambda: func(_default_yfinance_session()), cancel_before_start=True
    )


USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    ),
]


class YahooError(Exception):
    """Exception raised for Yahoo Finance provider errors."""

    pass


def _clean_float(val: Any) -> float | None:
    """Helper to convert float values to None if NaN."""
    if pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _underlying_spot(chain: Any) -> float | None:
    """The underlying's price out of the option-chain payload, or ``None`` if it is absent.

    ``yfinance``'s ``option_chain()`` returns ``(calls, puts, underlying)`` and the third
    element is Yahoo's own ``quote`` block from the *same* ``optionChain`` response — the
    one request that produced the strikes also carried the spot they are struck against.
    That is why this is read here rather than fetched separately: a second call would
    stamp a price from a different instant onto a chain snapshot, and every moneyness in
    the surface would be a ratio of two moments.

    Only ``regularMarketPrice`` is consulted. ``regularMarketPreviousClose`` is present in
    the same block and is a *different* measurement — yesterday's close — so reading it
    when today's is missing would put a stale number in a field the record documents as
    the underlying's price, with nothing on the row to say which one it is. An absent
    spot stays ``None``, which the surface renders as a ``NaN`` moneyness and an
    ``unavailable`` vol wherever the venue published no IV: a visible hole rather than a
    quiet approximation.
    """
    quote = getattr(chain, "underlying", None)
    if not isinstance(quote, Mapping):
        return None
    price = _clean_float(quote.get("regularMarketPrice"))
    return price if price is not None and price > 0.0 else None


def _clean_str(val: Any) -> str | None:
    """Helper to convert string values to None if NaN or None."""
    if pd.isna(val) or val is None:
        return None
    return str(val)


def _expiry_to_ns(expiry: str) -> int:
    """Widen Yahoo's ``YYYY-MM-DD`` expiry to the UTC epoch nanoseconds the record wants.

    Yahoo publishes a date and nothing more, so the instant has to be chosen here.
    **Midnight UTC on that date.** Two reasons, and the second is the binding one:

    * It is the only instant from which the original date comes back. Every other
      timestamp in this product is UTC epoch nanoseconds and every date is derived from
      one by UTC truncation (``store.rows._date_from_ns``), so midnight UTC round-trips
      through the same arithmetic the lake partitions with. 21:00 UTC — the 16:00 ET
      close — would round-trip too, but only by accident of the offset, and not in
      summer.
    * Any market-hours instant would be an invention. A US listed option settles at a
      time this payload never states, and it differs between a PM-settled weekly and an
      AM-settled index option. Stating 00:00 UTC says "Yahoo gave a date"; stating a
      close time says "Yahoo gave a settlement instant", which is false.

    The direction that must not lose anything is nanoseconds → date, and it does not.
    ``OptionsChain.expiry`` is an ``int`` precisely so a venue that *does* report an
    intraday expiry can say so; this converter is for the venues that do not.

    Raises:
        ValueError: if ``expiry`` is not an ISO date. Guessing an expiry would put an
            option on the wrong day of a term structure with nothing to show for it.
    """
    day = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(day.timestamp()) * 1_000_000_000


def _intraday_records(
    df: pd.DataFrame, symbol: str, interval: str, local_ts: int
) -> list[OHLCV]:
    """Convert one fetched frame without touching the client's network session."""
    records: list[OHLCV] = []
    for idx, row in df.iterrows():
        if not isinstance(idx, datetime):
            ts_val = pd.to_datetime(idx)
        else:
            ts_val = idx

        if ts_val.tzinfo is None:
            ts_val = ts_val.tz_localize("UTC")
        else:
            ts_val = ts_val.tz_convert("UTC")
        source_ts = int(ts_val.timestamp() * 1e9)

        open_val = _clean_float(row.get("Open"))
        high_val = _clean_float(row.get("High"))
        low_val = _clean_float(row.get("Low"))
        close_val = _clean_float(row.get("Close"))
        volume_val = _clean_float(row.get("Volume"))

        if (
            open_val is None
            or high_val is None
            or low_val is None
            or close_val is None
            or volume_val is None
        ):
            logger.warning(
                "Skipping bar with missing price/volume for %s at %s",
                symbol,
                ts_val,
            )
            continue

        bar = OHLCV(
            source="yahoo",
            symbol=symbol,
            symbol_raw=symbol,
            source_ts=source_ts,
            local_ts=local_ts,
            interval=interval,
            open=open_val,
            high=high_val,
            low=low_val,
            close=close_val,
            volume=volume_val,
            vwap=None,
            num_trades=None,
            asset_class=AssetClass.EQUITY,
        )
        records.append(bar)

    return records


class YahooClient:
    """Client for Yahoo Finance using the unofficial yfinance library.

    Designed with rate limiting, retries, and crumb/cookie session resets on 429 errors.
    """

    def __init__(
        self,
        rate_limiter: TokenBucket | None = None,
        backoff_delay: float = 5.0,
    ) -> None:
        """Initialize YahooClient.

        Args:
            rate_limiter: Token bucket rate limiter. Defaults to ~1 req/sec.
            backoff_delay: Initial retry/backoff delay on rate limits.
        """
        # Default rate limit of ~1 req per 1.5 seconds, burst capacity 5
        self.rate_limiter = rate_limiter or TokenBucket(capacity=5.0, refill_rate=1.0 / 1.5)
        self.backoff_delay = backoff_delay
        # Async requests own the full retry lifecycle. The thread lock also
        # protects explicit synchronous reset callers while a worker uses it.
        self._request_lock = asyncio.Lock()
        self._session_lock = threading.RLock()
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Construct a requests session with randomized User-Agent and retry logic."""
        session = requests.Session()
        ua = random.choice(USER_AGENTS)
        session.headers.update(
            {
                "User-Agent": ua,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        retries = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )
        session.mount("https://", HTTPAdapter(max_retries=retries))

        # Enforce default timeout on all requests using this session to avoid hanging
        orig_request = session.request

        def request_with_timeout(*args: Any, **kwargs: Any) -> requests.Response:
            kwargs.setdefault("timeout", 10.0)
            return orig_request(*args, **kwargs)

        session.request = request_with_timeout  # type: ignore

        return session

    def reset_session(self) -> None:
        """Reset the internal requests session to force a fresh cookie and crumb handshake."""
        with _YF_WORKER_LOCK, self._session_lock:
            self._reset_session()

    def _reset_session(self) -> None:
        try:
            self.session.close()
        except Exception as e:
            logger.debug("Error closing old requests session: %s", e)
        self.session = self._create_session()
        logger.info("Yahoo Finance requests session reset with fresh User-Agent and clean cookies.")

        # Clear yfinance crumb/cookie state used by yfinance 1.4.x+ (YfData singleton)
        # and legacy utils modules if present.
        try:
            import yfinance as yf

            modules_to_clear = [yf.utils]
            try:
                import yfinance.scrapers.quote as yf_quote

                modules_to_clear.append(yf_quote.utils)
            except Exception:
                pass
            for mod in modules_to_clear:
                if mod is not None:
                    if hasattr(mod, "_crumb"):
                        mod._crumb = None
                    if hasattr(mod, "_cookie"):
                        mod._cookie = None

            try:
                from yfinance.data import YfData

                yf_data = YfData()
                # Clear crumb/cookie under lock; assign session directly so we
                # do not re-enter _cookie_lock via _set_session (deadlock risk).
                lock = getattr(yf_data, "_cookie_lock", None)

                def _clear() -> None:
                    if hasattr(yf_data, "_crumb"):
                        yf_data._crumb = None
                    if hasattr(yf_data, "_cookie"):
                        yf_data._cookie = None
                    if hasattr(yf_data, "_session"):
                        yf_data._session = self.session

                if lock is not None:
                    with lock:
                        _clear()
                else:
                    _clear()
            except Exception as e:
                logger.debug("Failed to reset YfData crumb/cookie: %s", e)
        except Exception as e:
            logger.debug("Failed to purge yfinance global crumb cache: %s", e)

    async def _execute_yf_call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a yfinance blocking call in an executor, handling 429s and crumb reset logic."""
        async with self._request_lock:
            return await self._execute_yf_call_locked(func, *args, **kwargs)

    async def _run_session_worker(
        self, func: Callable[[], Any], *, cancel_before_start: bool = False
    ) -> Any:
        """Keep client and shared ownership until an executor worker finishes."""
        return await _run_yfinance_worker(func, cancel_before_start=cancel_before_start)

    async def _execute_yf_call_locked(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        max_retries = 5
        delay = self.backoff_delay

        def _call() -> Any:
            with self._session_lock:
                # Bind in the worker, after any synchronous reset has finished.
                kwargs["session"] = self.session
                return func(*args, **kwargs)

        for attempt in range(max_retries):
            await self.rate_limiter.acquire(1.0)

            try:
                result = await self._run_session_worker(_call, cancel_before_start=True)
                return result
            except YFRateLimitError:
                logger.warning(
                    "Yahoo Finance rate limit (HTTP 429) hit. "
                    "Attempt %d/%d. Backing off for %.2f seconds.",
                    attempt + 1,
                    max_retries,
                    delay,
                )
                self.rate_limiter.update_backoff(delay)
                await self._run_session_worker(self.reset_session)
                await asyncio.sleep(delay)
                delay *= 2.0
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    logger.warning(
                        "Yahoo Finance HTTP 429 error. "
                        "Attempt %d/%d. Backing off for %.2f seconds.",
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    self.rate_limiter.update_backoff(delay)
                    await self._run_session_worker(self.reset_session)
                    await asyncio.sleep(delay)
                    delay *= 2.0
                else:
                    raise
            except Exception as exc:
                exc_str = str(exc).lower()
                if "crumb" in exc_str or "rate limit" in exc_str or "429" in exc_str:
                    logger.warning(
                        "Yahoo Finance potential rate limit or crumb issue: %s. "
                        "Attempt %d/%d. Resetting session and backing off for %.2f seconds.",
                        exc,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    self.rate_limiter.update_backoff(delay)
                    await self._run_session_worker(self.reset_session)
                    await asyncio.sleep(delay)
                    delay *= 2.0
                else:
                    logger.error("Unexpected Yahoo Finance client error: %s", exc)
                    raise

        raise RuntimeError(f"Yahoo Finance provider request failed after {max_retries} attempts.")

    async def close(self) -> None:
        """Close the requests session."""
        def _close() -> None:
            with self._session_lock:
                _detach_yfinance_session(self.session)
                self.session.close()

        async with self._request_lock:
            await self._run_session_worker(_close)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    async def _check_health(self) -> bool:
        """Verify client health by querying a highly active ticker like SPY."""
        try:

            def _call(session: requests.Session) -> pd.DataFrame:
                return yf.Ticker("SPY", session=session).history(period="1d", auto_adjust=False)

            df = await self._execute_yf_call(_call)
            return df is not None and not df.empty
        except Exception as e:
            logger.debug("Yahoo health check failed: %s", e)
            return False

    async def fetch_eod_history(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[Record]:
        """Fetch daily historical EOD bars and embedded splits/dividends.

        Args:
            symbol: Ticker symbol (e.g. AAPL)
            start: Start date string (YYYY-MM-DD)
            end: End date string (YYYY-MM-DD)

        Returns:
            A list of OHLCV and CorporateAction records.
        """

        def _call(session: requests.Session) -> pd.DataFrame:
            ticker = yf.Ticker(symbol, session=session)
            if start:
                return ticker.history(
                    start=start,
                    end=end,
                    interval="1d",
                    auto_adjust=False,
                    actions=True,
                )
            else:
                return ticker.history(
                    period="max",
                    interval="1d",
                    auto_adjust=False,
                    actions=True,
                )

        df = await self._execute_yf_call(_call)
        if df is None or df.empty:
            if not await self._check_health():
                raise YahooError(
                    "Yahoo Finance integration is unhealthy (crumb/cookie or "
                    f"rate limit failure) while fetching EOD history for {symbol}."
                )
            return []

        records: list[Record] = []
        local_ts = time.time_ns()

        for idx, row in df.iterrows():
            # Index is typically a DatetimeIndex
            if not isinstance(idx, datetime):
                # Fallback conversion
                ts_val = pd.to_datetime(idx)
            else:
                ts_val = idx

            # Convert to UTC epoch nanoseconds
            if ts_val.tzinfo is None:
                ts_val = ts_val.tz_localize("UTC")
            else:
                ts_val = ts_val.tz_convert("UTC")
            source_ts = int(ts_val.timestamp() * 1e9)
            date_str = ts_val.strftime("%Y-%m-%d")

            open_val = _clean_float(row.get("Open"))
            high_val = _clean_float(row.get("High"))
            low_val = _clean_float(row.get("Low"))
            close_val = _clean_float(row.get("Close"))
            volume_val = _clean_float(row.get("Volume"))

            if (
                open_val is None
                or high_val is None
                or low_val is None
                or close_val is None
                or volume_val is None
            ):
                logger.warning(
                    "Skipping bar with missing price/volume for %s on %s",
                    symbol,
                    date_str,
                )
                continue

            # Map the OHLCV record
            bar = OHLCV(
                source="yahoo",
                symbol=symbol,
                symbol_raw=symbol,
                source_ts=source_ts,
                local_ts=local_ts,
                interval="1d",
                open=open_val,
                high=high_val,
                low=low_val,
                close=close_val,
                volume=volume_val,
                vwap=None,
                num_trades=None,
                asset_class=AssetClass.EQUITY,
            )
            records.append(bar)

            # Map dividends
            if "Dividends" in row:
                div_val = _clean_float(row.get("Dividends"))
                if div_val is not None and div_val > 0.0:
                    div = CorporateAction(
                        source="yahoo",
                        symbol=symbol,
                        symbol_raw=symbol,
                        source_ts=source_ts,
                        local_ts=local_ts,
                        ex_date=date_str,
                        type=CorpActionType.DIVIDEND_CASH,
                        value=div_val,
                        asset_class=AssetClass.EQUITY,
                    )
                    records.append(div)

            # Map stock splits
            if "Stock Splits" in row:
                split_val = _clean_float(row.get("Stock Splits"))
                if split_val is not None and split_val > 0.0:
                    split = CorporateAction(
                        source="yahoo",
                        symbol=symbol,
                        symbol_raw=symbol,
                        source_ts=source_ts,
                        local_ts=local_ts,
                        ex_date=date_str,
                        type=CorpActionType.SPLIT,
                        value=split_val,
                        asset_class=AssetClass.EQUITY,
                    )
                    records.append(split)

        return records

    async def fetch_intraday_bars(
        self,
        symbol: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
    ) -> list[OHLCV]:
        """Fetch intraday historical bars.

        Args:
            symbol: Ticker symbol (e.g. AAPL)
            interval: Bar resolution (e.g. 1m, 5m, 1h)
            start: Start date string (YYYY-MM-DD)
            end: End date string (YYYY-MM-DD)

        Returns:
            A list of OHLCV records.
        """

        def _call(session: requests.Session) -> pd.DataFrame:
            ticker = yf.Ticker(symbol, session=session)
            if start:
                return ticker.history(
                    start=start,
                    end=end,
                    interval=interval,
                    auto_adjust=False,
                    actions=False,
                )
            else:
                # If no start date, fetch maximum window allowed by Yahoo for that interval
                # e.g., 1m has 7 days limit, 5m has 60 days.
                period = "7d" if interval == "1m" else "60d"
                return ticker.history(
                    period=period,
                    interval=interval,
                    auto_adjust=False,
                    actions=False,
                )

        df = await self._execute_yf_call(_call)
        if df is None or df.empty:
            if not await self._check_health():
                raise YahooError(
                    "Yahoo Finance integration is unhealthy (crumb/cookie or "
                    f"rate limit failure) while fetching intraday bars for {symbol}."
                )
            return []

        return await asyncio.to_thread(_intraday_records, df, symbol, interval, time.time_ns())

    async def fetch_option_chain(
        self,
        symbol: str,
        expiry: str | None = None,
    ) -> list[OptionsChain]:
        """Fetch option chain quotes.

        Args:
            symbol: Underlying symbol (e.g. AAPL)
            expiry: Optional specific expiration date string (YYYY-MM-DD).
                    If None, fetches options for all available expirations.

        Returns:
            A list of OptionsChain records.
        """
        if expiry:
            expirations = (expiry,)
        else:

            def _get_exp(session: requests.Session) -> tuple[str, ...]:
                opts = yf.Ticker(symbol, session=session).options
                if not isinstance(opts, (list, tuple)):
                    return ()
                return tuple(str(x) for x in opts)

            try:
                expirations = await self._execute_yf_call(_get_exp)
            except Exception as e:
                logger.error("Failed to fetch option expirations list for %s: %s", symbol, e)
                raise YahooError(f"Failed to fetch option expirations list for {symbol}") from e

        if not expirations:
            if not await self._check_health():
                raise YahooError(
                    "Yahoo Finance integration is unhealthy (crumb/cookie or "
                    f"rate limit failure) while fetching options for {symbol}."
                )
            return []

        records: list[OptionsChain] = []
        local_ts = time.time_ns()

        # Fetch option chains concurrently
        async def _fetch_single_chain(exp: str) -> list[OptionsChain]:
            def _get_chain(session: requests.Session, current_exp: str = exp) -> Any:
                return yf.Ticker(symbol, session=session).option_chain(current_exp)

            try:
                chain = await self._execute_yf_call(_get_chain)
            except Exception as e:
                logger.error("Failed to fetch option chain for %s expiry %s: %s", symbol, exp, e)
                return []

            single_records: list[OptionsChain] = []
            underlying_price = _underlying_spot(chain)
            for opt_type, df in (("calls", chain.calls), ("puts", chain.puts)):
                if df is None or getattr(df, "empty", True):
                    continue
                opt_enum = OptType.CALL if opt_type == "calls" else OptType.PUT
                for _, row in df.iterrows():
                    contract_symbol = str(row["contractSymbol"])

                    # Parse lastTradeDate if present
                    source_ts = None
                    if "lastTradeDate" in row and not pd.isna(row["lastTradeDate"]):
                        ts_val = pd.to_datetime(row["lastTradeDate"])
                        if ts_val.tzinfo is None:
                            ts_val = ts_val.tz_localize("UTC")
                        else:
                            ts_val = ts_val.tz_convert("UTC")
                        source_ts = int(ts_val.timestamp() * 1e9)

                    try:
                        strike_val = _clean_float(row.get("strike"))
                        if strike_val is None:
                            continue
                    except Exception:
                        continue

                    quote = OptionsChain(
                        source="yahoo",
                        symbol=contract_symbol,
                        symbol_raw=contract_symbol,
                        source_ts=source_ts,
                        local_ts=local_ts,
                        asset_class=AssetClass.EQUITY,
                        underlying=symbol,
                        # The spot out of this same payload's quote block, or `None` when
                        # it did not carry one — a different fact from a price of zero,
                        # and the one thing the four vol-surface capabilities cannot
                        # compute a moneyness or invert a mid without. See
                        # `_underlying_spot` for why it is not fetched separately.
                        underlying_price=underlying_price,
                        expiry=_expiry_to_ns(exp),
                        strike=strike_val,
                        opt_type=opt_enum,
                        bid_px=_clean_float(row.get("bid")),
                        ask_px=_clean_float(row.get("ask")),
                        last_price=_clean_float(row.get("lastPrice")),
                        volume=_clean_float(row.get("volume")),
                        open_interest=_clean_float(row.get("openInterest")),
                        mark_iv=_clean_float(row.get("impliedVolatility")),
                        delta=None,
                        gamma=None,
                        vega=None,
                        theta=None,
                        rho=None,
                    )
                    single_records.append(quote)
            return single_records

        tasks = [_fetch_single_chain(exp) for exp in expirations]
        results = await asyncio.gather(*tasks)
        for res in results:
            records.extend(res)

        return records

    async def fetch_financial_statements(self, symbol: str) -> list[Fundamental]:
        """Fetch annual and quarterly income statement, balance sheet, and cash flow.

        Maps financial statements to Fundamental records.

        Args:
            symbol: Ticker symbol (e.g. AAPL)

        Returns:
            A list of Fundamental records.
        """

        def _call_financials(session: requests.Session) -> dict[str, pd.DataFrame]:
            ticker = yf.Ticker(symbol, session=session)
            return {
                "annual_financials": ticker.financials,
                "quarterly_financials": ticker.quarterly_financials,
                "annual_balance_sheet": ticker.balance_sheet,
                "quarterly_balance_sheet": ticker.quarterly_balance_sheet,
                "annual_cashflow": ticker.cashflow,
                "quarterly_cashflow": ticker.quarterly_cashflow,
            }

        try:
            statements = await self._execute_yf_call(_call_financials)
        except Exception as e:
            logger.error("Failed to fetch financials for %s: %s", symbol, e)
            return []

        records: list[Fundamental] = []
        local_ts = time.time_ns()

        # Config mapping for parsing statements
        config = [
            ("annual_financials", "FY", "10-K", True),
            ("quarterly_financials", None, "10-Q", True),
            ("annual_balance_sheet", "FY", "10-K", False),
            ("quarterly_balance_sheet", None, "10-Q", False),
            ("annual_cashflow", "FY", "10-K", True),
            ("quarterly_cashflow", None, "10-Q", True),
        ]

        for name, fp_val, form, is_duration in config:
            df = statements.get(name)
            if df is None or df.empty:
                continue

            for tag, row in df.iterrows():
                tag_str = str(tag)
                for col_date, val in row.items():
                    val_float = _clean_float(val)
                    if val_float is None:
                        continue

                    # Parse date column
                    ts = pd.to_datetime(col_date)
                    end_str = ts.strftime("%Y-%m-%d")

                    # Period end date timestamp as source_ts
                    source_ts = int(ts.timestamp() * 1e9)

                    # Determine fiscal period for quarter if None
                    fp = fp_val
                    if fp is None:
                        # Calendar approximation for Q1-Q4 based on month
                        m = ts.month
                        if m in (1, 2, 3):
                            fp = "Q1"
                        elif m in (4, 5, 6):
                            fp = "Q2"
                        elif m in (7, 8, 9):
                            fp = "Q3"
                        else:
                            fp = "Q4"

                    # Calculate period start for duration statements
                    start_str = None
                    if is_duration:
                        if fp == "FY":
                            # Approx 1 year prior
                            start_str = (ts - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
                        else:
                            # Approx 3 months prior
                            start_str = (ts - pd.DateOffset(months=3)).strftime("%Y-%m-%d")

                    fundamental = Fundamental(
                        source="yahoo",
                        symbol=symbol,
                        symbol_raw=symbol,
                        source_ts=source_ts,
                        local_ts=local_ts,
                        taxonomy="yahoo",
                        tag=tag_str,
                        unit="USD",
                        val=val_float,
                        end=end_str,
                        start=start_str,
                        fy=int(ts.year),
                        fp=FundPeriod(fp) if fp else None,
                        form=form,
                        filed=None,
                        accn=None,
                        frame=None,
                        asset_class=AssetClass.EQUITY,
                    )
                    records.append(fundamental)

        return records

    async def fetch_insider_transactions(self, symbol: str) -> list[InsiderTransaction]:
        """Fetch insider transactions.

        Args:
            symbol: Ticker symbol (e.g. AAPL)

        Returns:
            A list of InsiderTransaction records.
        """

        def _call(session: requests.Session) -> pd.DataFrame | None:
            ticker = yf.Ticker(symbol, session=session)
            return ticker.insider_transactions

        df = await self._execute_yf_call(_call)
        if df is None or df.empty:
            return []

        records: list[InsiderTransaction] = []
        local_ts = time.time_ns()

        for _, row in df.iterrows():
            insider_name = _clean_str(row.get("Insider"))
            transaction_type = _clean_str(row.get("Transaction"))
            position = _clean_str(row.get("Position"))
            date_val = row.get("Start Date")

            if not insider_name or not transaction_type or pd.isna(date_val):
                continue

            ts = pd.to_datetime(date_val)
            date_str = ts.strftime("%Y-%m-%d")

            shares = _clean_float(row.get("Shares"))
            value = _clean_float(row.get("Value"))
            ownership = _clean_str(row.get("Ownership"))

            # Calculate price if possible
            price = None
            if value is not None and shares is not None and shares > 0.0:
                price = value / shares

            txn = InsiderTransaction(
                source="yahoo",
                symbol=symbol,
                symbol_raw=symbol,
                source_ts=None,
                local_ts=local_ts,
                insider_name=insider_name,
                position=position or "Unknown",
                transaction_type=transaction_type,
                transaction_date=date_str,
                shares=shares,
                price=price,
                value=value,
                ownership=ownership,
                asset_class=AssetClass.EQUITY,
            )
            records.append(txn)

        return records
