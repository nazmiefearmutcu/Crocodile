"""One place that knows what an environment variable means.

Crypcodile read 16 environment variables and Stockodile 23, each at its point of
use, listed nowhere. ``Settings`` replaces that with one object and one prefix.
Legacy ``CRYPCODILE_*`` / ``STOCKODILE_*`` names are honoured for one minor
version, with a deprecation warning.

``Settings`` parses exactly as much as has a wrong answer. A flag becomes a
``bool`` here, because ``bool("false")`` is ``True`` and a reader that forgets is
wrong in the direction that turns a security control quietly on. Everything else
stays the string the environment supplied: splitting a comma-separated URL list
has no ambiguity for this module to resolve, so the caller keeps it.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import msgspec

from crocodile.core.errors import ConfigError

__all__ = ["Settings"]

PREFIX: Final = "CROCODILE_"
"""The one prefix Crocodile reads."""

LEGACY_PREFIXES: Final = ("CRYPCODILE_", "STOCKODILE_")
"""Accepted for one minor version, then removed. Deprecated, not supported."""

_SECRET_SUFFIXES: Final = ("_key", "_keys", "_secret", "_token", "_password")
"""A field whose name ends in one of these carries a credential.

Deriving the redaction set from the names means a new credential is redacted the
moment it is named like one, rather than the moment someone remembers to add it
to a list.
"""


class Settings(msgspec.Struct, frozen=True):
    """Everything Crocodile reads from the environment.

    Construct with :meth:`from_env`; the defaults below are what you get when the
    environment says nothing.
    """

    data_dir: Path = Path("data")
    """Root of the local lake."""

    raw_capture: bool = False
    """Preserve public market frames and snapshot anchors; capture failure ends the run."""

    raw_capture_queue_bytes: str = "33554432"
    """Encoded raw journal admission budget, validated before live collection."""

    home: str | None = None
    """State directory for payment records and IPC files. ``None`` means ``~/.crocodile``.

    Resolving the default here would bake ``$HOME`` into the object at construction
    time; the consumer expands it. Note that the bare name ``HOME`` is never read —
    only ``CROCODILE_HOME`` and the two legacy spellings — so there is no clash with
    the POSIX variable.
    """

    alpaca_api_key: str | None = None
    """Alpaca market-data key ID."""

    alpaca_api_secret: str | None = None
    """Alpaca market-data secret."""

    alpaca_feed: str = "iex"
    """``iex`` (free) or ``sip`` (paid, full consolidated tape)."""

    finnhub_api_key: str | None = None
    """Finnhub token."""

    finnhub_free_tier: bool = True
    """Whether to apply free-tier rate limits. Defaults on, so an unkeyed deployment
    does not hammer an endpoint it has no allowance for."""

    coingecko_api_key: str | None = None
    """CoinGecko Pro key. Absent means the public rate-limited endpoint."""

    msn_money_api_key: str | None = None
    """MSN Money apikey query parameter."""

    stooq_zip_path: str | None = None
    """Path to a pre-downloaded Stooq bulk archive, bypassing the HTTP fetch."""

    tiingo_api_key: str | None = None
    """Tiingo API token. Absent means the provider refuses rather than degrades.

    Unlike :attr:`openfigi_api_key`, there is no keyless tier here: every price endpoint
    Tiingo publishes requires a token, and the free plan's limit is 500 unique symbols a
    month rather than a rate. So an absent key is not a slower answer, it is no answer, and
    :class:`~crocodile.equity.providers.tiingo.connector.TiingoProvider` raises
    :class:`~crocodile.core.errors.ConfigError` naming ``CROCODILE_TIINGO_API_KEY`` and
    pointing at ``stooq``, which serves the same channel keylessly.
    """

    openfigi_api_key: str | None = None
    """OpenFIGI mapping key. Absent means the keyless tier, which is slower and not smaller.

    The two tiers differ in throughput and in nothing else: keyless is twenty-five requests
    a minute in batches of ten, keyed is twenty-five per six seconds in batches of a hundred,
    and both return the same fields for the same ticker. So this is never the difference
    between an answer and no answer — it is the difference between how much of a universe
    the equity reference merge can enrich before it stops, and the rows it did not reach say
    so through ``reference_merge``'s confidence rather than by going missing.
    """

    sec_user_agent: str | None = None
    """Contact string sent to SEC EDGAR, which requires one and blocks requests without it.

    Deliberately undefaulted. SEC's requirement is that the User-Agent identify
    someone *contactable*, so an invented address satisfies the string check while
    giving the regulator a dead mailbox — worse than refusing to guess, because it
    fails silently. The contract for whichever task wires the EDGAR client: raise
    :class:`~crocodile.core.errors.ConfigError` when this is needed and unset, naming
    ``CROCODILE_SEC_USER_AGENT`` and what SEC expects in it.
    """

    base_rpc_url: str = "https://base-rpc.publicnode.com"
    """Single Base L2 JSON-RPC endpoint, used when ``base_rpc_urls`` is empty."""

    base_rpc_urls: str = ""
    """Comma-separated Base endpoints to rotate across.

    Left a string on purpose, unlike the boolean flags: a comma split has no wrong
    answer for this module to prevent, and how many endpoints to keep, in what order,
    and what to do when one dies is the connector's policy rather than config's.
    """

    custom_pools_ipc_file: str | None = None
    """Where the CLI and the on-chain connector exchange custom pool definitions."""

    admin_token: str | None = None
    """Bearer token for the API server's admin routes.

    Crypcodile spelled this ``ADMIN_API_KEY`` and Stockodile ``ADMIN_TOKEN``; they
    were the same setting under two names and are one name now.
    """

    metrics_token: str | None = None
    """Bearer token guarding the Prometheus metrics route."""

    api_keys: str | None = None
    """JSON object mapping provider to a list of keys, for the rotating key pool."""

    api_keys_file: str | None = None
    """Path to the same JSON, for when it is too large or too secret for the environment."""

    payments_file: str | None = None
    """Ledger of x402 payments. ``None`` means the default under :attr:`home`."""

    recipient_wallet: str = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    """Address x402 payments are sent to. The default is a well-known test account."""

    price_usdc: str = "0.001"
    """USDC charged per metered API request."""

    trust_forwarded_for: bool = False
    """Whether to believe ``X-Forwarded-For`` when rate-limiting by client IP.

    Believing it behind no proxy lets any caller forge its own address and evade
    the limiter, so it defaults off and must be turned on deliberately.
    """

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read every known name out of ``env``, defaulting to :data:`os.environ`.

        Raises:
            ConfigError: Two legacy prefixes disagree about the same setting, or a
                boolean field was given something that is not a boolean.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        values: dict[str, Any] = {}
        for field in cls.__struct_fields__:
            found = _lookup(source, _VARS[field])
            if found is None:
                continue
            spelling, raw = found
            if field in _BOOL_FIELDS:
                values[field] = _parse_bool(spelling, raw)
            elif field in _PATH_FIELDS:
                values[field] = Path(raw)
            else:
                values[field] = raw
        return cls(**values)

    @classmethod
    def known_names(cls) -> tuple[str, ...]:
        """Every environment variable Crocodile reads, in its canonical spelling."""
        return tuple(sorted(PREFIX + _VARS[field] for field in cls.__struct_fields__))

    def __repr__(self) -> str:
        """Redact credentials, so logging a ``Settings`` cannot leak one.

        msgspec's generated ``__repr__`` prints every field, which puts secrets into
        any log line, exception context, or crash dump that renders the object. A
        redacted field still reports whether it is *set*, since that is usually the
        question being debugged.
        """
        parts = [
            f"{field}=<redacted>"
            if field in _SECRET_FIELDS and getattr(self, field) is not None
            else f"{field}={getattr(self, field)!r}"
            for field in self.__struct_fields__
        ]
        return f"{type(self).__name__}({', '.join(parts)})"


_VARS: Final[Mapping[str, str]] = {
    "data_dir": "DATA_DIR",
    "raw_capture": "RAW_CAPTURE",
    "raw_capture_queue_bytes": "RAW_CAPTURE_QUEUE_BYTES",
    "home": "HOME",
    "alpaca_api_key": "ALPACA_API_KEY",
    "alpaca_api_secret": "ALPACA_API_SECRET",
    "alpaca_feed": "ALPACA_FEED",
    "finnhub_api_key": "FINNHUB_API_KEY",
    "finnhub_free_tier": "FINNHUB_FREE_TIER",
    "coingecko_api_key": "COINGECKO_API_KEY",
    "msn_money_api_key": "MSN_MONEY_API_KEY",
    "stooq_zip_path": "STOOQ_ZIP_PATH",
    "tiingo_api_key": "TIINGO_API_KEY",
    "openfigi_api_key": "OPENFIGI_API_KEY",
    "sec_user_agent": "SEC_USER_AGENT",
    "base_rpc_url": "BASE_RPC_URL",
    "base_rpc_urls": "BASE_RPC_URLS",
    "custom_pools_ipc_file": "CUSTOM_POOLS_IPC_FILE",
    "admin_token": "ADMIN_TOKEN",
    "metrics_token": "METRICS_TOKEN",
    "api_keys": "API_KEYS",
    "api_keys_file": "API_KEYS_FILE",
    "payments_file": "PAYMENTS_FILE",
    "recipient_wallet": "RECIPIENT_WALLET",
    "price_usdc": "PRICE_USDC",
    "trust_forwarded_for": "TRUST_FORWARDED_FOR",
}
"""Field name to unprefixed variable name.

