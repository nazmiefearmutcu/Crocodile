from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import traceback
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Final

from crocodile.core.ingest.deadletter import DeadLetterQueue, dlq_db_path
from crocodile.core.ingest.transport import Transport
from crocodile.core.schema.records import Record

if TYPE_CHECKING:
    from crocodile.core.sink.base import Sink
    from crocodile.equity.reference.identity import InstrumentIdentity
    from crocodile.equity.reference.registry import InstrumentRegistry

log = logging.getLogger(__name__)

US_EQUITY_MICS: Final[frozenset[str]] = frozenset({"IEXG", "XCBO", "XNAS", "XNYS"})
"""Operating MICs exchanges.csv lists for country US. A literal because base.py is imported by
every connector at CLI start; tests/conformance/test_provider_mics.py asserts it equals the
table's US rows, so data and literal cannot drift (if they differ, fix the literal, never the
CSV)."""


class ProviderError(Exception):
    """Base class for provider-level errors."""


class FatalProviderError(ProviderError):
    """Non-recoverable provider error — do not reconnect (auth, config, etc.)."""


class TransientProviderError(ProviderError):
    """Recoverable provider error — reconnect with backoff is appropriate."""


class SinkError(ProviderError):
    """Failure writing to the output sink (not a bad market-data frame)."""


class UnsupportedMarketError(ValueError):
    """A provider was asked for a market outside the scope it declares (spec §3.12)."""


def backoff_delays(
    attempt: int,
    base: float = 1.0,
    cap: float = 30.0,
    jitter: float = 0.25,
    rand: float = 0.0,
) -> float:
    raw = min(cap, base * float(2**attempt))
    # Apply the cap AFTER jitter too: jitter can otherwise push a capped delay
    # above `cap` (e.g. raw=30, jitter=0.25, rand→1.0 → 37.5s), defeating the
    # documented ceiling during sustained reconnect failures.
    return min(cap, raw * (1.0 + jitter * rand))


