"""Declarative market-wide sources: one request that answers for a whole market.

Many exchanges publish their whole board on one page or one endpoint: a JSON array, a CSV file,
an XML feed or an HTML table of every security with its last price and, often, its best bid and
ask. Writing a module for each would be eighty near-identical parsers, so this module reads them
all from data. ``data/sites.json`` holds one :class:`SiteSpec` per publisher: where the board
is, which format it is in, how to find the rows, and which column holds which fact. The engine
turns a response into :class:`~crocodile.equity.universal.live.model.LiveSnapshot` rows.

Every spec is evidence, not configuration: it carries the date it was last seen answering, and
``tests/equity/universal/live/test_sites.py`` runs the shipped specs against captured answers.
A spec whose publisher changes its page yields no rows (or a ``LivePayloadError``); it never
yields rows from the wrong columns, because a column is addressed by its header text or key and
a missing header is refused, not guessed.

Formats.

- ``json``: ``rows`` lists dotted paths to the row arrays (``""`` is the document root). A path
  that lands on an object whose values are arrays or objects contributes all of them, which is
  how a board grouped by segment is read. Field addresses are dotted keys.
- ``csv``: ``delimiter`` and ``header_line`` find the header; field addresses are header texts.
- ``xml``: ``rows`` names the row element; a row is that element's attributes plus the text of
  its child elements; field addresses are attribute or child names.
- ``html``: the board is a ``<table>`` already present in the page source. The table is the
  first one whose header row holds every header text the spec addresses (or ``table_id`` /
  ``table_index`` when given); field addresses are header texts, or ``#N`` for the N-th cell
  where the page has no header for it. A page that fills its table with JavaScript has no rows
  in its source and is not a source at all.

Numbers. ``number`` is ``plain`` (``1,234.50``) or ``european`` (``1.234,50`` and ``1 234,50``).
Currency words, percent signs and plus signs around the number are stripped, so ``BWP 5.75``
and ``8,599 UZS`` read as numbers. Anything else that is not a number is ``None``.

Ruling: a row is kept only when its symbol is a valid local symbol and it carries at least one
price. Cost if wrong: header, subtotal and footer rows of a page would become listings.

Ruling: the engine never follows a redirect and never sends a cookie, Referer or Origin; the
fetcher enforces that. A board behind a consent or bot wall is a wall.

The module is ASCII-only, as every new file in ``equity/universal`` must be.
"""

from __future__ import annotations

import csv
import dataclasses
import functools
import importlib.resources
import io
import json
import re
import unicodedata
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar, Final, Literal

import msgspec
from bs4 import BeautifulSoup
from bs4.element import Tag

from crocodile.core.markets.errors import InvalidSymbolError
from crocodile.core.markets.exchanges import ExchangeRegistry
from crocodile.core.markets.symbols import validate_local_symbol
from crocodile.core.markets.usage import UsageClass
from crocodile.equity.universal.http import HttpRequest, HttpResponse, raise_if_unservable
from crocodile.equity.universal.live.base import LivePayloadError, LiveSource
from crocodile.equity.universal.live.model import (
    LiveListing,
    LiveSnapshot,
    LiveTier,
    clean_levels,
)

__all__ = [
    "FIELDS",
    "SiteLive",
    "SiteSpec",
    "load_specs",
    "parse_number",
    "parse_site",
    "site_source_classes",
    "spec_request",
]

FIELDS: Final[tuple[str, ...]] = (
    "symbol",
    "name",
    "isin",
    "currency",
    "last",
    "last_size",
    "open",
    "high",
    "low",
    "prev_close",
    "volume",
    "turnover",
    "bid",
    "ask",
    "bid_size",
    "ask_size",
)
_NUMERIC: Final = frozenset(FIELDS) - {"symbol", "name", "isin", "currency"}
_PRICES: Final = ("last", "bid", "ask", "prev_close", "open", "high", "low")
_SPECS_RESOURCE: Final = "data/sites.json"
_PACKAGE: Final = "crocodile.equity.universal.live"
_WS: Final = re.compile(r"\s+")
_NUMBER_BODY: Final = re.compile(r"[-+]?[0-9][0-9.,' \u00a0\u202f]*|[-+]?[.,][0-9]+")
_GROUP_RUN: Final = re.compile("[ \u00a0\u202f']+")
_GROUP_HEAD: Final = re.compile(r"[-+]?[0-9]{1,3}")
_GROUP_TAIL: Final = re.compile(r"[0-9]{3}(?:[.,][0-9]+)?")
_MAX_DATE_LOOKBACK: Final = 5


