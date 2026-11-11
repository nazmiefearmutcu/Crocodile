"""The shared rate budget registry: one TokenBucket per source and host per event loop."""

import asyncio
import gc
import weakref
from collections.abc import Callable, Iterator

import pytest

from crocodile.core.ratelimit import (
    DEFAULT_BUDGETS,
    BudgetSpec,
    budget,
    default_budget,
    reset_budgets,
)
from crocodile.core.ratelimit.token_bucket import TokenBucket

HOST = "scanner.tradingview.com"


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    reset_budgets()
    yield
    reset_budgets()


def _on_a_loop[T](factory: Callable[[], T]) -> T:
    async def main() -> T:
        return factory()

    return asyncio.run(main())


def test_one_bucket_per_source_and_host_on_one_loop() -> None:
    async def main() -> tuple[TokenBucket, TokenBucket]:
        return budget("tradingview", HOST), budget("tradingview", HOST)

    first, second = asyncio.run(main())
    assert first is second


async def test_two_markets_on_one_loop_spend_one_budget() -> None:
    # The refill is negligible on purpose, so the assertion does not depend on timer resolution.
    a = budget("tradingview", HOST, spec=BudgetSpec(capacity=1.0, refill_rate=0.001))
    b = budget("tradingview", HOST)

    await a.acquire()

    assert b is a
    assert b.tokens < 0.01


def test_different_hosts_get_different_buckets() -> None:
    def pair() -> tuple[TokenBucket, TokenBucket]:
        return budget("tradingview", HOST), budget("tradingview", "other.example.com")

    first, second = _on_a_loop(pair)
    assert first is not second


def test_source_and_host_are_normalised() -> None:
    def pair() -> tuple[TokenBucket, TokenBucket]:
        return budget("TradingView", " Scanner.TradingView.com "), budget(
            "tradingview", "scanner.tradingview.com"
        )

    first, second = _on_a_loop(pair)
    assert first is second


def test_defaults_come_from_the_source_table() -> None:
    def fetch() -> tuple[TokenBucket, TokenBucket]:
        return (
            budget("yahoo_chart", "query2.finance.yahoo.com"),
            budget("tradingview", HOST),
        )

    yahoo, tradingview = _on_a_loop(fetch)

    assert yahoo.capacity == 2.0
    assert yahoo.refill_rate == pytest.approx(1 / 1.5)
    assert tradingview.capacity == 1.0
    assert tradingview.refill_rate == 1.0
    for source in DEFAULT_BUDGETS:
        assert default_budget(source) == DEFAULT_BUDGETS[source]


def test_an_unknown_source_needs_an_explicit_spec() -> None:
    def unknown() -> TokenBucket:
        return budget("nobody_knows", "host.example.com")

    with pytest.raises(KeyError, match="DEFAULT_BUDGETS"):
        _on_a_loop(unknown)

    def explicit() -> TokenBucket:
        return budget("nobody_knows", "host.example.com", spec=BudgetSpec(1.0, 1.0))

    assert isinstance(_on_a_loop(explicit), TokenBucket)


def test_a_conflicting_spec_for_an_existing_bucket_is_refused() -> None:
    async def main() -> None:
        budget("tradingview", HOST)
        # The same spec is not a conflict; a different one is.
        budget("tradingview", HOST, spec=default_budget("tradingview"))
        budget("tradingview", HOST, spec=BudgetSpec(capacity=5.0, refill_rate=5.0))

    with pytest.raises(ValueError):
        asyncio.run(main())


def test_each_event_loop_gets_its_own_bucket() -> None:
    first = _on_a_loop(lambda: budget("tradingview", HOST))
    second = _on_a_loop(lambda: budget("tradingview", HOST))
    assert first is not second


def test_the_registry_does_not_keep_a_closed_loop_alive() -> None:
    refs: list[weakref.ref[asyncio.AbstractEventLoop]] = []

    async def main() -> None:
        refs.append(weakref.ref(asyncio.get_running_loop()))
        budget("tradingview", HOST)

    asyncio.run(main())
    gc.collect()

    assert refs[0]() is None


def test_budget_outside_a_running_loop_raises() -> None:
    with pytest.raises(RuntimeError, match="coroutine"):
        budget("tradingview", HOST)


def test_an_empty_source_or_host_is_refused() -> None:
    def empty_source() -> TokenBucket:
        return budget("  ", HOST)

    def empty_host() -> TokenBucket:
        return budget("tradingview", "  ")

    with pytest.raises(ValueError):
        _on_a_loop(empty_source)
    with pytest.raises(ValueError):
        _on_a_loop(empty_host)


@pytest.mark.parametrize(
    ("capacity", "refill_rate"),
    [(0.0, 1.0), (-1.0, 1.0), (1.0, 0.0), (1.0, -1.0), (float("nan"), 1.0), (1.0, float("inf"))],
)
def test_a_budget_spec_must_be_positive(capacity: float, refill_rate: float) -> None:
    with pytest.raises(ValueError):
        BudgetSpec(capacity=capacity, refill_rate=refill_rate)


async def test_a_budget_works_from_an_async_test() -> None:
    bucket = budget("nasdaqtrader", "www.nasdaqtrader.com")

    await bucket.acquire()

    assert bucket.capacity == 1.0