class Provider(ABC):
    name: str
    ws_url: str
    rest_url: str

    supported_channels: ClassVar[frozenset[str] | None] = None
    """Every channel this provider can actually serve, or ``None`` for "not declared".

    ``None`` is not "all": it means this connector has not been through the exercise
    and nothing is enforced for it, which is where every connector starts. Declaring
    the set turns it into a contract — :meth:`_reject_unservable_channels` warns for
    each requested channel outside it and refuses the run when none survives, which is
    what :class:`~crocodile.equity.providers.finnhub.connector.FinnhubProvider` already
    does by hand for its tier-dependent set.

    The alternative is what ``google_finance --channels quote`` did after its ``Quote``
    was removed: poll forever, four fetches per symbol per cycle, and return nothing.
    A channel that is configured, never errors and never produces a row is
    indistinguishable from a market with nothing to report.
    """

    supported_mics: ClassVar[frozenset[str] | None] = None
    """Operating MICs (ISO 10383) this provider can answer for, or ``None`` for "not declared".

    ``None`` is never "every market", exactly as for ``supported_channels``. A declared set is a
    contract: :func:`require_supported_mic` refuses a MIC outside it, and the constructor drops
    (with a warning) every requested ``LOCAL@MIC`` symbol on a market outside it, refusing the
    run when nothing servable remains. A bare symbol is a US listing (spec §3.4), so it is
    servable exactly when the declared set contains a US MIC. The US-only sources declare
    :data:`US_EQUITY_MICS`; yahoo, google_finance and stooq are multi-market and stay
    undeclared (reasons in tests/conformance/test_provider_mics.py)."""

    unservable_channels: ClassVar[Mapping[str, str]] = MappingProxyType({})
    """Channels this provider is *asked* for and deliberately does not serve, and why.

    A decision, recorded where the decision lives. An entry here is a capability the
    connector could plausibly be expected to have and does not, with the argument —
    the same discipline :data:`crocodile.core.capability.IRREDUCIBLE` carries. The
    reason is emitted in the warning and in the refusal, so a user who asks for the
    channel is told why rather than left with an empty lake.
    """

    wants_settings: ClassVar[bool] = False
    """Whether :func:`~crocodile.equity.providers.factory.make_provider` should hand this
    connector the resolved :class:`~crocodile.core.config.Settings`.

    Opt-in rather than a parameter on every constructor, because most connectors need
    nothing from configuration and a parameter they accept and ignore is dead weight that
    reads as a promise. The two that do need it — ``sec_edgar``, whose User-Agent must name
    a contactable party, and ``tiingo``, which is keyed — are the two whose credentials a
    *surface* owns: :attr:`CapabilityContext.settings
    <crocodile.core.capability.CapabilityContext.settings>` is the resolved environment for
    the invocation, and a REST deployment configured from anywhere other than ``os.environ``
    would otherwise have its configuration quietly ignored by the ingest layer. ``stooq``
    and ``msn_money`` read ``os.environ`` directly in their own constructors, which is the
    behaviour this flag exists to stop spreading.
    """

    def __init__(
        self,
        symbols: list[str],
        channels: list[str],
        out: Sink,
        registry: InstrumentRegistry,
    ) -> None:
        self.symbols = symbols
        self.channels = channels
        self.out = out
        self.registry = registry
        self.transport: Transport | None = None
        self._dlq: DeadLetterQueue = DeadLetterQueue()
        self._reject_unservable_channels()
        self._reject_unservable_mics()

    def _reject_unservable_channels(self) -> None:
        """Warn for each requested channel this provider cannot serve; refuse if none can.

        A no-op for a connector that has not declared :attr:`supported_channels`.

        A retired tag is resolved to its successor before the check
        (:data:`~crocodile.core.schema.enums.CHANNEL_SUCCESSORS`), because ``bar`` and
        ``ohlcv`` are one channel under two spellings and a declaration that listed both
        would put two names for one thing into the set that decides the CLI's menu.
        :func:`~crocodile.equity.providers.factory.channels_for_provider` widens in the same
        direction for the same reason; before this, the menu offered ``bar`` for a connector
        whose constructor would then refuse it.

        Raises:
            ValueError: if every requested channel is unservable. Constructed here
                rather than at the first poll so the CLI reports it before opening a
                session — ``collect`` already turns a ``ValueError`` from the factory
                into a message and a non-zero exit.
        """
        if self.supported_channels is None:
            return
        from crocodile.core.schema.enums import CHANNEL_SUCCESSORS

        unservable = [
            ch
            for ch in self.channels
            if CHANNEL_SUCCESSORS.get(ch, ch) not in self.supported_channels
        ]
        for channel in unservable:
            log.warning(
                "%s cannot serve the %r channel and will emit nothing for it%s",
                self.name,
                channel,
                f": {self.unservable_channels[channel]}"
                if channel in self.unservable_channels
                else "",
            )
        if unservable and len(unservable) == len(self.channels):
            raise ValueError(
                f"{self.name} has no supported channels among {self.channels!r} "
                f"(supported: {sorted(self.supported_channels)})"
            )

    def _reject_unservable_mics(self) -> None:
        """Drop each requested symbol this provider's market scope does not serve; refuse if none.

        A no-op for a connector that has not declared :attr:`supported_mics`. A ``LOCAL@MIC``
        symbol is servable exactly when its operating MIC is declared. A bare symbol is a US
        listing (spec §3.4), so it is servable exactly when the declared set holds a US MIC; it
        is not parsed, because bare symbols include forms such as ``BINANCE:BTCUSDT`` that the
        listing parser refuses.

        Raises:
            UnsupportedMarketError: if symbols were requested and none is servable.
        """
        scope = self.supported_mics
        if scope is None:
            return
        from crocodile.core.markets.errors import MarketError
        from crocodile.core.markets.symbols import parse_listing_symbol

        servable: list[str] = []
        for symbol in self.symbols:
            if "@" in symbol:
                try:
                    _, mic = parse_listing_symbol(symbol)
                except MarketError as exc:
                    log.warning("%s will not request %r: %s", self.name, symbol, exc)
                    continue
                if mic is not None and mic in scope:
                    servable.append(symbol)
                    continue
                market = mic or "an unknown market"
            elif scope & US_EQUITY_MICS:
                servable.append(symbol)
                continue
            else:
                market = "US"
            log.warning(
                "%s serves %s and not %s; %s will not be requested",
                self.name,
                sorted(scope),
                market,
                symbol,
            )
        if self.symbols and not servable:
            raise UnsupportedMarketError(
                f"{self.name} serves {sorted(scope)} and none of the requested symbols "
                f"{self.symbols!r} is on those markets"
            )
        self.symbols = servable

    def use_durable_dlq(self, data_dir: str | os.PathLike[str]) -> Path:
        """Mirror this provider's dead letters into ``dlq_db_path(data_dir, self.name)`` (spec 3.9).

        Letters already queued in memory move into the durable queue, and letters a crashed run
        left in that file are reloaded by ``DeadLetterQueue`` itself, so the next drain reports
        them. Returns the path.
        """
        path = dlq_db_path(data_dir, self.name)
        pending = self._dlq.drain()
        durable = DeadLetterQueue(db_path=str(path))
        for item in pending:
            durable.put(item.local_ts, item.raw, item.error_type, item.traceback)
        self._dlq = durable
        return path

    @abstractmethod
    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]: ...

    @abstractmethod
    async def list_instruments(self) -> list[InstrumentIdentity]: ...

    def subscribe_channels(self) -> list[str] | list[dict[str, str]]:
        """Return the WS channel descriptors this connector will subscribe to.

        Override in concrete connectors.  Not abstract so that future connectors
        are not forced to implement it before they are ready.
        """
        raise NotImplementedError

    @abstractmethod
    async def _subscribe(self, transport: Transport) -> None:
        """Send exchange-specific subscribe frames over *transport*.

        Each exchange uses a completely different wire format for subscription
        (Deribit: JSON-RPC 2.0 ``public/subscribe``; Binance: ``{"method":
        "SUBSCRIBE", "params": [...]}``; Bybit/OKX/Coinbase differ again —
        appendix §4 table, §3.2).  This method is therefore abstract: every
        concrete connector is responsible for composing and sending its own
        subscribe frame(s).  A connector that needs no subscription (e.g. a
        pure pull source) should implement an explicit no-op.
        """

    async def backfill(
        self,
        channel: str,
        symbol: str,
        start_ns: int,
        end_ns: int,
    ) -> AsyncIterator[Record]:
        raise NotImplementedError
        yield  # pragma: no cover  (makes this an async generator)

    async def run(self, max_reconnects: int = -1) -> None:
        """Supervised run loop.

        Connects the transport, subscribes, then drains frames into the sink.
        On exception: exponential backoff then reconnect (up to max_reconnects).
        max_reconnects=-1 means unlimited; max_reconnects=0 means no reconnect.
        Unparseable frames go to the DLQ; the loop continues.
        ``transport.close()`` is always called — on clean exit and on exception
        — via a ``try/finally`` block so that socket handles are never leaked.
        """
        attempt = 0
        while True:
            transport = self.transport
            if transport is None:
                raise RuntimeError("No transport configured; set provider.transport before run()")
            try:
                await transport.connect()
                await self._subscribe(transport)
                # Successful connect/subscribe resets consecutive-failure budget
                attempt = 0

                async for raw in transport:
                    # Use standard time_ns for local timestamp
                    import time

                    local_ts = time.time_ns()
                    try:
                        msg = json.loads(raw)
                    except Exception as exc:
                        tb = traceback.format_exc()
                        self._dlq.put(local_ts, raw, type(exc).__name__, tb)
                        log.debug("DLQ: unparseable frame: %s", exc)
                        continue

                    if isinstance(msg, dict) and msg.get("error") is not None:
                        log.warning("%s: provider rejected request: %s", self.name, msg["error"])
                        continue

                    try:
                        records = list(self.normalize(msg, local_ts))
                    except FatalProviderError:
                        raise
                    except Exception as exc:
                        tb = traceback.format_exc()
                        self._dlq.put(local_ts, raw, type(exc).__name__, tb)
                        log.debug("DLQ: normalize error: %s", exc)
                        continue

                    for rec in records:
                        try:
                            await self.out.put(rec)
                        except Exception as exc:
                            # Sink failures are not bad frames — do not DLQ
                            raise SinkError(
                                f"Sink put failed for {self.name}: {exc}"
                            ) from exc

                # Transport exhausted normally (StopAsyncIteration) -> done
                break

            except FatalProviderError:
                log.error("Provider %s fatal error — not reconnecting", self.name)
                raise
            except Exception as exc:
                log.warning("Provider %s error (attempt %d): %s", self.name, attempt, exc)
                if max_reconnects == 0 or (max_reconnects > 0 and attempt >= max_reconnects):
                    raise
                delay = backoff_delays(attempt, jitter=0.25, rand=random.random())
                log.info("Reconnecting in %.2fs...", delay)
                await asyncio.sleep(delay)
                attempt += 1
            finally:
                await transport.close()


