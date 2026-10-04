"""FINRA Market Transparency Data as a registered equity provider.

Provides canonical writers for:
- ``short_volume``: FINRA Reg SHO daily consolidated short sale & exempt volume.
- ``short_interest``: FINRA bi-weekly consolidated short interest positions.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

from crocodile.core.schema.enums import Channel
from crocodile.core.schema.records import Record
from crocodile.equity.providers.base import PullProvider
from crocodile.equity.providers.finra.client import (
    FinraClient,
    _ns_to_date,
    _parse_iso_date_to_ns,
)

if TYPE_CHECKING:
    from crocodile.core.sink.base import Sink
    from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

__all__ = ["FinraProvider"]

_NS_PER_SECOND: Final = 1_000_000_000
_MAX_NS: Final = 2**63 - 1


class FinraProvider(PullProvider):
    """Pages FINRA short volume and short interest data into the lake.

    Registered provider satisfying the ``short_volume`` and ``short_interest`` channels.
    """

    name = "finra"
    rest_url = "https://api.finra.org"

    supported_channels: ClassVar[frozenset[str]] = frozenset(
        {
            Channel.SHORT_VOLUME.value,
            Channel.SHORT_INTEREST.value,
        }
    )

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        client: FinraClient | None = None,
        **_: Any,
    ) -> None:
        super().__init__(symbols, channels, out, registry)
        self.client = client or FinraClient()

    async def close(self) -> None:
        """Release client session handles."""
        await self.client.close()

    async def backfill(
        self,
        channel: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> AsyncIterator[Record]:
        """Yield this symbol's records for ``channel`` whose date falls inside range."""
        if end_ns < start_ns:
            return

        now_date = datetime.now(UTC).date()
        start_date = _ns_to_date(start_ns) if start_ns > 0 else None
        end_date = _ns_to_date(end_ns) if end_ns < _MAX_NS else now_date

        if channel == Channel.SHORT_VOLUME.value:
            records = await self.client.get_regsho_short_volume(
                symbol, start_date=start_date, end_date=end_date
            )
            for rec in records:
                rec_ns = _parse_iso_date_to_ns(rec.date)
                if start_ns <= rec_ns <= end_ns:
                    yield rec

        elif channel == Channel.SHORT_INTEREST.value:
            si_records = await self.client.get_short_interest(
                symbol, start_date=start_date, end_date=end_date
            )
            for si_rec in si_records:
                rec_ns = _parse_iso_date_to_ns(si_rec.settlement_date)
                if start_ns <= rec_ns <= end_ns:
                    yield si_rec

        else:
            raise ValueError(f"finra serves {sorted(self.supported_channels)} and not {channel!r}")
