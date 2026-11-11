"""Tiingo as a registered provider: keyed EOD and intraday bars, and no option chain.

The fourth of the four clients that had no shipped call site. Unlike the other three it
closes no gap in the read/write asymmetry: everything it can write —
:class:`~crocodile.core.schema.records.OHLCV` and the corporate actions Tiingo returns
inline — ``stooq`` and ``msn_money`` already write keylessly. What it adds is a second
opinion on the same channel from a source with a published adjustment methodology, which is
worth having and is not worth pretending is more than it is.

**Its option chain is the honest unavailable case.**
:meth:`TiingoClient.fetch_option_chain` raises ``NotImplementedError``, and that is not an
oversight to be fixed here: Tiingo's options data is a paid add-on on top of a paid plan,
and this codebase's promise is that a capability answers on free sources. So
``options_chain`` is declared unservable *with the reason*, and the channel is closed for
equities by ``yahoo``, which is keyless. Nothing about this provider is on the critical
path of the defect it was named in.

**It is keyed, and the key comes from configuration.** ``stooq`` and ``msn_money`` read
``os.environ`` inside their own constructors; this one sets
:attr:`~crocodile.equity.providers.base.Provider.wants_settings` and is handed the resolved
:class:`~crocodile.core.config.Settings` by the factory, so a surface configured from
anywhere other than the process environment is actually obeyed.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

from crocodile.core.config import Settings
from crocodile.core.errors import ConfigError
from crocodile.core.schema.enums import Channel
from crocodile.core.schema.records import Record
from crocodile.equity.providers.base import US_EQUITY_MICS, PullProvider
from crocodile.equity.providers.tiingo.client import TiingoClient

if TYPE_CHECKING:
    from crocodile.core.sink.base import Sink
    from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

__all__ = ["TiingoProvider"]

_NS_PER_SECOND: Final = 1_000_000_000


class TiingoProvider(PullProvider):
    """Pages Tiingo's daily or intraday price series into the lake.

    ``resample_freq`` is a constructor argument rather than a channel, because ``daily`` and
    ``1min`` produce the same record type over the same channel and differ only in the bar
    width — which is what :attr:`~crocodile.core.schema.records.OHLCV.interval` on the
    record already says. Spelling it as two channels would be two names for one thing, which
    is the ``bar``/``ohlcv`` mistake this schema has already had to undo once.
    """

    name = "tiingo"
    rest_url = "https://api.tiingo.com"

    wants_settings: ClassVar[bool] = True

    supported_channels: ClassVar[frozenset[str]] = frozenset(
        {Channel.OHLCV.value, Channel.CORP_ACTION.value}
    )

    supported_mics: ClassVar[frozenset[str]] = US_EQUITY_MICS
    """US daily and intraday series in this package. Non-US exchange rows are skipped
    per spec §3.6.
    """

    unservable_channels: ClassVar[dict[str, str]] = {
        Channel.OPTIONS_CHAIN.value: (
            "Tiingo's option chain is a paid add-on and `TiingoClient.fetch_option_chain` "
            "raises NotImplementedError rather than pretending otherwise. The equity "
            "options_chain channel is served keylessly by `yahoo`; nothing here is blocked "
            "on a subscription."
        ),
        Channel.QUOTE.value: (
            "the IEX endpoint this client reads returns bars, not a top of book. A quote "
            "synthesised from a bar's close would be a two-sided quote nobody posted, "
            "which is the record google_finance had removed for exactly this reason."
        ),
    }

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
        client: TiingoClient | None = None,
        settings: Settings | None = None,
        resample_freq: str = "daily",
        **_: Any,
    ) -> None:
        super().__init__(symbols, channels, out, registry)
        self.resample_freq = resample_freq
        if client is not None:
            self.client = client
        else:
            resolved = settings or Settings.from_env()
            if not (resolved.tiingo_api_key or "").strip():
                raise ConfigError(
                    "Tiingo requires an API token; set CROCODILE_TIINGO_API_KEY. The free "
                    "tier covers 500 unique symbols a month, which this client tracks. "
                    "For keyless daily bars use `stooq` instead."
                )
            self.client = TiingoClient(api_key=resolved.tiingo_api_key)

    async def close(self) -> None:
        """Release the client's session, if it opened one for itself."""
        await self.client.close()

    async def backfill(
        self,
        channel: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> AsyncIterator[Record]:
        """Yield this symbol's bars, and the splits and dividends Tiingo returns with them.

        The daily endpoint carries corporate actions inline, so a request for ``ohlcv``
        writes ``corp_action`` rows too — the same shape ``msn_money`` and ``yahoo`` have.
        The intraday endpoint carries none, so ``--resample-freq 1min`` writes bars only,
        and that is a property of the endpoint rather than a decision made here.

        Raises:
            ValueError: for a channel this provider does not serve. ``options_chain`` gets
                the argument recorded in :attr:`unservable_channels` rather than a bare
                refusal, because a caller who asked for it is asking a reasonable question.
        """
        if channel not in self.supported_channels:
            reason = self.unservable_channels.get(channel)
            raise ValueError(
                f"tiingo serves {sorted(self.supported_channels)} and not {channel!r}"
                + (f": {reason}" if reason else "")
            )
        if end_ns < start_ns:
            return
        start_date = _iso_day(start_ns)
        end_date = _iso_day(end_ns)
        if self.resample_freq == "daily":
            records: list[Record] = list(
                await self.client.get_eod_prices(symbol, start_date, end_date)
            )
        else:
            records = list(
                await self.client.get_intraday_bars(
                    symbol, start_date, end_date, resample_freq=self.resample_freq
                )
            )
        for record in records:
            yield record


def _iso_day(stamp_ns: int) -> str:
    """``YYYY-MM-DD`` in UTC, for the same reason ``yahoo``'s copy gives."""
    return datetime.fromtimestamp(stamp_ns / _NS_PER_SECOND, tz=UTC).date().isoformat()
