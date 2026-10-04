"""SEC EDGAR Provider Client."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator, Generator, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import msgspec

from crocodile.core.errors import ConfigError, FatalConnectorError
from crocodile.core.ratelimit import TokenBucketLimiter
from crocodile.core.schema.enums import AssetClass, FundPeriod
from crocodile.core.schema.records import Filing, Fundamental, Holding13F, InsiderTransaction
from crocodile.equity.providers.sec_edgar.form4 import Form4ParseError, parse_form4
from crocodile.equity.providers.sec_edgar.form13f import (
    Form13FParseError,
    parse_13f_information_table,
    parse_13f_primary_document,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from crocodile.core.config import Settings

log = logging.getLogger(__name__)

FORM4_FORMS = frozenset({"4", "4/A"})
"""The Section 16(a) forms this client parses as ownership documents.

Forms 3 and 5 share the ``ownershipDocument`` schema and are deliberately absent. A Form 3
is an initial statement of holdings and reports no transaction at all; a Form 5 is an
annual catch-up whose lines are the ones exempt from the two-business-day rule, so their
``transaction_date`` and their filing date can be eleven months apart — which is exactly the
gap ``sec_form4``'s docstring argues is *not* a sampling deficiency for a Form 4 because a
Form 4's own date is timely. Reading the two under one basis would make that argument false
for a fraction of the rows and there would be no column saying which fraction.
"""

FORM_13F_FORMS = frozenset({"13F-HR", "13F-HR/A"})
"""Holdings reports and their amendments. ``13F-NT`` is a notice that the positions are
reported by somebody else and carries no information table, so it is not fetched."""

_MAX_REQUEST_ATTEMPTS = 5
"""Retries per request, counting the connect and the body read against one budget.

One budget rather than one each because the two failures are the same failure seen at two
points on the wire — a stalled EDGAR — and a per-stage budget multiplies into a wait no
caller asked for: five connect attempts each allowed five reads is twenty-five backoffs
capped at thirty seconds apiece."""

_XSL_RENDERED_PREFIX = "xsl"
"""EDGAR serves an XSL-rendered HTML view of an ownership document under a sibling
directory whose name starts with this — ``xslF345X03/wf-form4_1234.xml`` — and the raw XML
at the same basename in the filing's root. ``primaryDocument`` in the submissions index
names the rendered one, which parses as HTML and yields no transactions at all."""


def require_user_agent(settings: Settings) -> str:
    """Return the contact string SEC requires, or refuse to invent one.

    The single gate on one contract, and it is here — in the module that puts the header on
    the wire — because the two modules that need it cannot both own it:
    ``crocodile.equity.reference.universe`` imports :class:`SecEdgarClient`, so the
    dependency only runs one way.

    It was written twice, and the two spellings disagreed. This one strips; the reference
    universe's ``require_sec_user_agent`` tested ``if not settings.sec_user_agent`` with no
    ``.strip()``, and that was the copy every caller reached, because nothing routed through
    :meth:`SecEdgarClient.from_settings`. So ``CROCODILE_SEC_USER_AGENT="   "`` passed the
    gate, and the failure moved from configuration time to *inside* ``begin()`` — the exact
    place ``capabilities.ops`` says it must not be, "must not first fail on an await inside
    somebody's event loop". Measured: six requests, 2 + 4 + 8 + 16 + 30 seconds of backoff,
    sixty seconds before a 403 surfaced, with ``User-Agent: '   '`` on every one of them.

    Stripping is the right half of the disagreement, and not by preference. A shell that
    exports ``CROCODILE_SEC_USER_AGENT=" "`` has supplied nothing, and a header of three
    spaces identifies nobody — which is precisely what SEC blocks. A gate whose answer
    depends on whitespace is a gate that says the request is fine and then watches it fail
    at a layer with no configuration vocabulary left to explain itself.

    Note what this is *not*: a credential. SEC issues none — ``company_tickers.json`` is
    public to anyone who says who they are, and saying so is free.

    Raises:
        ConfigError: naming the variable and what belongs in it.
    """
    user_agent = (settings.sec_user_agent or "").strip()
    if not user_agent:
        raise ConfigError(
            "SEC EDGAR requires a User-Agent identifying a contactable party and blocks "
            "requests carrying none. Set CROCODILE_SEC_USER_AGENT to something of the form "
            "'YourApp/1.0 (you@example.com)'. It is not an API key and SEC issues none: the "
            "file is public to anyone who says who they are — but an invented address is "
            "worse than a missing one, because it satisfies the string check and gives the "
            "regulator a dead mailbox."
        )
    return user_agent


COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
"""SEC's registrant-to-ticker index. Keyless, one file, no pagination.

