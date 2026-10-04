from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from crocodile.core.schema.enums import CHANNEL_SUCCESSORS, Channel
from crocodile.core.sink.base import Sink
from crocodile.equity.providers.alpaca.connector import AlpacaProvider
from crocodile.equity.providers.base import Provider
from crocodile.equity.providers.finnhub.connector import FinnhubProvider
from crocodile.equity.providers.finra.connector import FinraProvider
from crocodile.equity.providers.google_finance.connector import GoogleFinanceProvider
from crocodile.equity.providers.msn_money.connector import MsnMoneyProvider
from crocodile.equity.providers.sec_edgar.connector import SecEdgarProvider
from crocodile.equity.providers.stooq.connector import StooqProvider
from crocodile.equity.providers.tiingo.connector import TiingoProvider
from crocodile.equity.providers.treasury.connector import TreasuryProvider
from crocodile.equity.providers.yahoo.connector import YahooProvider
from crocodile.equity.reference.registry import InstrumentRegistry

if TYPE_CHECKING:
    from crocodile.core.config import Settings

_REGISTRY: dict[str, type[Provider]] = {
    "alpaca": AlpacaProvider,
    "finnhub": FinnhubProvider,
    "finra": FinraProvider,
    "stooq": StooqProvider,
    "google_finance": GoogleFinanceProvider,
    "msn_money": MsnMoneyProvider,
    "sec_edgar": SecEdgarProvider,
    "tiingo": TiingoProvider,
    "treasury": TreasuryProvider,
    "yahoo": YahooProvider,
}
"""Every equity source ``collect`` and ``backfill`` can resolve, keyed by the name a user
types.

The last four joined it late, and being outside it is what the defect *was*: eleven shipped
equity capability implementations read ``options_chain``, ``macro_series`` and ``insider``,
and no shipped ingest path could write any of them, because these four sources were plain
clients that ``make_provider`` had never heard of. See
:class:`~crocodile.equity.providers.base.PullProvider` for why they were clients and what
changed.

Imports here are eager, which is what lets :data:`VALID_CHANNELS` be *derived* from the
classes rather than hand-written. That trade only holds while a connector module is cheap
to import, so a connector whose dependencies are not — ``yahoo`` pulls ``yfinance`` and
``pandas``, about 1.2s, against a 0.46s CLI import — defers its own client import into its
constructor rather than making this registry lazy.
"""

_VALID_NAMES = sorted(_REGISTRY)


def _derive_vocabulary() -> list[str]:
    """Every channel some registered provider declares it serves, plus retired spellings.

    Derived rather than written out, and that is the change that made ``macro_series``,
    ``options_chain``, ``insider`` and ``holding_13f`` typeable at all.

    The old list was four names — ``trade``, ``quote``, ``bar``, ``ohlcv`` — and it was
    offered as the channel menu for *every* provider, including the ones that declared
    nothing. That is what made adding a channel to it dangerous: ``macro_series`` in a flat
    list is ``macro_series`` offered for ``alpaca``, which is precisely the dead channel
    ``tests/conformance/test_provider_channels.py`` exists to stop the picker walking a user
    into. The argument was recorded in ``treasury/client.py`` as a reason not to register the
    source at all, and it was a real objection to a real hazard — but the hazard is in the
    *flatness*, not in the length. A vocabulary that is the union of per-provider
    declarations, narrowed back to the chosen provider by :func:`channels_for_provider`,
    carries every channel any source serves and offers none of them for a source that does
    not. The gate that keeps it honest is that every registered provider declares; an
    undeclared one would be offered the whole union again.

    Ordering: :class:`~crocodile.core.schema.enums.Channel`'s declaration order, which groups
    the shared market-data channels first and the equity reference channels after, so a menu
    reads the way the schema does. A retired tag rides immediately behind its successor.
    """
    served: set[str] = set()
    for cls in _REGISTRY.values():
        served |= set(cls.supported_channels or ())
    retired = {
        retired_tag: successor
        for retired_tag, successor in CHANNEL_SUCCESSORS.items()
        if successor in served
    }
    ordered: list[str] = []
    for channel in Channel:
        if channel.value in served:
            ordered.append(channel.value)
            ordered.extend(
                tag for tag, successor in retired.items() if successor == channel.value
            )
    return ordered


VALID_CHANNELS: Final[list[str]] = _derive_vocabulary()
"""Channel names a caller may ask *some* equity provider for.

A vocabulary — which names a user may type — and not a read widening: a ``channel=bar/``
partition is read by asking for ``bar``. ``bar`` is in it because lakes and scripts still
spell ``ohlcv`` that way and the connectors that predate the struct collapse still answer to
it (:data:`crocodile.core.schema.enums.CHANNEL_SUCCESSORS`).

It lives here rather than in a surface because it is a fact about what the providers in
:data:`_REGISTRY` speak, and it outlived the CLI that used to hold it. It is now derived
from them rather than agreeing with them by inspection — see :func:`_derive_vocabulary`.
"""


