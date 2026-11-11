"""SEC EDGAR as a registered provider: Form 4, 13F-HR, the filing index and XBRL facts.

:class:`~crocodile.equity.providers.sec_edgar.client.SecEdgarClient` could parse a Form 4
into an :class:`~crocodile.core.schema.records.InsiderTransaction` and a 13F-HR information
table into a :class:`~crocodile.core.schema.records.Holding13F` long before anything could
put either into the lake. ``whale-alerts`` for equities reads the ``insider`` channel, so it
answered zero rows on every lake this product could build — not because the parser was wrong
but because ``collect`` and ``backfill`` resolve against ``providers.factory._REGISTRY`` and
the client was not in it.

``holding_13f`` is the same absence one step further out. ``smart-money`` differences
consecutive information tables, but it takes them in ``params`` rather than reading them, so
nothing failed visibly while the channel had no writer — a caller simply had to obtain the
tables from somewhere that was not this product. That is why the end-to-end test for this
channel reads back through ``catalog-scan``: the round trip has to end somewhere, and where
it ends is a capability rather than a fixture.

**Backfill and not collect.** A filing is published when somebody files it, and EDGAR's
index is the record of that. There is no stream, and a poll loop would spend the ten
requests per second SEC allows re-reading an index that changes a few times a day per
issuer. ``backfill`` states a range and finishes, which is what a filing history is.

**The range is applied to the business date, not to** ``source_ts``. Neither
:func:`~…sec_edgar.form4.parse_form4` nor
:func:`~…sec_edgar.form13f.parse_13f_information_table` sets ``source_ts``, and both are
right not to: a Form 4 stamps calendar dates and never a time of day, so an instant there
would be a claim the document does not make. What the documents *do* state is on the
records — ``transaction_date`` on a Form 4 line and ``report_date`` on a 13F position — so
those are what a requested range filters. A row whose business date is unreadable is kept
rather than dropped, because dropping it would answer "this filing does not exist" to a
question about a range it may well fall inside.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

from crocodile.core.config import Settings
from crocodile.core.schema.enums import Channel
from crocodile.core.schema.records import Record
from crocodile.equity.providers.base import US_EQUITY_MICS, PullProvider
from crocodile.equity.providers.sec_edgar.client import SecEdgarClient

if TYPE_CHECKING:
    from crocodile.core.sink.base import Sink
    from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

__all__ = ["SecEdgarProvider"]

_NS_PER_SECOND: Final = 1_000_000_000

_DATE_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    Channel.INSIDER.value: ("transaction_date",),
    Channel.HOLDING_13F.value: ("report_date",),
    Channel.FILING.value: ("report_date", "filing_date"),
    Channel.FUNDAMENTAL.value: ("end",),
}
"""Which field carries the business date each channel's range filter applies to, best first.

``filing`` lists two because the two answer different questions and only one of them is
always present: ``report_date`` is the period the filing is *about* and is what a caller
asking for "2023" means, while ``filing_date`` is when it became public and is the only one
an 8-K carries.
"""


def _iso_to_ns(raw: object) -> int | None:
    """UTC-midnight nanoseconds for a ``YYYY-MM-DD`` string, or ``None`` if unreadable.

    Midnight for the same reason ``stooq`` uses it for a daily bar and the Treasury parser
    for a curve date: the document states a date and no time of day, and a date-to-instant
    conversion is total in this direction.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.strptime(raw.strip()[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(parsed.timestamp()) * _NS_PER_SECOND


class SecEdgarProvider(PullProvider):
    """Pages one issuer's or manager's filings into the lake, one channel per call.

    The client is injectable so every path here is testable against checked-in filing
    fixtures with no socket, and :attr:`wants_settings` is set because SEC requires a
    User-Agent that names a contactable party. ``Settings`` is asked for that rather than
    ``os.environ``: which User-Agent this deployment files under is the surface's to know,
    and :meth:`SecEdgarClient.from_settings` already refuses to invent one.
    """

    name = "sec_edgar"
    rest_url = "https://data.sec.gov"

    wants_settings: ClassVar[bool] = True

    supported_channels: ClassVar[frozenset[str]] = frozenset(
        {
            Channel.INSIDER.value,
            Channel.HOLDING_13F.value,
            Channel.FILING.value,
            Channel.FUNDAMENTAL.value,
        }
    )

    supported_mics: ClassVar[frozenset[str]] = US_EQUITY_MICS
    """US filings."""

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        client: SecEdgarClient | None = None,
        settings: Settings | None = None,
        form4_limit: int = 40,
        form13f_limit: int = 4,
        **_: Any,
    ) -> None:
        super().__init__(symbols, channels, out, registry)
        self.client = client or SecEdgarClient.from_settings(settings or Settings.from_env())
        self.form4_limit = form4_limit
        self.form13f_limit = form13f_limit

    async def close(self) -> None:
        """Release the client's session. Called by the backfill orchestrator's ``finally``."""
        await self.client.close()

    def _fetch(
        self, channel: str, symbol: str
    ) -> Callable[[], Coroutine[Any, Any, Sequence[Record]]]:
        """The client call that answers ``channel`` for ``symbol``, unstarted.

        A dispatch table and not an ``if`` chain, so the set of channels this provider can
        actually answer is one object rather than a control-flow shape, and
        :attr:`supported_channels` can be checked against it by a test rather than by
        reading.
        """
        table: dict[str, Callable[[], Coroutine[Any, Any, Sequence[Record]]]] = {
            Channel.INSIDER.value: lambda: self.client.get_insider_transactions(
                symbol, limit=self.form4_limit
            ),
            Channel.HOLDING_13F.value: lambda: self.client.get_13f_holdings(
                symbol, limit=self.form13f_limit
            ),
            Channel.FILING.value: lambda: self.client.get_filings(symbol),
            Channel.FUNDAMENTAL.value: lambda: self.client.get_fundamentals(symbol),
        }
        try:
            return table[channel]
        except KeyError:
            raise ValueError(
                f"sec_edgar serves {sorted(self.supported_channels or ())} and not "
                f"{channel!r}"
            ) from None

    @staticmethod
    def _in_range(record: Record, channel: str, start_ns: int, end_ns: int) -> bool:
        """Whether ``record``'s business date falls inside the requested range.

        A record whose date fields are all absent or unreadable is *kept*. The alternative
        would answer "no such filing" to a question about a window it may well fall inside,
        and the record still carries every date it stated, so a caller who needs a stricter
        window can apply one to data it can see rather than to data it never received.
        """
        for field in _DATE_FIELDS.get(channel, ()):
            stamped = _iso_to_ns(getattr(record, field, None))
            if stamped is not None:
                return start_ns <= stamped <= end_ns
        return True

    async def backfill(
        self,
        channel: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> AsyncIterator[Record]:
        """Yield this symbol's records for ``channel`` whose business date is in range.

        ``symbol`` is a ticker or a CIK, and which entity it names depends on the channel:
        ``insider`` is indexed against the *issuer* (the side a market-data question asks
        from) while ``holding_13f`` is indexed against the *manager*, most of whom are not
        themselves listed — ``CIK0001067983`` is Berkshire's filer identity. Both spellings
        resolve; see :meth:`SecEdgarClient._resolve_cik`.

        Raises:
            ValueError: for a channel this provider does not serve.
        """
        fetch = self._fetch(channel, symbol)
        if end_ns < start_ns:
            return
        for record in await fetch():
            if self._in_range(record, channel, start_ns, end_ns):
                yield record