class PullProvider(Provider):
    """A source that is *fetched* rather than subscribed to, wired into the same registry.

    :class:`Provider` is a supervised websocket run loop: :meth:`run` connects a transport,
    sends a subscribe frame, and drains frames through :meth:`normalize` into the sink,
    reconnecting with backoff. Four equity sources have no wire of that shape at all — the
    Treasury publishes one CSV per calendar year, SEC EDGAR serves an index and a document
    per filing, Yahoo answers one option chain per expiry, Tiingo one price series per
    request — and for each of them ``normalize`` is ``return ()``, ``_subscribe`` is
    ``pass`` and ``list_instruments`` is the requested symbols echoed back.

    Those three no-ops are the reason all four shipped as bare clients outside
    ``factory._REGISTRY``, and the argument was recorded at the time: writing them out four
    more times is four copies of an ABC being satisfied rather than used. It was the right
    objection and the wrong conclusion. Being outside the registry is not a stylistic
    preference — it is what made ``options_chain``, ``macro_series`` and ``insider`` channels
    that eleven shipped equity capability implementations *read* and that no shipped ingest
    path could *write*, so ``iv-surface``, ``open-interest``, ``perp-basis`` and
    ``whale-alerts`` returned zero rows on every lake this product can build, and
    ``spot-future-basis`` returned a row whose ``carry_pct`` was null under a
    ``prov_confidence`` of 0.667. ``holding_13f`` was the same absence with nothing pointed
    at it: ``smart-money``'s equity half differences information tables handed to it in
    ``params``, so the channel could stay unwritten without any capability visibly failing,
    which is the version of this defect no gate over *reads* can see.

    So the three no-ops are written once, here, with the argument, and the four sources join
    the registry that ``collect`` and ``backfill`` resolve against. What remains genuinely
    per-source — which channels it serves, and how a fetch turns into records — is what each
    subclass states.

    The second half of the original objection was the load-bearing one:
    :data:`~crocodile.equity.providers.factory.VALID_CHANNELS` was a hand-written list
    offered as a menu for *every* provider, so adding ``macro_series`` to it would have
    offered ``macro_series`` for ``alpaca`` — precisely the dead channel
    ``tests/conformance/test_provider_channels.py`` exists to stop the picker walking a user
    into. That is answered on the other side: the vocabulary is now derived from what the
    registered providers declare, every registered provider declares, and the menu narrows
    to the chosen one. See :data:`~crocodile.equity.providers.factory.VALID_CHANNELS`.

    :meth:`run` stays unimplemented here. A pull source is not automatically pollable — a
    curve that is republished once per business day, or a filing index that changes when
    somebody files, is a thing to *backfill*, and a poll loop over it would spend a rate
    limit re-reading yesterday. Subclasses that genuinely have a live snapshot to
    re-read — Yahoo's option chain is one — override it; the rest inherit a refusal that
    names the verb that does work, the way ``msn_money`` already did by hand.
    """

    ws_url = ""
    """No websocket. Present because :class:`Provider` declares the attribute and
    ``collect`` reads it to decide whether to attach a transport; an empty string is what
    ``stooq``, ``msn_money`` and ``google_finance`` already carry for the same reason."""

    rest_url = ""

    def normalize(self, msg: object, local_ts: int) -> Iterable[Record]:
        """Nothing arrives unsolicited, so there is nothing to normalise."""
        return ()

    async def _subscribe(self, transport: Transport) -> None:
        """No subscription protocol exists for a source that answers one request at a time."""
        return None

    async def list_instruments(self) -> list[InstrumentIdentity]:
        """Echo the requested symbols back as identities.

        A pull source has no venue-wide instrument feed to enumerate — the caller names
        what it wants and the source answers for that name — so the honest identity set is
        the requested one. ``security_type`` is :attr:`SecurityType.UNKNOWN` rather than
        guessed from the string: these symbols are option contract ids, Treasury tenors and
        filer CIKs as often as they are tickers, and the enum has a member for "not
        established" for exactly this.
        """
        from crocodile.core.schema.enums import SecurityType
        from crocodile.equity.reference.identity import InstrumentIdentity

        return [
            InstrumentIdentity(
                symbol=symbol,
                source=self.name,
                symbol_raw=symbol,
                security_type=SecurityType.UNKNOWN,
            )
            for symbol in self.symbols
        ]

    async def run(self, max_reconnects: int = -1) -> None:
        """Refuse a live subscription and name the verb that works instead."""
        raise NotImplementedError(
            f"{self.name} publishes on request and not as a stream; there is nothing to "
            f"subscribe to. Use `backfill` with this source, which pages its history into "
            f"the same lake."
        )


def require_supported_mic(provider: type[Provider] | Provider, mic: str) -> None:
    """Refuse ``provider`` for ``mic`` when it declares a market scope that excludes it.

    ``mic`` is an operating MIC (what ``Listing.mic`` holds); segment MICs are not normalised.
    Raises UnsupportedMarketError naming the provider, the MIC and the declared set.
    """
    scope = provider.supported_mics
    if scope is None or mic in scope:
        return
    raise UnsupportedMarketError(
        f"{provider.name} does not serve {mic}; it declares {sorted(scope)}"
    )