Written out rather than derived by upper-casing, because the variable names are a
public contract with every existing deployment and must not follow a Python
field rename. :meth:`Settings.from_env` iterates the struct's fields and indexes
this mapping, so a field added without an entry raises ``KeyError`` on the first
read rather than being silently unreadable.
"""

_PATH_FIELDS: Final = frozenset(f.name for f in msgspec.structs.fields(Settings) if f.type is Path)
"""Fields to build a ``Path`` from, read off the declared types rather than listed.

Same for :data:`_BOOL_FIELDS`. The annotation is already the statement of what the
field is; a second hand-maintained list could only ever disagree with it.
"""

_BOOL_FIELDS: Final = frozenset(f.name for f in msgspec.structs.fields(Settings) if f.type is bool)

_SECRET_FIELDS: Final = frozenset(
    field for field in Settings.__struct_fields__ if field.endswith(_SECRET_SUFFIXES)
)

_TRUE_TOKENS: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS: Final = frozenset({"0", "false", "no", "off"})


def _parse_bool(spelling: str, raw: str) -> bool:
    """Turn an environment string into a ``bool``, or refuse to.

    A typo must not coerce to ``False``: ``trust_forwarded_for`` decides whether a
    forgeable header is believed, so silently reading ``"ture"`` as off — or a
    hypothetical truthiness check reading ``"false"`` as on — is a security control
    landing in a state nobody chose.
    """
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    accepted = ", ".join(sorted(_TRUE_TOKENS | _FALSE_TOKENS))
    raise ConfigError(f"{spelling} expects a boolean, got {raw!r}. Accepted: {accepted}.")


def _lookup(env: Mapping[str, str], name: str) -> tuple[str, str] | None:
    """Resolve one setting across the canonical prefix and the two legacy ones.

    A present ``CROCODILE_*`` wins outright and warns about nothing, so migrating is
    a matter of adding the new name rather than removing the old one.

    Returns the spelling that supplied the value alongside it, so a later parse
    failure can blame the variable the operator actually set rather than the
    canonical name they may never have heard of.
    """
    canonical = PREFIX + name
    if canonical in env:
        return canonical, env[canonical]

    legacy = {
        prefix + name: env[prefix + name] for prefix in LEGACY_PREFIXES if prefix + name in env
    }
    if not legacy:
        return None

    distinct = set(legacy.values())
    if len(distinct) > 1:
        # Naming the variables and not their values: one of these could be a
        # credential, and an exception message travels further than the object does.
        raise ConfigError(
            f"conflicting values for {canonical}: "
            f"{' and '.join(sorted(legacy))} disagree. Set {canonical} instead."
        )

    warnings.warn(
        f"{', '.join(sorted(legacy))} is deprecated and will be removed in the next "
        f"minor version; use {canonical}.",
        DeprecationWarning,
        stacklevel=3,
    )
    spelling = sorted(legacy)[0]
    return spelling, legacy[spelling]
