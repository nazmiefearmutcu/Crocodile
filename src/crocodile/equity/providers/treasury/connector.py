"""The Treasury par yield curve as a registered provider, so ``backfill`` can write it.

:class:`~crocodile.equity.providers.treasury.client.TreasuryYieldClient` had zero call sites
anywhere in ``src/``. Its module docstring argued — correctly, on the evidence it had — that
the source is not a websocket and that adding ``macro_series`` to a hand-written
``VALID_CHANNELS`` would offer the channel for ``alpaca``. Both objections are answered
elsewhere now (:class:`~crocodile.equity.providers.base.PullProvider` for the first,
:data:`~crocodile.equity.providers.factory.VALID_CHANNELS` for the second), and what the
argument missed is what an unreachable client costs: ``spot-future-basis``, ``funding-apr``
and ``perp-basis`` all subtract a risk-free leg read from the ``macro_series`` channel, and
in a lake no shipped path could write it, every one of those columns was null while the
provenance tail went on reporting a confidence of 0.667.

This is a backfill source and not a collect source, and that is a decision. Treasury
republishes the curve once per business day at about 3:30pm ET. A poll loop would re-read
the same CSV every interval for the twenty-three hours in which it has not changed, and
``collect``'s contract is an unbounded subscription — there is no bound at which it would
stop. ``backfill`` states a range, fetches the years the range touches, and finishes, which
is the shape of the question a curve answers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from crocodile.core.schema.enums import Channel
from crocodile.core.schema.records import MacroSeries, Record
from crocodile.equity.providers.base import US_EQUITY_MICS, PullProvider
from crocodile.equity.providers.treasury.client import (
    SOURCE,
    TreasuryYieldClient,
    parse_tenor,
    tenor_days,
)

if TYPE_CHECKING:
    from crocodile.core.sink.base import Sink
    from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

__all__ = ["CURVE_SYMBOLS", "TreasuryProvider"]

CURVE_SYMBOLS: frozenset[str] = frozenset({"*", "ALL", "CURVE", "UST"})
"""Symbols that mean "every tenor Treasury published", rather than one of them.

``backfill`` is per-symbol on both asset classes — the crypto half pages one market at a
time — but the unit Treasury publishes is a whole curve, and the tenor set is deliberately
*read* from the file rather than listed (:func:`~…treasury.client.parse_tenor`), so a caller
cannot enumerate next year's tenors in advance. Requiring them to try would mean a caller
who wrote out today's eleven silently misses the twelfth the day it appears, which is the
failure that parser exists to avoid. Naming the whole curve is therefore the *ordinary*
request and a single tenor is the narrowing.
"""

_NS_PER_SECOND = 1_000_000_000


class TreasuryProvider(PullProvider):
    """Pages the daily par yield curve into the lake as ``macro_series`` records.

    The client is injectable so the whole path — provider construction, range filtering,
    record emission — is testable against a checked-in CSV fixture with no socket. Nothing
    here reads :data:`os.environ`: the endpoint is keyless, which is why this provider does
    not set :attr:`~crocodile.equity.providers.base.Provider.wants_settings`.
    """

    name = "treasury"
    rest_url = "https://home.treasury.gov"

    supported_channels: ClassVar[frozenset[str]] = frozenset({Channel.MACRO_SERIES.value})

    supported_mics: ClassVar[frozenset[str]] = US_EQUITY_MICS
    """The US Treasury par yield curve."""

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        client: TreasuryYieldClient | None = None,
        **_: Any,
    ) -> None:
        super().__init__(symbols, channels, out, registry)
        self.client = client or TreasuryYieldClient()
        self._years: dict[int, list[MacroSeries]] = {}

    async def close(self) -> None:
        """Release the client's session. Called by the backfill orchestrator's ``finally``."""
        await self.client.close()

    async def _year(self, year: int) -> list[MacroSeries]:
        """One calendar year of the curve, fetched at most once per provider instance.

        ``backfill`` calls this provider once per requested symbol and the endpoint is
        per-year, so a caller asking for four tenors over three years would otherwise make
        twelve requests for three distinct documents. The cache is per-instance rather than
        module-level: a long-lived process must not serve today's curve from a copy it
        fetched last week.
        """
        if year not in self._years:
            self._years[year] = await self.client.par_yield_curve(year)
        return self._years[year]

    @staticmethod
    def _wanted(symbol: str) -> tuple[bool, str | None]:
        """Resolve a requested symbol into ``(whole curve?, the one tenor symbol)``.

        A tenor may be named the way this product spells it (``treasury:UST10Y``), the way
        the file's header spells it (``10 Yr``), or bare (``UST10Y``). All three resolve,
        because all three are things a user reads off something this codebase printed: the
        canonical symbol is what the lake stores, the header is what
        :attr:`~crocodile.core.schema.records.MacroSeries.symbol_raw` carries, and the bare
        form is what a CLI user types.
        """
        token = symbol.strip()
        if token.upper() in CURVE_SYMBOLS:
            return True, None
        if token.startswith(f"{SOURCE}:"):
            return False, token
        header = parse_tenor(token)
        if header is not None:
            return False, header.symbol
        candidate = f"{SOURCE}:{token.upper()}"
        return False, candidate if tenor_days(candidate) is not None else token

    async def backfill(
        self,
        channel: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> AsyncIterator[Record]:
        """Yield every published curve point in ``[start_ns, end_ns]`` for ``symbol``.

        The range is applied to ``source_ts`` — the date Treasury published — and not to
        ``local_ts``, which is when this process happened to fetch it. A backfill asked for
        2023 must not answer with today, which is the same rule
        :meth:`~…treasury.client.TreasuryYieldClient.backfill` states.

        Raises:
            ValueError: for any channel but ``macro_series``. The base class already refuses
                a construction whose channels are *all* unservable; this is the per-call
                half, and it is a refusal rather than an empty iterator because an empty
                backfill and an unservable one are the two facts this whole change exists to
                keep apart.
        """
        if channel != Channel.MACRO_SERIES.value:
            raise ValueError(
                f"treasury serves {Channel.MACRO_SERIES.value!r} and not {channel!r}; it "
                f"publishes one par yield curve and nothing else"
            )
        if end_ns < start_ns:
            return
        whole_curve, wanted = self._wanted(symbol)
        first = datetime.fromtimestamp(start_ns / _NS_PER_SECOND, tz=UTC).year
        last = datetime.fromtimestamp(end_ns / _NS_PER_SECOND, tz=UTC).year
        emitted = 0
        for year in range(first, last + 1):
            for record in await self._year(year):
                if record.source_ts is None or not start_ns <= record.source_ts <= end_ns:
                    continue
                if not whole_curve and record.symbol != wanted:
                    continue
                emitted += 1
                yield record
        if emitted == 0 and not whole_curve:
            # Named rather than silent: a mistyped tenor and a Treasury holiday are both
            # zero rows, and only one of them is the user's to fix.
            log.warning(
                "treasury: no curve point matched %r in the requested range; the tenors "
                "this source publishes are spelled like 'treasury:UST10Y' or '10 Yr', and "
                "any of %s asks for the whole curve",
                symbol,
                sorted(CURVE_SYMBOLS),
            )