def channels_for_provider(provider: str | None) -> list[str]:
    """Return the channels *this* provider can serve, in :data:`VALID_CHANNELS` order.

    The equity CLI's picker used to offer all four channels *after* the provider was
    chosen, with no cross-check between the two lists: ``[3] google_finance`` then
    ``[2] quote`` was a dead channel the tool walked the user into — a poll loop that
    returns nothing. A connector that declares its servable set narrows the menu; one that
    has not declared anything keeps the full vocabulary, so nothing is hidden on a guess.

    A retired tag stays on offer wherever its successor is: ``bar`` behind ``ohlcv`` and
    ``option_quote`` behind ``options_chain`` are the same channel under two spellings, and
    connectors that predate the struct collapse still accept the older one. The widening is
    over :data:`~crocodile.core.schema.enums.CHANNEL_SUCCESSORS` rather than over ``ohlcv``
    by name, because a rule written for one pair silently omits the second the day it
    appears — and it did: ``option_quote`` was in the derived vocabulary and off every menu.
    """
    servable = supported_channels(provider) if provider else None
    if servable is None:
        return list(VALID_CHANNELS)
    widened = set(servable) | {
        retired for retired, successor in CHANNEL_SUCCESSORS.items() if successor in servable
    }
    return [channel for channel in VALID_CHANNELS if channel in widened] or list(VALID_CHANNELS)


def list_providers() -> list[str]:
    """Sorted names of the registered equity providers.

    The counterpart of :func:`crocodile.crypto.exchanges.factory.list_exchanges`, and it
    exists for the same reason that one does: something outside this module needs to ask
    which sources this asset class serves. Two callers do — the ``list-exchanges``
    capability, which needs both halves to answer one question about the whole build, and
    the surface projection, which uses it to decide which market a canonical
    ``source:SYMBOL`` belongs to. Both would otherwise read ``_REGISTRY`` across a package
    boundary, which is how a rename becomes a caller's problem.

    Note this cannot be answered with :func:`supported_channels`, which returns ``None``
    both for an unknown provider and for a registered one that declares no channels.
    """
    return list(_VALID_NAMES)


def supported_channels(provider: str) -> frozenset[str] | None:
    """Return the channels ``provider`` declares it can serve, or ``None`` if it has not.

    ``None`` is "not declared", never "all" — see
    :attr:`crocodile.equity.providers.base.Provider.supported_channels`. Callers that
    offer a channel menu should narrow it with this and fall back to the full
    vocabulary when it answers ``None``, so an undeclared connector keeps working
    exactly as it did.

    The CLI needed this because its picker offered all four channels *after* the
    provider was chosen, with no cross-check: `[3] google_finance` then `[2] quote` is a
    dead channel the tool walked you into.
    """
    cls = _REGISTRY.get(provider)
    return None if cls is None else cls.supported_channels


def make_provider(
    provider: str,
    symbols: list[str],
    channels: list[str],
    out: Sink,
    registry: InstrumentRegistry,
    settings: Settings | None = None,
    **kw: Any,
) -> Provider:
    """Instantiate and return the correct Provider subclass.

    Parameters
    ----------
    provider:
        Lowercase provider name. Valid values: ``alpaca``, ``finnhub``,
        ``finra``, ``google_finance``, ``msn_money``, ``sec_edgar``, ``stooq``,
        ``tiingo``, ``treasury``, ``yahoo`` — ``_REGISTRY`` below spelled out, with
        ``tests/conformance/test_prose_counts.py`` asserting the two agree.

        The last four arrived when the argument for keeping them out was measured and
        found to cost more than it bought. It ran: they are plain HTTP clients rather
        than supervised run loops, and registering one would add its channel to
        :data:`VALID_CHANNELS`, which was the CLI's channel menu for *every* provider —
        so ``macro_series`` would be offered for ``alpaca``. Both halves were true. What
        neither accounted for is that ``collect``, ``collect-market`` and ``backfill``
        resolve their sources through this registry, so a client outside it is a client
        no shipped command can reach — and eleven equity capabilities read channels only
        those clients write. The menu was the fixable half: :data:`VALID_CHANNELS` is now
        derived from each provider's own ``supported_channels`` rather than being one
        flat list, so a channel is offered for the providers that serve it and nowhere
        else. ``openfigi`` stays out; it enriches a universe and writes no channel. See
        :mod:`crocodile.equity.providers.treasury.client` for the argument in full.
    symbols:
        List of symbol strings to subscribe to.
    channels:
        List of canonical channel names (e.g. ``"trade"``, ``"ohlcv"``). ``"bar"`` is
        still accepted by the connectors that predate the struct collapse, but it is a
        retired tag, not a canonical one, and nothing writes it any more. A connector
        that declares :attr:`~crocodile.equity.providers.base.Provider.supported_channels`
        warns for each name outside its set and refuses the run when none survives, so
        ``google_finance`` with ``["quote"]`` raises here rather than polling forever
        and returning nothing; :func:`supported_channels` answers the same question
        without constructing anything.
    out:
        Sink to receive normalised records.
    registry:
        Instrument registry for symbol resolution.
    settings:
        The resolved environment, forwarded only to connectors that declare
        :attr:`~crocodile.equity.providers.base.Provider.wants_settings`. Opt-in because
        most connectors need nothing from configuration and would carry a parameter they
        ignore; the two that do — ``sec_edgar``'s contactable User-Agent and ``tiingo``'s
        token — are credentials a *surface* owns, and passing
        :attr:`CapabilityContext.settings
        <crocodile.core.capability.CapabilityContext.settings>` here is what stops a
        deployment configured from anywhere but ``os.environ`` being quietly ignored by
        the ingest layer.
    **kw:
        Extra keyword arguments forwarded verbatim to the provider constructor.

    Raises
    ------
    ValueError
        If *provider* is not a recognised name, or if the connector declares its
        servable channels and none of *channels* is among them.
    """
    cls = _REGISTRY.get(provider)
    if cls is None:
        raise ValueError(f"Unknown provider {provider!r}. Valid names: {_VALID_NAMES}")
    if cls.wants_settings and settings is not None:
        kw.setdefault("settings", settings)
    return cls(
        symbols=symbols,
        channels=channels,
        out=out,
        registry=registry,
        **kw,
    )