Named here rather than inlined because two callers now read it: :meth:`
SecEdgarClient.fetch_ticker_map`, which wants the ticker-to-CIK direction, and
:meth:`SecEdgarClient.fetch_company_tickers`, which wants the rows themselves. One URL
with two spellings is one URL that can be changed in one of them.
"""


class SecCompanyTicker(msgspec.Struct, frozen=True):
    """One row of ``company_tickers.json``: a registrant, a ticker and the legal name.

    The name is what makes this worth having as a struct rather than as the two dicts
    :meth:`SecEdgarClient.fetch_ticker_map` builds. Those dicts drop ``title``, which is the
    only registrant-attested company name in the tree — Tiingo's supported-tickers file
    carries no name at all and OpenFIGI's is a security description rather than the filer's
    legal name — so the equity reference merge had nothing to fill
    :attr:`crocodile.core.schema.records.Instrument.name` from until this stopped being
    discarded at parse time.
    """

    cik: int
    ticker: str
    title: str | None = None


def parse_company_tickers(data: Any) -> list[SecCompanyTicker]:
    """Turn the decoded ``company_tickers.json`` payload into rows.

    Separate from the fetch so the parse is exercisable against a fixture rather than
    against sec.gov. The payload is a JSON *object* whose keys are row indices as strings —
    not an array — which is the shape :meth:`SecEdgarClient.fetch_ticker_map` already
    assumes and the reason anything but a ``dict`` yields nothing rather than raising: a
    schema change should empty the universe loudly downstream, not crash the parse with a
    ``TypeError`` that says nothing about SEC.

    **One bad row is skipped; every bad row is a schema change and is raised.** ``cik_str``
    and ``ticker`` are the identity, so a row without them names nothing that can be merged
    against and dropping it costs one registrant out of ten thousand. A payload in which
    *no* row has them is not a payload with bad rows in it — it is a different payload, and
    the base implementation said so by accident: it did ``int(item["cik_str"])`` and raised
    ``KeyError: 'cik_str'``, which names the column that moved.

    Skipping every row instead turned that into three quieter failures at once. The map
    came back ``{}``, so ``_resolve_cik('AAPL')`` raised ``ValueError: Unknown symbol or
    CIK: AAPL`` — a message about the caller's ticker, for a fault in SEC's file. An empty
    dict is falsy, so ``ensure_ticker_map`` never considered itself loaded and re-downloaded
    a ~1 MB index on every subsequent call, under a 10 req/s bucket. And
    ``get_13f_holdings('CIK0001067983')``, whose argument bypasses the map entirely,
    returned ``[]`` with no error at all.

    The diagnosis therefore names the cause rather than the symptom: it reports the keys the
    rows *do* carry, which is the shortest path from the exception to the renamed field.

    Raises:
        FatalConnectorError: the payload has rows and not one of them carries an identity.
            Fatal rather than transient because a retry fetches the same file.
    """
    if not isinstance(data, dict):
        return []
    rows: list[SecCompanyTicker] = []
    mappings = [item for item in data.values() if isinstance(item, dict)]
    for item in mappings:
        cik_raw, ticker_raw = item.get("cik_str"), item.get("ticker")
        if cik_raw is None or not ticker_raw:
            continue
        try:
            cik = int(cik_raw)
        except (TypeError, ValueError):
            continue
        title = item.get("title")
        rows.append(
            SecCompanyTicker(
                cik=cik,
                ticker=str(ticker_raw).upper(),
                title=str(title) if title else None,
            )
        )
    if mappings and not rows:
        observed = sorted({key for item in mappings for key in item})
        raise FatalConnectorError(
            f"{COMPANY_TICKERS_URL} returned {len(mappings)} rows and none carries both "
            f"'cik_str' and 'ticker'; the rows carry {observed}. That is a schema change at "
            f"SEC, not a bad row — skipping them all would hand back an empty registrant "
            f"index, which reads downstream as 'this ticker does not exist'."
        )
    return rows


def _safe_float(val: Any) -> float | None:
    """Parse an XBRL fact value, answering ``None`` where the filing published no number.

    It used to answer ``0.0`` for both an absent ``val`` and an unparseable one, and
    ``Fundamental.val`` is required — so a fact nobody reported went into the lake as a
    reported zero at the header's default ``prov=NATIVE``. ``SELECT sum(val) … WHERE
    tag='Revenues'`` added those zeros in and ``avg(val)`` was dragged toward zero by
    facts that were never filed, with no column on the row separating a reported zero
    from an unparsed one. The caller skips the fact instead.
    """
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_zip_chunk(
    zip_path: str | Path, filenames: list[str]
) -> list[tuple[int, dict[str, Any]]]:
    import zipfile

    import msgspec

    results = []
    with zipfile.ZipFile(zip_path, "r") as z:
        for name in filenames:
            basename = os.path.basename(name)
            cik_str = basename.replace(".json", "").replace("CIK", "")
            try:
                cik = int(cik_str)
            except ValueError:
                continue
            try:
                with z.open(name) as f:
                    content = f.read()
                    data = msgspec.json.decode(content)
                results.append((cik, data))
            except Exception:
                continue
    return results


class SecEdgarClient:
    """Client for interacting with the SEC EDGAR API and parsing XBRL facts."""

    def __init__(
        self,
        user_agent: str = "Stockodile/0.0.1 (contact@crocodile.equity.org)",
        session: aiohttp.ClientSession | None = None,
        rate_limit: float = 10.0,
    ) -> None:
        """Initialize the SEC EDGAR client.

        Args:
            user_agent: Mandatory User-Agent header (must contain AppName contact@domain).
            session: Optional pre-existing aiohttp ClientSession.
            rate_limit: Rate limit in requests per second (default 10.0).
        """
        self.user_agent = user_agent
        self.session = session
        self._limiter = TokenBucketLimiter(rate_limit, rate_limit)

        self._ticker_to_cik: dict[str, int] = {}
        self._cik_to_tickers: dict[int, list[str]] = {}
        self._cik_to_primary_ticker: dict[int, str] = {}
        self._ticker_map_loaded = False
        """Whether the registrant index has been fetched, as opposed to being non-empty.

        Two different questions, and ``ensure_ticker_map`` used to ask the second one to
        answer the first. An empty dict is falsy, so any fetch that produced no usable rows
        left the client looking un-fetched and every subsequent call re-downloaded a ~1 MB
        file under a 10 req/s bucket. Four calls, four downloads.
        """

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> SecEdgarClient:
        """Build a client whose User-Agent came from configuration rather than from a guess.

        This is the supported constructor, and it exists because the ``__init__`` default is
        not a usable one. ``crocodile.core.config.Settings.sec_user_agent`` is deliberately
        undefaulted, and its docstring states the contract this method implements: SEC
        requires the header to identify someone *contactable*, so an invented address
        satisfies the string check while giving the regulator a dead mailbox — which fails
        silently rather than loudly, and is worse than refusing to guess. The literal in
        ``__init__`` is exactly such an address; it is left in place because the ingest paths
        that predate this method still pass through it, and it is not a value any new call
        site should inherit.

        Reading ``os.environ`` here instead would be the sixteen-scattered-reads problem
        :mod:`crocodile.core.config` exists to end, one module deeper.

        The validation itself is :func:`require_user_agent`, which is also what the equity
        reference universe calls — one contract, one gate, whether the caller wants a client
        or just the string.

        Raises:
            ConfigError: ``sec_user_agent`` is unset or blank.
        """
        return cls(user_agent=require_user_agent(settings), **kwargs)

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=60.0, connect=10.0, sock_read=30.0)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def _request_bytes(self, url: str) -> bytes:
        """Fetch ``url`` and read its body, with the read inside the retry rather than after it.

        ``session.get`` returns as soon as the response *headers* arrive. The body is still
        on the wire, and ``ClientTimeout(total=60.0, sock_read=30.0)`` governs reading it as
        much as connecting: on a slow EDGAR morning the request that fails is the one that
        got a 200 and then stalled. Reading after the retry loop had already returned meant
        that failure had five unused attempts sitting above it, and a single stalled body
        ended a forty-filing batch.

        The status check keeps the shape it had — a 403 or 429 is EDGAR throttling and is
        worth backing off for — but a status the server *meant* is not retried. A 404 for a
        filing that is not there cannot become a 200 by waiting, and five backoffs before
        saying so turns a correct answer into a minute of silence. That is why
        :class:`aiohttp.ClientResponseError` is re-raised rather than falling into the
        general handler; the throttle branch raises the same type only once the budget is
        spent, having already retried.
        """
        attempts = 0
        while True:
            await self._limiter.acquire()
            headers = {"User-Agent": self.user_agent}
            resp: aiohttp.ClientResponse | None = None
            try:
                session = self._get_session()
                # `timeout` is deliberately not passed. `ClientSession._request` treats its
                # parameter as sentinel-or-override, and `None` is an override: it builds
                # `ClientTimeout(total=None)`, which is no deadline at all on any of the
                # three axes. The old spelling passed `timeout=client_timeout` with
                # `client_timeout` defaulted to `None` at both call sites, so the
                # `ClientTimeout(total=60.0, connect=10.0, sock_read=30.0)` that
                # `_get_session` builds was discarded on every request and a stalled EDGAR
                # connection hung until the socket died. Omitting the argument is what
                # applies it.
                resp = await session.get(url, headers=headers)
                if resp.status in (403, 429):
                    attempts += 1
                    if attempts > _MAX_REQUEST_ATTEMPTS:
                        resp.raise_for_status()
                else:
                    resp.raise_for_status()
                    return await resp.read()
            except aiohttp.ClientResponseError:
                raise
            except Exception:
                attempts += 1
                if attempts > _MAX_REQUEST_ATTEMPTS:
                    raise
            finally:
                if resp is not None:
                    resp.close()
            # Exponential backoff
            await asyncio.sleep(min(30.0, 1.0 * (2**attempts)))

    async def _request_json(self, url: str) -> Any:
        return msgspec.json.decode(await self._request_bytes(url))

    async def _request_text(self, url: str) -> str:
        """Fetch ``url`` as text, for the two attachments that are XML rather than JSON.

        Separate from :meth:`_request_json` rather than a flag on it: the ownership and
        information-table documents are not JSON, and ``msgspec.json.decode`` over an XML
        body raises a decode error naming a byte offset, which is a long way from "this
        filing's attachment is not where the index said".
        """
        return (await self._request_bytes(url)).decode("utf-8", errors="replace")

    async def _resolve_cik(self, symbol: str) -> int:
        """Return the CIK for a ticker or for a CIK spelled as one.

        The same two-step ``get_filings`` and ``get_fundamentals`` each do inline. It is a
        method here because the two new fetchers below would otherwise be the third and
        fourth copies, and the fallback branch is the interesting half: a caller passing
        ``CIK0001067983`` for a filer that has no ticker at all — every 13F filer that is
        not itself listed — depends on it.
        """
        await self.ensure_ticker_map()
        symbol_upper = symbol.upper()
        cik = self._ticker_to_cik.get(symbol_upper)
        if cik is not None:
            return cik
        try:
            return int(symbol_upper.replace("CIK", ""))
        except ValueError as err:
            raise ValueError(f"Unknown symbol or CIK: {symbol}") from err
    async def fetch_company_tickers(self) -> list[SecCompanyTicker]:
        """Fetch SEC's registrant index as rows, names included.

        Keyless: ``company_tickers.json`` is a static file and needs no token, only the
        User-Agent every SEC request carries. That is what lets the equity universe resolve
        without a credential, which is the half of the product promise the merge keeps
        insisting on.
        """
        return parse_company_tickers(await self._request_json(COMPANY_TICKERS_URL))

    async def fetch_ticker_map(self) -> None:
        """Fetch the ticker-to-CIK mapping from the SEC website."""
        ticker_to_cik: dict[str, int] = {}
        cik_to_tickers: dict[int, list[str]] = {}
        cik_to_primary_ticker: dict[int, str] = {}

        for row in await self.fetch_company_tickers():
            ticker_to_cik[row.ticker] = row.cik
            cik_to_tickers.setdefault(row.cik, []).append(row.ticker)

        for cik, tickers in cik_to_tickers.items():
            cik_to_primary_ticker[cik] = tickers[0]

        self._ticker_to_cik = ticker_to_cik
        self._cik_to_tickers = cik_to_tickers
        self._cik_to_primary_ticker = cik_to_primary_ticker
        self._ticker_map_loaded = True

    async def ensure_ticker_map(self) -> None:
        """Fetch the ticker-to-CIK mapping once.

        Keyed on whether the fetch happened rather than on whether it yielded anything —
        see :attr:`_ticker_map_loaded`. A fetch that raises leaves the flag down, so a
        transport failure is still retried; a fetch that legitimately returns no rows is
        not re-attempted on every lookup.
        """
        if not self._ticker_map_loaded:
            await self.fetch_ticker_map()

    async def fetch_submissions(self, cik: str | int) -> dict[str, Any]:
        """Fetch the submissions metadata JSON for a company by CIK."""
        cik_str = self.normalize_cik(cik)
        url = f"https://data.sec.gov/submissions/CIK{cik_str}.json"
        res = await self._request_json(url)
        if not isinstance(res, dict):
            raise TypeError("Expected dict from SEC submissions endpoint")
        return res

    async def fetch_company_facts(self, cik: str | int) -> dict[str, Any]:
        """Fetch the XBRL facts JSON for a company by CIK."""
        cik_str = self.normalize_cik(cik)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
        res = await self._request_json(url)
        if not isinstance(res, dict):
            raise TypeError("Expected dict from SEC company facts endpoint")
        return res

    @staticmethod
    def normalize_cik(cik: str | int) -> str:
        """Normalize a CIK to a 10-digit zero-padded string."""
        if isinstance(cik, int):
            return f"{cik:010d}"
        clean = "".join(filter(str.isdigit, cik))
        if not clean:
            raise ValueError(f"Invalid CIK: {cik}")
        return f"{int(clean):010d}"

    def _parse_filing_dict(
        self, filings_data: dict[str, Any], symbol: str, cik: int, local_ts: int
    ) -> list[Filing]:
        accession_numbers = filings_data.get("accessionNumber", [])
        forms = filings_data.get("form", [])
        filing_dates = filings_data.get("filingDate", [])
        report_dates = filings_data.get("reportDate", [])
        primary_documents = filings_data.get("primaryDocument", [])
        is_xbrl_list = filings_data.get("isXBRL", [])

        filings = []
        for i in range(len(accession_numbers)):
            accn = accession_numbers[i]
            accn_no_dashes = accn.replace("-", "")
            doc = primary_documents[i] if i < len(primary_documents) else ""
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn_no_dashes}/{doc}"
                if doc
                else ""
            )

            filings.append(
                Filing(
                    source="sec_edgar",
                    symbol=symbol,
                    symbol_raw=symbol,
                    source_ts=None,
                    local_ts=local_ts,
                    accession_number=accn,
                    form=forms[i] if i < len(forms) else "",
                    filing_date=filing_dates[i] if i < len(filing_dates) else "",
                    report_date=report_dates[i] if i < len(report_dates) else None,
                    primary_document=doc,
                    document_url=doc_url,
                    is_xbrl=bool(is_xbrl_list[i]) if i < len(is_xbrl_list) else None,
                    asset_class=AssetClass.EQUITY,
                )
            )
        return filings

    async def get_filings(self, symbol: str, include_historical: bool = False) -> list[Filing]:
        """Get the filings for a company by symbol or CIK.

        Args:
            symbol: Ticker symbol (e.g. 'AAPL') or CIK.
            include_historical: If True, fetch older submission files listed in history.
        """
        await self.ensure_ticker_map()
        symbol_upper = symbol.upper()

        cik = self._ticker_to_cik.get(symbol_upper)
        if cik is None:
            try:
                cik = int(symbol_upper.replace("CIK", ""))
            except ValueError as err:
                raise ValueError(f"Unknown symbol or CIK: {symbol}") from err

        data = await self.fetch_submissions(cik)
        local_ts = time.time_ns()

        filings = self._parse_filing_dict(
            data.get("filings", {}).get("recent", {}), symbol_upper, cik, local_ts
        )

        if include_historical:
            files = data.get("filings", {}).get("files", [])
            for file_info in files:
                filename = file_info.get("name")
                if filename:
                    url_hist = f"https://data.sec.gov/submissions/{filename}"
                    hist_data = await self._request_json(url_hist)
                    filings.extend(self._parse_filing_dict(hist_data, symbol_upper, cik, local_ts))

        return filings

    def _normalize_facts(
        self, cik: int, facts_data: dict[str, Any], local_ts: int
    ) -> Generator[Fundamental, None, None]:
        symbol = self._cik_to_primary_ticker.get(cik, f"CIK{cik:010d}")
        facts = facts_data.get("facts", {})
        for taxonomy, tags in facts.items():
            for tag, tag_data in tags.items():
                units = tag_data.get("units", {})
                for unit, values in units.items():
                    for val_obj in values:
                        fp_str = val_obj.get("fp")
                        fp = None
                        if fp_str is not None:
                            try:
                                fp = FundPeriod(fp_str)
                            except ValueError:
                                pass
                        fact_val = _safe_float(val_obj.get("val"))
                        if fact_val is None:
                            log.debug(
                                "sec_edgar: %s/%s fact for %s carries no numeric val; "
                                "skipping rather than filing a zero",
                                taxonomy,
                                tag,
                                symbol,
                            )
                            continue
                        yield Fundamental(
                            source="sec_edgar",
                            symbol=symbol,
                            symbol_raw=symbol,
                            source_ts=None,
                            local_ts=local_ts,
                            taxonomy=taxonomy,
                            tag=tag,
                            unit=unit,
                            val=fact_val,
                            end=val_obj.get("end", ""),
                            start=val_obj.get("start"),
                            fy=val_obj.get("fy"),
                            fp=fp,
                            form=val_obj.get("form"),
                            filed=val_obj.get("filed"),
                            accn=val_obj.get("accn"),
                            frame=val_obj.get("frame"),
                            asset_class=AssetClass.EQUITY,
                        )

    def _deduplicate_facts(self, facts: Iterable[Fundamental]) -> list[Fundamental]:
        """Keep the latest restatement of each fact, where *each fact* means the period.

        The key is every field that identifies **which measurement** a row is, and none
        that says what the measurement came out as: taxonomy, tag, unit, and the period
        ``(start, end, fy, fp)``. Two rows sharing all of those are the same number
        reported twice, and the later ``filed`` wins.

        ``unit`` and ``start`` are in the key because :meth:`_normalize_facts` produces
        both in multiples and neither is a restatement of the other:

        * ``units.items()`` is iterated, so ``EarningsPerShareDiluted`` arrives once in
          ``USD/shares`` and once in ``pure``, and ``NetIncomeLoss`` arrives in ``USD``
          for the consolidated entity and in ``USD`` per segment taxonomy. Collapsing a
          per-share figure onto a dollar figure keeps whichever the generator yielded
          last, which is a $1.62 answer to "what were revenues".
        * A 10-Q states duration facts over more than one window against one ``end``: a
          quarterly ``NetIncomeLoss`` with ``start`` at the quarter open and a
          year-to-date one with ``start`` at the fiscal-year open share ``end``, ``fy``,
          ``fp`` *and* ``filed``, because they are on the same page of the same filing.
          With ``start`` out of the key the survivor was decided by list order — measured
          on a nine-month/quarterly pair, ``4_308_000_000`` returned where the filer
          reported ``1_828_000_000``, a 2.36x overstatement of one quarter's earnings.

        Neither collapse announces itself: the row that survives is a real fact that the
        filer really reported, just not the one asked for, and ``deduplicate=True`` is the
        default. The alternative considered was keying on ``accn`` as well, which would be
        exact but would also defeat the purpose — a restatement is precisely a second
        accession number for the same period, and dropping the earlier one is the job.
        """
        deduped: dict[
            tuple[str, str, str, str, str | None, int | None, FundPeriod | None], Fundamental
        ] = {}
        for fact in facts:
            key = (
                fact.taxonomy,
                fact.tag,
                fact.unit,
                fact.end,
                fact.start,
                fact.fy,
                fact.fp,
            )
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = fact
            else:
                existing_filed = existing.filed or ""
                new_filed = fact.filed or ""
                if new_filed > existing_filed:
                    deduped[key] = fact
                elif new_filed == existing_filed:
                    if fact.frame and not existing.frame:
                        deduped[key] = fact
        return list(deduped.values())

    async def get_fundamentals(self, symbol: str, deduplicate: bool = True) -> list[Fundamental]:
        """Get fundamental facts for a company by symbol or CIK.

        Args:
            symbol: Ticker symbol or CIK.
            deduplicate: If True, keep only the latest restatement of each fact.
        """
        await self.ensure_ticker_map()
        symbol_upper = symbol.upper()

        cik = self._ticker_to_cik.get(symbol_upper)
        if cik is None:
            try:
                cik = int(symbol_upper.replace("CIK", ""))
            except ValueError as err:
                raise ValueError(f"Unknown symbol or CIK: {symbol}") from err

        data = await self.fetch_company_facts(cik)
        local_ts = time.time_ns()

        raw_facts = self._normalize_facts(cik, data, local_ts)
        if deduplicate:
            return self._deduplicate_facts(raw_facts)
        return list(raw_facts)

    @staticmethod
    def filing_directory(cik: int, accession_number: str) -> str:
        """Return the EDGAR archive directory a filing's attachments live in."""
        return (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_number.replace('-', '')}"
        )

    @staticmethod
    def raw_ownership_document_url(cik: int, filing: Filing) -> str:
        """Return the URL of a Form 4's *machine-readable* attachment.

        ``Filing.primary_document`` names the XSL-rendered view — ``xslF345X03/wf-form4_….xml``
        — which is served as HTML for a browser. Parsing that yields a well-formed document
        with no ``ownershipDocument`` in it, so the failure is an empty transaction list
        rather than an error, which is the shape this whole tree treats as the dangerous one.
        The raw XML sits at the same basename in the filing's root directory, so the
        rendered-view segment is stripped when there is one.
        """
        document = filing.primary_document
        head, _, tail = document.partition("/")
        if tail and head.startswith(_XSL_RENDERED_PREFIX):
            document = tail
        return f"{SecEdgarClient.filing_directory(cik, filing.accession_number)}/{document}"

    async def get_insider_transactions(
        self, symbol: str, *, limit: int = 40
    ) -> list[InsiderTransaction]:
        """Fetch and parse this issuer's recent Form 4 filings.

        Args:
            symbol: Ticker or CIK of the **issuer**, not of the insider. Form 4 is indexed
                against both, and the issuer is the side a market-data question asks from.
            limit: How many of the most recent Form 4 filings to fetch, newest first. A
                bound rather than "all of them" because each filing is its own request and a
                large issuer files hundreds a year; ``get_filings`` already returns them in
                the index's own order, which is reverse-chronological.

        Returns:
            Every Table I transaction across those filings, oldest filing last. A filing
            whose attachment cannot be parsed is logged and skipped rather than failing the
            batch: one malformed document out of forty is a gap, and raising would turn it
            into a total loss of the other thirty-nine.

            ``TimeoutError`` is caught alongside :class:`aiohttp.ClientError` and is not
            covered by it. In aiohttp 3.14.0 ``asyncio.TimeoutError`` *is*
            ``builtins.TimeoutError``, and ``issubclass(TimeoutError, aiohttp.ClientError)``
            is ``False`` — only ``ServerTimeoutError`` and its two subclasses sit under
            ``ClientError``, and the ``total`` timeout does not raise those. So the one
            network failure most likely to hit a forty-request batch was the one shape this
            handler did not cover, and it took the other thirty-nine filings with it.
        """
        cik = await self._resolve_cik(symbol)
        filings = [f for f in await self.get_filings(symbol) if f.form in FORM4_FORMS][:limit]
        local_ts = time.time_ns()

        records: list[InsiderTransaction] = []
        for filing in filings:
            url = self.raw_ownership_document_url(cik, filing)
            try:
                records.extend(parse_form4(await self._request_text(url), local_ts=local_ts))
            except (Form4ParseError, aiohttp.ClientError, TimeoutError) as exc:
                log.warning(
                    "sec_edgar: skipping Form 4 %s for %s: %s: %s",
                    filing.accession_number,
                    symbol,
                    type(exc).__name__,
                    exc,
                )
        return records

    async def get_13f_holdings(self, symbol: str, *, limit: int = 4) -> list[Holding13F]:
        """Fetch and parse a filing manager's recent 13F-HR information tables.

        Args:
            symbol: Ticker or CIK of the **manager**. Most 13F filers are not themselves
                listed, so this is usually a CIK — ``CIK0001067983`` for Berkshire Hathaway's
                filer identity — which is why :meth:`_resolve_cik` has to accept one.
            limit: How many of the most recent 13F-HR filings to fetch. Four is a year, and a
                year is the smallest window in which ``smart-money`` can difference anything:
                one information table is a position and two consecutive ones are a flow.

        Returns:
            Every reported position across those filings. As with Form 4, a filing whose
            attachments cannot be read is logged and skipped rather than failing the batch,
            and — as there — ``TimeoutError`` is listed because ``aiohttp.ClientError`` does
            not cover it.
        """
        cik = await self._resolve_cik(symbol)
        filings = [f for f in await self.get_filings(symbol) if f.form in FORM_13F_FORMS][:limit]
        local_ts = time.time_ns()

        records: list[Holding13F] = []
        for filing in filings:
            try:
                records.extend(await self._parse_one_13f(cik, filing, local_ts))
            except (
                Form13FParseError,
                aiohttp.ClientError,
                TimeoutError,
                KeyError,
                ValueError,
            ) as exc:
                log.warning(
                    "sec_edgar: skipping 13F %s for %s: %s: %s",
                    filing.accession_number,
                    symbol,
                    type(exc).__name__,
                    exc,
                )
        return records

    async def _parse_one_13f(
        self, cik: int, filing: Filing, local_ts: int
    ) -> list[Holding13F]:
        """Fetch both of a 13F-HR's attachments and parse them into holdings.

        The filing's ``index.json`` is fetched rather than the filenames being guessed. A
        cover page is reliably ``primary_doc.xml``, but the information table is named by the
        filing agent — ``form13fInfoTable.xml``, ``infotable.xml``, ``0001067983-24-000011.xml``
        and a dozen others are all live — so the only way to find it that does not silently
        return nothing for whole families of filers is to read the directory.
        """
        directory = self.filing_directory(cik, filing.accession_number)
        index = await self._request_json(f"{directory}/index.json")
        names = [
            str(item.get("name", ""))
            for item in index.get("directory", {}).get("item", [])
            if str(item.get("name", "")).lower().endswith(".xml")
        ]

        primary = next((n for n in names if n.lower() == "primary_doc.xml"), None)
        if primary is None:
            raise Form13FParseError(f"{filing.accession_number} has no primary_doc.xml")
        cover = parse_13f_primary_document(await self._request_text(f"{directory}/{primary}"))

        # Whatever else is XML and is not the cover page or EDGAR's own submission index.
        # Two candidates would mean a filing carrying two information tables, which the form
        # does not permit; taking the first keeps the failure a missing amendment rather than
        # a doubled portfolio.
        table = next(
            (n for n in names if n != primary and not n.lower().endswith("-index.xml")), None
        )
        if table is None:
            raise Form13FParseError(f"{filing.accession_number} carries no information table")

        return parse_13f_information_table(
            await self._request_text(f"{directory}/{table}"),
            cover=cover,
            filing_date=filing.filing_date,
            accession_number=filing.accession_number,
            local_ts=local_ts,
        )

    async def download_company_facts_zip(self, dest_path: str | Path) -> None:
        """Download the bulk company facts ZIP file."""
        url = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
        dest_path = Path(dest_path)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

        # Override session default timeout with a larger timeout for the bulk download
        timeout = aiohttp.ClientTimeout(total=1800.0, connect=15.0, sock_read=60.0)
        resp = await self._request(url, client_timeout=timeout)
        try:
            resp.raise_for_status()

            def _write_chunks(file_obj: Any, data_bytes: bytes) -> None:
                file_obj.write(data_bytes)

            with open(tmp_path, "wb") as f:  # noqa: ASYNC230
                buffer = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buffer.extend(chunk)
                    if len(buffer) >= 1024 * 1024:
                        await asyncio.to_thread(_write_chunks, f, bytes(buffer))
                        buffer.clear()
                if buffer:
                    await asyncio.to_thread(_write_chunks, f, bytes(buffer))

            tmp_path.rename(dest_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise
        finally:
            resp.close()

    async def parse_company_facts_zip(
        self, zip_path: str | Path, deduplicate: bool = True
    ) -> AsyncGenerator[Fundamental, None]:
        """Parse a bulk company facts ZIP file and yield Fundamental records.

        Args:
            zip_path: Path to the local companyfacts.zip file.
            deduplicate: If True, keep only the latest restatement of each company's facts.
        """
        import zipfile

        local_ts = time.time_ns()

        def _get_filenames(path: str | Path) -> list[str]:
            with zipfile.ZipFile(path, "r") as z:
                return [info.filename for info in z.infolist() if info.filename.endswith(".json")]

        filenames = await asyncio.to_thread(_get_filenames, zip_path)

        chunk_size = 100
        for i in range(0, len(filenames), chunk_size):
            chunk = filenames[i : i + chunk_size]
            parsed_chunk = await asyncio.to_thread(_parse_zip_chunk, zip_path, chunk)
            for cik, data in parsed_chunk:
                raw_facts = self._normalize_facts(cik, data, local_ts)
                if deduplicate:
                    for fact in self._deduplicate_facts(raw_facts):
                        yield fact
                else:
                    for fact in raw_facts:
                        yield fact

    async def get_fails_to_deliver(
        self,
        symbol: str,
        *,
        limit_months: int = 6,
    ) -> list[dict[str, Any]]:
        """Fetch and parse SEC Fails-To-Deliver (CNS fails) data for a symbol.

        URL: https://www.sec.gov/files/data/fails-deliver-data/cnsfails{YYYYMM}[a|b].zip
        Twice-monthly regulatory dataset of settlement failures across US clearing equities.
        """
        import io
        import zipfile
        from datetime import UTC, datetime, timedelta

        sym_clean = symbol.upper()
        now = datetime.now(UTC)
        results: list[dict[str, Any]] = []

        # Generate (year, month, half) tuples backwards from current month
        periods: list[tuple[int, int, str]] = []
        for i in range(limit_months):
            dt = now - timedelta(days=i * 30)
            periods.append((dt.year, dt.month, "b"))
            periods.append((dt.year, dt.month, "a"))

        for year, month, half in periods:
            url = f"https://www.sec.gov/files/data/fails-deliver-data/cnsfails{year}{month:02d}{half}.zip"
            try:
                body = await self._request_bytes(url)
            except Exception as exc:
                log.debug("SEC FTD file not available at %s: %s", url, exc)
                continue

            try:
                with zipfile.ZipFile(io.BytesIO(body)) as zf:
                    for name in zf.namelist():
                        raw_txt = zf.read(name).decode("latin-1")
                        for line in raw_txt.splitlines():
                            line = line.strip()
                            if (
                                not line
                                or line.startswith("SETTLEMENT DATE")
                                or line.startswith("Trailer")
                            ):
                                continue
                            parts = line.split("|")
                            if len(parts) < 6:
                                continue
                            if parts[2].strip().upper() == sym_clean:
                                date_str = parts[0].strip()
                                iso_date = (
                                    f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                                    if len(date_str) == 8
                                    else date_str
                                )
                                try:
                                    qty = float(parts[3].strip() or 0.0)
                                    px = float(parts[5].strip() or 0.0)
                                except (ValueError, TypeError):
                                    continue
                                results.append(
                                    {
                                        "settlement_date": iso_date,
                                        "cusip": parts[1].strip(),
                                        "symbol": sym_clean,
                                        "quantity": qty,
                                        "description": parts[4].strip(),
                                        "price": px,
                                        "dollar_value": round(qty * px, 2),
                                    }
                                )
            except Exception as exc:
                log.warning("Failed parsing SEC FTD archive %s: %s", url, exc)

        results.sort(key=lambda r: r["settlement_date"])
        return results
