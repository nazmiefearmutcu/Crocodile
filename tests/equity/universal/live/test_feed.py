"""The live feed: chain walk, cadence floor, walls, faults, batching, identify, board and bars."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

import pytest

from crocodile.core.schema.enums import Side
from crocodile.core.schema.provenance import Provenance
from crocodile.core.schema.records import Trade
from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.http import HttpRequest, HttpResponse
from crocodile.equity.universal.live import feed as feed_module
from crocodile.equity.universal.live.base import LivePayloadError, LiveSource
from crocodile.equity.universal.live.feed import LiveFeed, listing_of, volume_delta_trade
from crocodile.equity.universal.live.model import (
    LiveLevel,
    LiveListing,
    LivePrint,
    LiveSnapshot,
    LiveTier,
)
from crocodile.equity.universal.live.sources import tradingview
from crocodile.equity.universal.live.sources.yahoo_chart import YahooChartLive

T0 = 1_794_471_000_000_000_000
NS = 1_000_000_000
XIST = frozenset({"XIST"})
THYAO = LiveListing(mic="XIST", local_symbol="THYAO")
ASELS = LiveListing(mic="XIST", local_symbol="ASELS")
THYAO_ISIN = LiveListing(mic="XIST", local_symbol="THYAO", isin="TRTHYAO91M5")

Responder = Callable[[Sequence[LiveListing], int], list[LiveSnapshot]]
BoardAnswer = Callable[[str, int, int | None], list[LiveSnapshot]]


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += int(seconds * NS)


async def _no_network(request: HttpRequest) -> HttpResponse:
    raise AssertionError(f"tests never touch the network: {request.url}")


def snap(
    source: str,
    listing: LiveListing,
    captured_ns: int,
    *,
    last: float | None = 10.0,
    volume: float | None = None,
    bids: tuple[LiveLevel, ...] = (),
    asks: tuple[LiveLevel, ...] = (),
    last_size: float | None = None,
    prints: tuple[LivePrint, ...] = (),
    currency: str | None = "TRY",
    price_scale: int | None = 2,
) -> LiveSnapshot:
    return LiveSnapshot(
        source=source,
        listing=listing,
        symbol_raw=listing.local_symbol,
        captured_ns=captured_ns,
        last=last,
        volume=volume,
        bids=bids,
        asks=asks,
        last_size=last_size,
        prints=prints,
        currency=currency,
        price_scale=price_scale,
    )


def silent(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
    return []


def answering(source: str, *, last: float = 10.0) -> Responder:
    def respond(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        return [snap(source, listing, captured_ns, last=last) for listing in listings]

    return respond


def _no_board(mic: str, captured_ns: int, limit: int | None) -> list[LiveSnapshot]:
    return []


class FakeSource(LiveSource):
    """A configurable source. ``make_source`` fills the class attributes per test."""

    name: ClassVar[str] = "fake"
    supported_mics: ClassVar[frozenset[str]] = XIST
    max_tier: ClassVar[LiveTier] = LiveTier.L1
    calls: ClassVar[list[tuple[str, tuple[LiveListing, ...], int]]] = []
    board_calls: ClassVar[list[tuple[str, int | None]]] = []
    closes: ClassVar[list[str]] = []
    responder: ClassVar[Any] = staticmethod(silent)
    board_answer: ClassVar[Any] = staticmethod(_no_board)

    async def snapshots(
        self, listings: Sequence[LiveListing], *, captured_ns: int
    ) -> list[LiveSnapshot]:
        cls = type(self)
        cls.calls.append((cls.name, tuple(listings), captured_ns))
        return list(cls.responder(listings, captured_ns))

    async def board(
        self, mic: str, *, captured_ns: int, limit: int | None = None
    ) -> list[LiveSnapshot]:
        cls = type(self)
        cls.board_calls.append((mic, limit))
        return list(cls.board_answer(mic, captured_ns, limit))

    async def close(self) -> None:
        type(self).closes.append(type(self).name)


def make_source(
    name: str,
    *,
    tier: LiveTier = LiveTier.L1,
    answer: Responder = silent,
    official: bool = False,
    delay_s: int | None = None,
    batch_size: int = 1,
    min_interval_s: float = 5.0,
    needs_isin: bool = False,
    enumerates: bool = False,
    board_answer: BoardAnswer = _no_board,
    mics: frozenset[str] = XIST,
) -> type[FakeSource]:
    namespace: dict[str, Any] = {
        "name": name,
        "supported_mics": mics,
        "max_tier": tier,
        "official": official,
        "delay_s": delay_s,
        "batch_size": batch_size,
        "min_interval_s": min_interval_s,
        "needs_isin": needs_isin,
        "enumerates": enumerates,
        "calls": [],
        "board_calls": [],
        "closes": [],
        "responder": staticmethod(answer),
        "board_answer": staticmethod(board_answer),
    }
    return type(f"Fake_{name}", (FakeSource,), namespace)


def _feed(
    monkeypatch: pytest.MonkeyPatch,
    chain: Sequence[type[LiveSource]],
    clock: Clock,
    **kwargs: Any,
) -> LiveFeed:
    classes = tuple(chain)
    monkeypatch.setattr(feed_module, "sources_for", lambda mic, *, allow=(): classes)
    return LiveFeed(fetch=_no_network, clock_ns=clock, **kwargs)


# poll(): the chain walk and the cadence floor.


async def test_poll_returns_the_deepest_sources_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    deep = make_source("deep", tier=LiveTier.L2, answer=answering("deep", last=10.0))
    shallow = make_source("shallow", tier=LiveTier.LAST, answer=answering("shallow", last=11.0))
    feed = _feed(monkeypatch, [deep, shallow], Clock())
    snapshot = await feed.poll(THYAO)
    assert snapshot is not None
    assert snapshot.source == "deep" and snapshot.last == 10.0
    assert len(deep.calls) == 1
    assert shallow.calls == []


async def test_poll_falls_through_when_the_first_source_has_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep = make_source("deep", tier=LiveTier.L2, answer=silent)
    shallow = make_source("shallow", tier=LiveTier.LAST, answer=answering("shallow", last=11.0))
    feed = _feed(monkeypatch, [deep, shallow], Clock())
    snapshot = await feed.poll(THYAO)
    assert snapshot is not None
    assert snapshot.source == "shallow" and snapshot.last == 11.0
    assert len(deep.calls) == 1 and len(shallow.calls) == 1
    assert feed.walls == ()


async def test_poll_returns_none_when_every_source_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep = make_source("deep", tier=LiveTier.L2, answer=silent)
    shallow = make_source("shallow", tier=LiveTier.LAST, answer=silent)
    feed = _feed(monkeypatch, [deep, shallow], Clock())
    assert await feed.poll(THYAO) is None


async def test_an_empty_snapshot_counts_as_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def priceless(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        return [snap("deep", listing, captured_ns, last=None) for listing in listings]

    deep = make_source("deep", tier=LiveTier.L2, answer=priceless)
    shallow = make_source("shallow", tier=LiveTier.LAST, answer=answering("shallow"))
    feed = _feed(monkeypatch, [deep, shallow], Clock())
    snapshot = await feed.poll(THYAO)
    assert snapshot is not None and snapshot.source == "shallow"
    assert len(deep.calls) == 1


async def test_a_second_poll_inside_the_interval_returns_the_cached_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = iter(float(n) for n in range(1, 100))

    def counting(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        return [snap("deep", listing, captured_ns, last=next(prices)) for listing in listings]

    clock = Clock()
    deep = make_source("deep", tier=LiveTier.L2, answer=counting, min_interval_s=5.0)
    feed = _feed(monkeypatch, [deep], clock)
    first = await feed.poll(THYAO)
    clock.advance(2)
    second = await feed.poll(THYAO)
    assert second is first
    assert len(deep.calls) == 1
    clock.advance(4)
    third = await feed.poll(THYAO)
    assert third is not first
    assert third is not None and third.last == 2.0
    assert len(deep.calls) == 2


async def test_a_cached_nothing_is_not_asked_again_but_the_chain_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    deep = make_source("deep", tier=LiveTier.L2, answer=silent, min_interval_s=5.0)
    shallow = make_source(
        "shallow", tier=LiveTier.LAST, answer=answering("shallow"), min_interval_s=5.0
    )
    feed = _feed(monkeypatch, [deep, shallow], clock)
    first = await feed.poll(THYAO)
    assert first is not None and first.source == "shallow"
    clock.advance(2)
    second = await feed.poll(THYAO)
    assert second is first
    assert len(deep.calls) == 1 and len(shallow.calls) == 1
    clock.advance(4)
    third = await feed.poll(THYAO)
    assert third is not None and third is not first
    assert len(deep.calls) == 2 and len(shallow.calls) == 2


# Walls and faults.


async def test_a_wall_rests_the_source_for_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    flags = {"walled": True}

    def respond(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        if flags["walled"]:
            raise SourceUnservableError("deep", UnservableReason.BOT_CHALLENGE, "challenge page")
        return [snap("deep", listing, captured_ns, last=99.0) for listing in listings]

    clock = Clock()
    deep = make_source("deep", tier=LiveTier.L2, answer=respond)
    shallow = make_source("shallow", tier=LiveTier.LAST, answer=answering("shallow"))
    feed = _feed(monkeypatch, [deep, shallow], clock, wall_cooldown_s=60.0)

    snapshot = await feed.poll(THYAO)
    assert snapshot is not None and snapshot.source == "shallow"
    assert len(deep.calls) == 1
    (note,) = feed.walls
    assert note.source == "deep"
    assert note.kind == "wall"
    assert note.reason == "bot_challenge"
    assert note.detail == "challenge page"
    assert note.until_ns == T0 + 60 * NS

    clock.advance(30)
    snapshot = await feed.poll(THYAO)
    assert snapshot is not None and snapshot.source == "shallow"
    assert len(deep.calls) == 1
    assert len(feed.walls) == 1

    flags["walled"] = False
    clock.advance(31)
    snapshot = await feed.poll(THYAO)
    assert snapshot is not None and snapshot.source == "deep" and snapshot.last == 99.0
    assert len(deep.calls) == 2
    assert feed.walls == ()


async def test_three_unreadable_answers_rest_the_source_for_the_fault_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        raise LivePayloadError("deep", "bad body")

    clock = Clock()
    deep = make_source("deep", tier=LiveTier.L2, answer=unreadable, min_interval_s=0.0)
    shallow = make_source(
        "shallow", tier=LiveTier.LAST, answer=answering("shallow"), min_interval_s=0.0
    )
    feed = _feed(monkeypatch, [deep, shallow], clock, fault_cooldown_s=60.0)

    for _ in range(2):
        snapshot = await feed.poll(THYAO)
        assert snapshot is not None and snapshot.source == "shallow"
        clock.advance(1)
    assert len(deep.calls) == 2
    assert feed.walls == ()

    snapshot = await feed.poll(THYAO)
    assert snapshot is not None and snapshot.source == "shallow"
    assert len(deep.calls) == 3
    (note,) = feed.walls
    assert note.kind == "fault"
    assert note.source == "deep"
    assert note.reason == "LivePayloadError"
    assert note.detail == "deep: bad body"

    clock.advance(30)
    await feed.poll(THYAO)
    assert len(deep.calls) == 3

    clock.advance(31)
    snapshot = await feed.poll(THYAO)
    assert snapshot is not None and snapshot.source == "shallow"
    assert len(deep.calls) == 4
    assert feed.walls == ()


async def test_max_tier_skips_deeper_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    deep = make_source("deep", tier=LiveTier.L2, answer=answering("deep"))
    mid = make_source("mid", tier=LiveTier.L1, answer=answering("mid"))
    last = make_source("last", tier=LiveTier.LAST, answer=answering("last"))
    feed = _feed(monkeypatch, [deep, mid, last], Clock())

    snapshot = await feed.poll(THYAO, max_tier=LiveTier.LAST)
    assert snapshot is not None and snapshot.source == "last"
    assert deep.calls == [] and mid.calls == []

    snapshot = await feed.poll(ASELS, max_tier=LiveTier.L1)
    assert snapshot is not None and snapshot.source == "mid"
    # Only the first poll reached the LAST source; the L1 poll never did.
    assert deep.calls == [] and len(last.calls) == 1


# poll_many(): batching, fall-through, resting sources, cadence and attribution.


async def test_poll_many_batches_by_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    listings = [LiveListing(mic="XIST", local_symbol=f"S{n}") for n in range(5)]
    deep = make_source("deep", tier=LiveTier.L2, answer=answering("deep"), batch_size=2)
    feed = _feed(monkeypatch, [deep], Clock())
    out = await feed.poll_many(listings)
    assert set(out) == set(listings)
    assert [len(call[1]) for call in deep.calls] == [2, 2, 1]


async def test_poll_many_falls_through_per_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    listing_a = LiveListing(mic="XIST", local_symbol="AAA")
    listing_b = LiveListing(mic="XIST", local_symbol="BBB")
    listing_c = LiveListing(mic="XIST", local_symbol="CCC")

    def all_but_b(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        return [snap("deep", x, captured_ns) for x in listings if x.local_symbol != "BBB"]

    deep = make_source("deep", tier=LiveTier.L2, answer=all_but_b, batch_size=10)
    shallow = make_source("shallow", tier=LiveTier.LAST, answer=answering("shallow"), batch_size=10)
    feed = _feed(monkeypatch, [deep, shallow], Clock())
    out = await feed.poll_many([listing_a, listing_b, listing_c])
    assert out[listing_a].source == "deep"
    assert out[listing_c].source == "deep"
    assert out[listing_b].source == "shallow"
    assert deep.calls[0][1] == (listing_a, listing_b, listing_c)
    assert [call[1] for call in shallow.calls] == [(listing_b,)]


async def test_poll_many_never_asks_a_resting_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def walled(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        raise SourceUnservableError("deep", UnservableReason.FORBIDDEN, "403")

    deep = make_source("deep", tier=LiveTier.L2, answer=walled, batch_size=10)
    shallow = make_source("shallow", tier=LiveTier.LAST, answer=answering("shallow"), batch_size=10)
    feed = _feed(monkeypatch, [deep, shallow], Clock(), wall_cooldown_s=900.0)
    await feed.poll(THYAO)
    assert len(deep.calls) == 1

    out = await feed.poll_many([ASELS, THYAO_ISIN])
    assert len(deep.calls) == 1
    assert set(out) == {ASELS, THYAO_ISIN}
    assert all(snapshot.source == "shallow" for snapshot in out.values())


async def test_poll_many_honours_the_cadence_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    deep = make_source(
        "deep", tier=LiveTier.L2, answer=answering("deep"), batch_size=10, min_interval_s=5.0
    )
    feed = _feed(monkeypatch, [deep], clock)
    first = await feed.poll_many([THYAO, ASELS])
    assert len(deep.calls) == 1 and len(deep.calls[0][1]) == 2

    clock.advance(2)
    second = await feed.poll_many([THYAO, ASELS])
    assert second[THYAO] is first[THYAO]
    assert len(deep.calls) == 1

    clock.advance(4)
    await feed.poll_many([THYAO, ASELS])
    assert len(deep.calls) == 2


async def test_an_answer_that_gained_an_isin_is_attributed_to_the_asked_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def learned(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        return [snap("deep", THYAO_ISIN, captured_ns, last=99.0)]

    deep = make_source("deep", tier=LiveTier.L2, answer=learned, batch_size=10)
    feed = _feed(monkeypatch, [deep], Clock())

    snapshot = await feed.poll(THYAO)
    assert snapshot is not None and snapshot.last == 99.0
    assert snapshot.listing == THYAO_ISIN

    out = await feed.poll_many([THYAO])
    assert set(out) == {THYAO}
    assert out[THYAO].listing == THYAO_ISIN


# identify(): the ISIN lookup through the scanner.


async def test_identify_keeps_a_listing_that_already_has_an_isin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyed = make_source("keyed", tier=LiveTier.L2, needs_isin=True)
    scanner = make_source(tradingview.SOURCE, tier=LiveTier.LAST, answer=answering("scanner"))
    feed = _feed(monkeypatch, [keyed, scanner], Clock())
    assert await feed.identify(THYAO_ISIN) is THYAO_ISIN
    assert scanner.calls == []


async def test_identify_asks_nothing_when_no_source_needs_an_isin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = make_source("plain", tier=LiveTier.L2)
    scanner = make_source(tradingview.SOURCE, tier=LiveTier.LAST, answer=answering("scanner"))
    feed = _feed(monkeypatch, [plain, scanner], Clock())
    assert await feed.identify(THYAO) is THYAO
    assert scanner.calls == []


async def test_identify_fills_the_isin_once_and_remembers_found_and_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def scanner_answers(listings: Sequence[LiveListing], captured_ns: int) -> list[LiveSnapshot]:
        # The scanner knows THYAO's ISIN and answers ASELS without one.
        return [
            snap(tradingview.SOURCE, THYAO_ISIN if listing == THYAO else listing, captured_ns)
            for listing in listings
        ]

    scanner = make_source(
        tradingview.SOURCE, tier=LiveTier.LAST, answer=scanner_answers, batch_size=200
    )
    keyed = make_source("keyed", tier=LiveTier.L2, needs_isin=True)
    feed = _feed(monkeypatch, [keyed, scanner], Clock())

    assert await feed.identify(THYAO) == THYAO_ISIN
    assert await feed.identify(THYAO) == THYAO_ISIN
    assert len(scanner.calls) == 1

    # Not found: the listing comes back unchanged, and the answer is remembered too.
    assert await feed.identify(ASELS) is ASELS
    assert await feed.identify(ASELS) is ASELS
    assert len(scanner.calls) == 2


# board() and minute_bars().


async def test_board_uses_the_first_enumerating_source_and_skips_a_walled_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def walled_board(mic: str, captured_ns: int, limit: int | None) -> list[LiveSnapshot]:
        raise SourceUnservableError("walled", UnservableReason.FORBIDDEN, "403")

    def ranked_board(mic: str, captured_ns: int, limit: int | None) -> list[LiveSnapshot]:
        rows = [
            snap("ranked", THYAO, captured_ns),
            snap("ranked", ASELS, captured_ns),
        ]
        return rows[:limit] if limit is not None else rows

    walled = make_source("walled", tier=LiveTier.L2, enumerates=True, board_answer=walled_board)
    ranked = make_source("ranked", tier=LiveTier.L1, enumerates=True, board_answer=ranked_board)
    plain = make_source("plain", tier=LiveTier.LAST, answer=answering("plain"))
    feed = _feed(monkeypatch, [walled, ranked, plain], Clock())

    rows = await feed.board("XIST", limit=1)
    assert [row.listing for row in rows] == [THYAO]
    assert ranked.board_calls == [("XIST", 1)]
    assert plain.board_calls == []
    (note,) = feed.walls
    assert note.source == "walled" and note.kind == "wall" and note.reason == "forbidden"

    rows = await feed.board("XIST")
    assert len(rows) == 2
    assert len(walled.board_calls) == 1
    assert len(ranked.board_calls) == 2


async def test_board_is_empty_when_no_source_enumerates(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = make_source("plain", tier=LiveTier.LAST, answer=answering("plain"))
    feed = _feed(monkeypatch, [plain], Clock())
    assert await feed.board("XIST") == []
    assert plain.board_calls == []


async def test_minute_bars_read_only_from_the_chart_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    seen: list[LiveListing] = []

    async def bars(self: YahooChartLive, listing: LiveListing) -> list[Any]:
        seen.append(listing)
        return [sentinel]

    monkeypatch.setattr(YahooChartLive, "minute_bars", bars)
    other = make_source("other", tier=LiveTier.L2, answer=answering("other"))
    feed = _feed(monkeypatch, [other, YahooChartLive], Clock())

    assert await feed.minute_bars(THYAO) == [sentinel]
    assert seen == [THYAO]
    assert other.calls == []


async def test_minute_bars_wall_rests_the_chart_source(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[LiveListing] = []

    async def walled(self: YahooChartLive, listing: LiveListing) -> list[Any]:
        seen.append(listing)
        raise SourceUnservableError("yahoo_chart", UnservableReason.BOT_CHALLENGE, "consent")

    monkeypatch.setattr(YahooChartLive, "minute_bars", walled)
    feed = _feed(monkeypatch, [YahooChartLive], Clock())

    assert await feed.minute_bars(THYAO) == []
    (note,) = feed.walls
    assert note.source == "yahoo_chart" and note.kind == "wall"
    assert await feed.minute_bars(THYAO) == []
    assert len(seen) == 1


async def test_unreadable_minute_bars_are_an_empty_list_and_not_a_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[LiveListing] = []

    async def unreadable(self: YahooChartLive, listing: LiveListing) -> list[Any]:
        seen.append(listing)
        raise LivePayloadError("yahoo_chart", "bad body")

    monkeypatch.setattr(YahooChartLive, "minute_bars", unreadable)
    feed = _feed(monkeypatch, [YahooChartLive], Clock())

    assert await feed.minute_bars(THYAO) == []
    assert feed.walls == ()
    assert await feed.minute_bars(THYAO) == []
    assert len(seen) == 2


# close() and the listing helpers.


async def test_close_closes_every_instance_once(monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_source("first", tier=LiveTier.L2, answer=answering("first"))
    second = make_source("second", tier=LiveTier.LAST, answer=answering("second"))
    feed = _feed(monkeypatch, [first, second], Clock())
    assert len(feed.chain(THYAO)) == 2

    await feed.close()
    assert first.closes == ["first"]
    assert second.closes == ["second"]
    await feed.close()
    assert first.closes == ["first"] and second.closes == ["second"]


def test_listing_of_a_canonical_symbol() -> None:
    assert listing_of("THYAO@XIST") == LiveListing(mic="XIST", local_symbol="THYAO")
    assert listing_of("THYAO@XIST", isin="TRTHYAO91M5") == THYAO_ISIN


def test_listing_of_a_bare_us_ticker_uses_the_one_us_live_mic() -> None:
    assert feed_module.US_LIVE_MIC == "XNAS"
    assert listing_of("AAPL") == LiveListing(mic="XNAS", local_symbol="AAPL")


# volume_delta_trade().


def _book(
    *,
    captured_ns: int,
    volume: float | None,
    last: float | None = 25.5,
    source: str = "deep",
    local: str = "THYAO",
    prints: tuple[LivePrint, ...] = (),
    last_size: float | None = None,
) -> LiveSnapshot:
    return LiveSnapshot(
        source=source,
        listing=LiveListing(mic="XIST", local_symbol=local),
        symbol_raw=local,
        captured_ns=captured_ns,
        last=last,
        volume=volume,
        prints=prints,
        last_size=last_size,
        currency="TRY",
        price_scale=2,
    )


def test_a_volume_delta_on_time_is_a_derived_print() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0, last=25.5)
    trade = volume_delta_trade(previous, current, cadence_s=10.0)
    assert isinstance(trade, Trade)
    assert trade.price == 25.5
    assert trade.amount == 600.0
    assert trade.side is Side.UNKNOWN
    assert trade.prov is Provenance.DERIVED
    assert trade.prov_basis == "volume_delta_print"
    assert trade.prov_confidence == 1.0
    assert trade.symbol == "THYAO@XIST"
    assert trade.source == "deep"
    assert trade.local_ts == T0 + 10 * NS
    assert trade.id == f"vd-{T0 + 10 * NS}"
    assert trade.currency == "TRY" and trade.price_scale == 2


def test_a_late_volume_delta_scores_the_fraction_of_the_interval_resolved() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 40 * NS, volume=1_600.0, last=25.5)
    trade = volume_delta_trade(previous, current, cadence_s=10.0)
    assert isinstance(trade, Trade)
    assert trade.amount == 600.0
    assert trade.prov is Provenance.DERIVED
    assert trade.prov_basis == "volume_delta_print"
    assert trade.prov_confidence == 0.25


def test_a_different_source_gives_no_delta() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0, source="other")
    assert volume_delta_trade(previous, current, cadence_s=10.0) is None


def test_a_different_listing_gives_no_delta() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0, local="ASELS")
    assert volume_delta_trade(previous, current, cadence_s=10.0) is None


def test_a_publisher_tape_is_never_derived_from() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    print_row = LivePrint(price=25.5, size=100.0, ts_ns=None)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0, prints=(print_row,))
    assert volume_delta_trade(previous, current, cadence_s=10.0) is None


def test_a_stated_last_size_is_never_derived_from() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0, last_size=100.0)
    assert volume_delta_trade(previous, current, cadence_s=10.0) is None


def test_a_missing_volume_gives_no_delta() -> None:
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0)
    assert volume_delta_trade(_book(captured_ns=T0, volume=None), current, cadence_s=10.0) is None
    previous = _book(captured_ns=T0, volume=1_000.0)
    no_volume = _book(captured_ns=T0 + 10 * NS, volume=None)
    assert volume_delta_trade(previous, no_volume, cadence_s=10.0) is None


def test_a_missing_last_price_gives_no_delta() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0, last=None)
    assert volume_delta_trade(previous, current, cadence_s=10.0) is None


def test_volume_that_did_not_grow_gives_no_delta() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    same = _book(captured_ns=T0 + 10 * NS, volume=1_000.0)
    shrunk = _book(captured_ns=T0 + 10 * NS, volume=900.0)
    assert volume_delta_trade(previous, same, cadence_s=10.0) is None
    assert volume_delta_trade(previous, shrunk, cadence_s=10.0) is None


def test_a_non_positive_gap_gives_no_delta() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    same_instant = _book(captured_ns=T0, volume=1_600.0)
    earlier = _book(captured_ns=T0 - 10 * NS, volume=1_600.0)
    assert volume_delta_trade(previous, same_instant, cadence_s=10.0) is None
    assert volume_delta_trade(previous, earlier, cadence_s=10.0) is None


def test_a_non_positive_cadence_gives_no_delta() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0)
    assert volume_delta_trade(previous, current, cadence_s=0.0) is None
    assert volume_delta_trade(previous, current, cadence_s=-1.0) is None


def test_a_non_positive_last_price_gives_no_delta() -> None:
    previous = _book(captured_ns=T0, volume=1_000.0)
    current = _book(captured_ns=T0 + 10 * NS, volume=1_600.0, last=0.0)
    assert volume_delta_trade(previous, current, cadence_s=10.0) is None