class SiteSpec(msgspec.Struct, frozen=True, kw_only=True):
    """One publisher's board, as data."""

    id: str
    """Source name of the records (``site_<id>``) and key of the fixture directory."""

    mic: str
    url: str
    """May hold ``{date}``, replaced with a recent local date in ``date_format``."""

    format: Literal["json", "csv", "xml", "html"]
    fields: dict[str, str]
    """Canonical field (one of :data:`FIELDS`) to its address in a row."""

    method: Literal["GET", "POST"] = "GET"
    json_body: dict[str, Any] | None = None
    form: dict[str, str] = {}
    rows: list[str] = []
    delimiter: str = ","
    header_line: int = 0
    encoding: str = "utf-8"
    table_id: str | None = None
    table_index: int | None = None
    header_row: int | None = None
    """For ``html``: index of the header ``<tr>``, when it is not the first row that holds
    every addressed header."""

    symbol_pattern: str | None = None
    """A regex with one group, applied to the symbol cell; rows it does not match are dropped."""

    field_patterns: dict[str, str] = {}
    """Canonical numeric field to a regex with one group, applied to its cell before the number
    is read. For a cell that holds more than one figure, such as ``"124.663 (0.237)"``."""

    row_filter: dict[str, list[str]] = {}
    """Row address to the values a row must hold there to be kept."""

    number: Literal["plain", "european"] = "plain"
    price_mult: float = 1.0
    size_mult: float = 1.0
    turnover_mult: float = 1.0
    currency: str | None = None
    """The market's single trading currency, when the board does not state one per row."""

    price_scale: int | None = None
    delay_s: int | None = None
    official: bool = True
    usage: Literal["display", "internal", "redistribution"] = "display"
    attribution: str | None = None
    date_format: str = "%Y-%m-%d"
    min_interval_s: float = 60.0
    evidence: str = ""
    """``YYYY-MM-DD`` the spec was last seen answering with rows."""

    notes: str = ""


