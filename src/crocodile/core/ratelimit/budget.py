"""Process-wide rate budgets, one TokenBucket per source and host per event loop (spec 3.9).

Why a registry: a universal run collects 80 markets through the same few sources. Yahoo's
chart endpoint, for example, is one budget for the whole process, and a bucket per market
would let eighty markets each spend it. ``budget(source, host)`` hands every caller on one
event loop the same bucket (codemap 1.3 and 4.2: "80 markets share one Yahoo budget").

Why per loop: a TokenBucket keeps its waiters and timers on the loop whose tasks wait on
it, so a bucket cannot be shared across loops. The registry therefore keys buckets by the
running loop, in a ``WeakKeyDictionary`` so a closed loop's buckets are collected with it.
Limit: two loops (for example two ``run_to_completion`` worker threads) get two budgets,
so cross-loop concurrency multiplies the rate. A universal run uses one loop.

Why a table: the defaults come from the source's published or measured pacing, not from a
per-client constant, so a new market cannot quietly lift a source's rate. Core knows nothing
about proxies or API-key pools; those compose from ``crocodile.contrib`` on the outside.
"""

from __future__ import annotations

import asyncio
import math
import threading
import weakref
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from crocodile.core.ratelimit.token_bucket import TokenBucket

__all__ = [
    "DEFAULT_BUDGETS",
    "BudgetSpec",
    "budget",
    "default_budget",
    "reset_budgets",
]


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    """The shape of one budget: a burst of ``capacity`` tokens refilled at ``refill_rate``."""

    capacity: float  # tokens, > 0
    refill_rate: float  # tokens per second, > 0

    def __post_init__(self) -> None:
        for field_name, value in (("capacity", self.capacity), ("refill_rate", self.refill_rate)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"BudgetSpec.{field_name} must be a positive finite number, got {value!r}"
                )


# Rows for the six universal sources (ruling 4). Arguments, per row:
# - yahoo_chart: keeps the legacy YahooClient refill of one token per 1.5 s
#   (equity/providers/yahoo/client.py:284) with a burst of 2 instead of 5, because one bucket
#   now serves every market in the process (task 10's brief proposed the same row).
# - tradingview: one request per second, matching the measured sweep's pacing and the spec's
#   "about 1 s" rule for an unpublished limit.
# - nasdaqtrader, nse_india, asx, twse: the four official directories, one request per second,
#   since each serves one file per day and the bucket only stops a retry loop from hammering.
# A source joins this table when its client moves onto the shared budget; legacy clients keep
# their own buckets until then.
DEFAULT_BUDGETS: Final[MappingProxyType[str, BudgetSpec]] = MappingProxyType(
    {
        "yahoo_chart": BudgetSpec(capacity=2.0, refill_rate=1.0 / 1.5),
        "tradingview": BudgetSpec(capacity=1.0, refill_rate=1.0),
        "nasdaqtrader": BudgetSpec(capacity=1.0, refill_rate=1.0),
        "nse_india": BudgetSpec(capacity=1.0, refill_rate=1.0),
        "asx": BudgetSpec(capacity=1.0, refill_rate=1.0),
        "twse": BudgetSpec(capacity=1.0, refill_rate=1.0),
    }
)

_Key = tuple[str, str]
_lock = threading.Lock()
_registry: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[_Key, tuple[BudgetSpec, TokenBucket]]
] = weakref.WeakKeyDictionary()


def _normalise(value: str, field_name: str) -> str:
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError(f"budget {field_name} must be a non-empty name")
    return cleaned


def default_budget(source: str) -> BudgetSpec:
    """The row of ``DEFAULT_BUDGETS`` for ``source``; KeyError when there is none."""
    key = _normalise(source, "source")
    try:
        return DEFAULT_BUDGETS[key]
    except KeyError:
        raise KeyError(
            f"no default budget for source {source!r}: add a row to DEFAULT_BUDGETS, "
            "or pass spec= to budget()"
        ) from None


def budget(source: str, host: str, *, spec: BudgetSpec | None = None) -> TokenBucket:
    """The shared bucket for ``(source, host)`` on the running event loop.

    Call it inside the coroutine that waits on the bucket: the bucket belongs to the loop
    that is running at the call. The first call creates the bucket from ``spec`` or from
    ``default_budget(source)``; a later call with ``spec=None`` returns it, and a later call
    with a different ``spec`` raises ValueError, since two callers disagreeing about one budget
    is a bug. Raises RuntimeError outside a running loop.
    """
    key: _Key = (_normalise(source, "source"), _normalise(host, "host"))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError(
            "budget() must be called from inside a coroutine: the bucket is bound to the event "
            "loop whose tasks wait on it"
        ) from exc

    with _lock:
        buckets = _registry.get(loop)
        if buckets is None:
            buckets = {}
            _registry[loop] = buckets
        existing = buckets.get(key)
        if existing is None:
            chosen = spec if spec is not None else default_budget(source)
            bucket = TokenBucket(capacity=chosen.capacity, refill_rate=chosen.refill_rate)
            buckets[key] = (chosen, bucket)
            return bucket

        stored_spec, bucket = existing
        if spec is not None and spec != stored_spec:
            raise ValueError(
                f"budget {key[0]}@{key[1]} already exists with {stored_spec}; "
                f"refusing a conflicting spec {spec}"
            )
        return bucket


def reset_budgets() -> None:
    """Drop every bucket on every loop. For tests."""
    with _lock:
        _registry.clear()