def _norm(text: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def parse_number(value: Any, style: str = "plain") -> float | None:
    """A published number as a float, or ``None``.

    ``style`` chooses how separators are read: ``plain`` takes ``,`` as a thousands separator
    and ``.`` as the decimal point; ``european`` takes ``.``, spaces and apostrophes as
    thousands separators and ``,`` as the decimal point. Text around the number (a currency
    word, ``%``, ``+``) is ignored. A cell with two separate numbers is not a number.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    if not isinstance(value, str):
        return None
    found = _NUMBER_BODY.findall(value)
    if len(found) != 1:
        return None
    body = found[0].strip()
    if _GROUP_RUN.search(body):
        # A space or apostrophe inside the body is a thousands separator only when it groups
        # three digits; "12 34" is two numbers, not 1234.
        head, *tail = _GROUP_RUN.split(body)
        if not _GROUP_HEAD.fullmatch(head) or not all(_GROUP_TAIL.fullmatch(p) for p in tail):
            return None
    for space in (" ", "\u00a0", "\u202f", "'"):
        body = body.replace(space, "")
    if style == "european":
        body = body.replace(".", "").replace(",", ".")
    else:
        body = body.replace(",", "")
    try:
        number = float(body)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _dig(row: Any, address: str) -> Any:
    current = row
    for key in address.split("."):
        if isinstance(current, Mapping):
            current = current.get(key)
        else:
            return None
    return current


def _json_rows(spec: SiteSpec, body: bytes) -> list[Mapping[str, Any]]:
    try:
        document = json.loads(body.decode(spec.encoding, errors="replace").lstrip("\ufeff"))
    except ValueError as exc:
        raise LivePayloadError(spec.id, f"body is not JSON: {exc}") from exc
    out: list[Mapping[str, Any]] = []
    found_any = False
    for path in spec.rows or [""]:
        node = document if path == "" else _dig(document, path)
        if node is None:
            continue
        found_any = True
        groups = list(node.values()) if isinstance(node, Mapping) else [node]
        if isinstance(node, list):
            groups = [node]
        for group in groups:
            if isinstance(group, list):
                out.extend(item for item in group if isinstance(item, Mapping))
            elif isinstance(group, Mapping):
                out.append(group)
    if not found_any:
        raise LivePayloadError(spec.id, f"none of the row paths {spec.rows!r} exist")
    return out


def _csv_rows(spec: SiteSpec, body: bytes) -> list[Mapping[str, Any]]:
    text = body.decode(spec.encoding, errors="replace").lstrip("\ufeff")
    lines = text.splitlines()
    if len(lines) <= spec.header_line:
        raise LivePayloadError(spec.id, "the CSV has no header line")
    reader = csv.reader(io.StringIO("\n".join(lines[spec.header_line :])), delimiter=spec.delimiter)
    try:
        header = [cell.strip() for cell in next(reader)]
    except StopIteration as exc:
        raise LivePayloadError(spec.id, "the CSV is empty") from exc
    _require_headers(spec, header)
    _refuse_repeated_headers(spec, header)
    return [dict(zip(header, cells, strict=False)) for cells in reader if cells]


def _xml_rows(spec: SiteSpec, body: bytes) -> list[Mapping[str, Any]]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise LivePayloadError(spec.id, f"body is not XML: {exc}") from exc
    tags = spec.rows or []
    if not tags:
        raise LivePayloadError(spec.id, "an xml spec needs the row element name in rows")
    out: list[Mapping[str, Any]] = []
    for element in root.iter(tags[0]):
        row: dict[str, Any] = dict(element.attrib)
        for child in element:
            if child.text is not None and child.tag not in row:
                row[child.tag] = child.text
        out.append(row)
    return out


def _addressed_headers(spec: SiteSpec) -> list[str]:
    addresses = list(spec.fields.values()) + list(spec.row_filter)
    return [address for address in addresses if not address.startswith("#")]


def _require_headers(spec: SiteSpec, header: Sequence[str]) -> None:
    have = {_norm(cell) for cell in header}
    missing = [name for name in _addressed_headers(spec) if _norm(name) not in have]
    if missing:
        raise LivePayloadError(spec.id, f"the board has no column named {missing[0]!r}")


def _refuse_repeated_headers(spec: SiteSpec, header: Sequence[str]) -> None:
    """A header text that names two columns is ambiguous: refused, never resolved by position."""
    names = [_norm(cell) for cell in header]
    for name in _addressed_headers(spec):
        if names.count(_norm(name)) > 1:
            raise LivePayloadError(spec.id, f"the board repeats the column {name!r}")


_CELL_TAGS: Final = ("td", "th")
_ROW_BOUNDARY: Final = frozenset({"td", "th", "tr", "table"})


def _own_text(node: Tag) -> str:
    """Text of a cell, without the text of any cell, row or table nested inside it.

    ``html.parser`` does not close an unclosed ``<td>`` or ``<tr>``, so on a page that omits the
    end tags every later cell and row is nested inside the first one. Stopping at the next
    cell, row or table boundary gives each cell its own text on both kinds of page.
    """
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, Tag):
            if child.name in _ROW_BOUNDARY:
                continue
            parts.append(_own_text(child))
        else:
            parts.append(str(child))
    return " ".join(parts)


def _cells(row: Tag) -> list[str]:
    out: list[str] = []
    for cell in row.find_all(list(_CELL_TAGS)):
        if not isinstance(cell, Tag) or cell.find_parent("tr") is not row:
            continue
        out.append(_WS.sub(" ", _own_text(cell)).strip())
    return out


def _table_rows(table: Tag) -> list[Tag]:
    return [
        row
        for row in table.find_all("tr")
        if isinstance(row, Tag) and row.find_parent("table") is table
    ]


def _html_rows(spec: SiteSpec, body: bytes) -> list[Mapping[str, Any]]:
    soup = BeautifulSoup(body.decode(spec.encoding, errors="replace"), "html.parser")
    tables = [table for table in soup.find_all("table") if isinstance(table, Tag)]
    wanted = [_norm(name) for name in _addressed_headers(spec)]
    chosen: Tag | None = None
    header_index = 0
    candidates: list[Tag]
    if spec.table_id is not None:
        candidates = [table for table in tables if table.get("id") == spec.table_id]
    elif spec.table_index is not None:
        candidates = tables[spec.table_index : spec.table_index + 1]
    else:
        candidates = tables
    # Positional addresses only, and no header row named: the table has no header, so its
    # first row is data. Taking row 0 as a header would drop a real row without a word.
    headerless = not wanted and spec.header_row is None
    for table in candidates:
        rows = _table_rows(table)
        if headerless:
            if rows:
                chosen, header_index = table, -1
                break
            continue
        if spec.header_row is not None:
            probes: Iterator[int] = iter([spec.header_row])
        else:
            probes = iter(range(min(len(rows), 4)))
        for index in probes:
            if index >= len(rows):
                continue
            have = {_norm(cell) for cell in _cells(rows[index])}
            if all(name in have for name in wanted):
                chosen, header_index = table, index
                break
        if chosen is not None:
            break
    if chosen is None:
        raise LivePayloadError(spec.id, "no table in the page holds the addressed columns")
    rows = _table_rows(chosen)
    header = [] if header_index < 0 else _cells(rows[header_index])
    data = rows if header_index < 0 else rows[header_index + 1 :]
    out: list[Mapping[str, Any]] = []
    for row in data:
        cells = _cells(row)
        if not cells:
            continue
        record: dict[str, Any] = {f"#{index}": cell for index, cell in enumerate(cells)}
        for index, name in enumerate(header):
            if index < len(cells) and name not in record:
                record[name] = cells[index]
        out.append(record)
    return out


def _lookup(spec: SiteSpec, row: Mapping[str, Any], address: str) -> Any:
    if spec.format == "json":
        return _dig(row, address)
    if address in row:
        return row[address]
    wanted = _norm(address)
    for key, value in row.items():
        if _norm(str(key)) == wanted:
            return value
    return None


def _symbol(spec: SiteSpec, raw: Any, pattern: re.Pattern[str] | None) -> str | None:
    if not isinstance(raw, str):
        if isinstance(raw, int) and not isinstance(raw, bool):
            raw = str(raw)
        else:
            return None
    text = raw.strip()
    if pattern is not None:
        match = pattern.search(text)
        if match is None:
            return None
        text = match.group(1).strip()
    try:
        return validate_local_symbol(text)
    except InvalidSymbolError:
        return None


def parse_site(spec: SiteSpec, body: bytes, *, captured_ns: int) -> list[LiveSnapshot]:
    """Every row of one board answer as a snapshot, first row of a symbol winning."""
    if spec.format == "json":
        rows = _json_rows(spec, body)
    elif spec.format == "csv":
        rows = _csv_rows(spec, body)
    elif spec.format == "xml":
        rows = _xml_rows(spec, body)
    else:
        rows = _html_rows(spec, body)
    pattern = re.compile(spec.symbol_pattern) if spec.symbol_pattern is not None else None
    cell_patterns = {field: re.compile(text) for field, text in spec.field_patterns.items()}
    source = f"site_{spec.id}"
    seen: set[str] = set()
    out: list[LiveSnapshot] = []
    for row in rows:
        if any(
            str(_lookup(spec, row, address)).strip() not in allowed
            for address, allowed in spec.row_filter.items()
        ):
            continue
        raw_symbol = _lookup(spec, row, spec.fields["symbol"])
        local = _symbol(spec, raw_symbol, pattern)
        if local is None or local in seen:
            continue
        numbers: dict[str, float | None] = {}
        for field in _NUMERIC:
            address = spec.fields.get(field)
            cell = _lookup(spec, row, address) if address else None
            cell_pattern = cell_patterns.get(field)
            if cell_pattern is not None:
                found = cell_pattern.search(cell) if isinstance(cell, str) else None
                cell = found.group(1) if found is not None else None
            value = parse_number(cell, spec.number)
            if value is not None and field in _PRICES:
                value = value * spec.price_mult if value > 0.0 else None
            elif value is not None and field in ("volume", "last_size", "bid_size", "ask_size"):
                value = value * spec.size_mult if value >= 0.0 else None
            elif value is not None and field == "turnover":
                value = value * spec.turnover_mult if value >= 0.0 else None
            numbers[field] = value
        if all(numbers[field] is None for field in _PRICES):
            continue
        texts: dict[str, str | None] = {}
        for field in ("name", "isin", "currency"):
            address = spec.fields.get(field)
            cell = _lookup(spec, row, address) if address else None
            texts[field] = cell.strip() if isinstance(cell, str) and cell.strip() else None
        last = numbers["last"]
        bid, ask = numbers["bid"], numbers["ask"]
        if bid is not None and ask is not None and bid >= ask:
            # A board row whose bid is not below its ask is not a continuous-trading top of
            # book: it is an auction state or a mislabelled column. Neither side is kept.
            bid = ask = None
        seen.add(local)
        out.append(
            LiveSnapshot(
                source=source,
                listing=LiveListing(mic=spec.mic, local_symbol=local, isin=texts["isin"]),
                symbol_raw=str(raw_symbol).strip(),
                captured_ns=captured_ns,
                last=last,
                last_size=numbers["last_size"] if last is not None else None,
                prev_close=numbers["prev_close"],
                open=numbers["open"],
                high=numbers["high"],
                low=numbers["low"],
                volume=numbers["volume"],
                turnover=numbers["turnover"],
                bids=clean_levels([(bid, numbers["bid_size"])], descending=True),
                asks=clean_levels([(ask, numbers["ask_size"])], descending=False),
                currency=texts["currency"] if "currency" in spec.fields else spec.currency,
                price_scale=spec.price_scale,
                delay_s=spec.delay_s,
                name=texts["name"],
            )
        )
    return out


def _recent_dates(spec: SiteSpec, captured_ns: int) -> list[date]:
    tz = ExchangeRegistry().get(spec.mic).tz
    today = datetime.fromtimestamp(captured_ns / 1_000_000_000, tz=UTC).astimezone(tz).date()
    return [today - timedelta(days=back) for back in range(_MAX_DATE_LOOKBACK + 1)]


def spec_request(spec: SiteSpec, day: date | None = None) -> HttpRequest:
    """The one request a spec makes; ``day`` fills a ``{date}`` placeholder."""
    url = spec.url
    if "{date}" in url:
        if day is None:
            raise ValueError(f"spec {spec.id} needs a date")
        url = url.replace("{date}", day.strftime(spec.date_format))
    return HttpRequest(
        source=f"site_{spec.id}",
        method=spec.method,
        url=url,
        json_body=spec.json_body,
        form=tuple(spec.form.items()),
    )


@functools.cache
def load_specs() -> tuple[SiteSpec, ...]:
    """The shipped specs, validated: unique ids, known fields, an allow-listed market."""
    raw = importlib.resources.files(_PACKAGE).joinpath(_SPECS_RESOURCE).read_bytes()
    specs = tuple(msgspec.json.decode(raw, type=list[SiteSpec]))
    registry = ExchangeRegistry()
    ids: set[str] = set()
    for spec in specs:
        if spec.id in ids:
            raise ValueError(f"duplicate site spec id {spec.id!r}")
        ids.add(spec.id)
        registry.get(spec.mic)
        unknown = sorted(set(spec.fields) - set(FIELDS))
        if unknown:
            raise ValueError(f"site spec {spec.id!r} maps unknown fields {unknown}")
        if "symbol" not in spec.fields:
            raise ValueError(f"site spec {spec.id!r} has no symbol field")
        if spec.json_body is not None and spec.form:
            raise ValueError(f"site spec {spec.id!r} has both a JSON and a form body")
        patterns = list(spec.field_patterns.items())
        if spec.symbol_pattern is not None:
            patterns.append(("symbol", spec.symbol_pattern))
        for field, text in patterns:
            if field != "symbol" and field not in _NUMERIC:
                raise ValueError(f"site spec {spec.id!r} has a pattern for {field!r}")
            if re.compile(text).groups != 1:
                raise ValueError(f"site spec {spec.id!r} pattern for {field!r} needs one group")
    return specs


_USAGE: Final[Mapping[str, UsageClass]] = {
    "display": UsageClass.DISPLAY,
    "internal": UsageClass.INTERNAL,
    "redistribution": UsageClass.REDISTRIBUTION,
}


class SiteLive(LiveSource):
    """A live source driven by one :class:`SiteSpec`. Subclassed per spec by the registry."""

    spec: ClassVar[SiteSpec]
    enumerates: ClassVar[bool] = True
    batch_size: ClassVar[int] = 100_000

    def __init__(self, fetch: Any = None) -> None:
        super().__init__(fetch)
        self._board: tuple[int, list[LiveSnapshot]] | None = None

    async def board(
        self, mic: str, *, captured_ns: int, limit: int | None = None
    ) -> list[LiveSnapshot]:
        """Every row of the publisher's board. Reused for ``min_interval_s`` between requests."""
        if mic != self.spec.mic:
            return []
        cached = self._board
        if cached is not None:
            age_s = (captured_ns - cached[0]) / 1_000_000_000
            if 0.0 <= age_s < self.spec.min_interval_s:
                return cached[1]
        rows = await self._fetch_board(captured_ns)
        self._board = (captured_ns, rows)
        return rows

    async def _fetch_board(self, captured_ns: int) -> list[LiveSnapshot]:
        spec = self.spec
        days: list[date | None] = [None]
        if "{date}" in spec.url:
            days = list(_recent_dates(spec, captured_ns))
        last_error: LivePayloadError | None = None
        for day in days:
            response = await self._fetch(spec_request(spec, day))
            self._check_board(response)
            if not 200 <= response.status <= 299 or not response.body:
                last_error = LivePayloadError(self.name, f"HTTP {response.status}")
                continue
            try:
                rows = parse_site(spec, response.body, captured_ns=captured_ns)
            except LivePayloadError as exc:
                last_error = exc
                continue
            if rows or day is None:
                return rows
        if last_error is not None:
            raise last_error
        return []

    def _check_board(self, response: HttpResponse) -> None:
        if self.spec.format == "html":
            if response.status in (401, 403, 451) or 300 <= response.status <= 399:
                raise_if_unservable(response, source=self.name, expect="text")
            return
        if self.spec.format in ("json", "csv"):
            # A 2xx HTML page where data was expected is a bot or consent wall, not a payload
            # error: it must not be walked back over by a {date} spec.
            raise_if_unservable(
                response, source=self.name, expect="json" if self.spec.format == "json" else "text"
            )
            return
        if response.status in (401, 403, 451) or 300 <= response.status <= 399:
            raise_if_unservable(response, source=self.name, expect="json")

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        wanted = {listing.local_symbol: listing for listing in listings if self.serves(listing)}
        if not wanted:
            return []
        out: list[LiveSnapshot] = []
        for row in await self.board(self.spec.mic, captured_ns=captured_ns):
            listing = wanted.get(row.listing.local_symbol)
            if listing is not None:
                out.append(dataclasses.replace(row, listing=listing))
        return out


def _class_for(spec: SiteSpec) -> type[SiteLive]:
    has_book = "bid" in spec.fields or "ask" in spec.fields
    attributes: dict[str, Any] = {
        "spec": spec,
        "name": f"site_{spec.id}",
        "usage_class": _USAGE[spec.usage],
        "attribution": spec.attribution,
        "official": spec.official,
        "supported_mics": frozenset({spec.mic}),
        "max_tier": LiveTier.L1 if has_book else LiveTier.LAST,
        "depth_levels": 1 if has_book else 0,
        "delay_s": spec.delay_s,
        "has_tape": "last_size" in spec.fields,
        "min_interval_s": spec.min_interval_s,
        "__doc__": f"Board of {spec.mic} read from {spec.url} ({spec.format}).",
    }
    return type(f"Site_{spec.id}", (SiteLive,), attributes)


@functools.cache
def site_source_classes() -> tuple[type[SiteLive], ...]:
    """One ``SiteLive`` subclass per shipped spec, in file order."""
    return tuple(_class_for(spec) for spec in load_specs())
