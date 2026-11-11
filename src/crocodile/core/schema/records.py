"""Canonical record types.

Every record leads with the ten fields of :class:`_Header`: the four that identify an
observation, ``asset_class``, ``source_ts``, and the four-field provenance tail. All ten
live on the base and no record can restate or reorder them.

``kw_only=True`` on each record is what makes that possible. msgspec forbids a required
field after a defaulted one, which would otherwise force every struct to repeat the
defaulted tail after its own required fields; keyword-only fields are exempt, and the
base's fields stay positional so they keep leading. The pairing is load-bearing in both
directions: ``kw_only`` on the base instead would order the subclass's fields first and
silently break the header. ``tests/conformance/test_gates.py`` enforces both halves.
"""

from __future__ import annotations

import math

import msgspec

from crocodile.core.schema.enums import (
    AssetClass,
    Channel,
    CorpActionType,
    FundPeriod,
    OptType,
    PriceAdjustment,
    SecurityType,
    Side,
    Tape,
)
from crocodile.core.schema.provenance import Provenance

Level = tuple[float, float]
"""(price, size). A size of 0.0 means REMOVE this level."""


class _Header(msgspec.Struct, frozen=True):
    """The ten fields every record carries.

    Never mark this ``kw_only``: msgspec orders positional fields ahead of keyword-only
    ones, so doing so would move each record's own fields in front of the header.
    """

    source: str
    """Venue (crypto) or data provider (equity)."""

    symbol: str
    """Canonical symbol, e.g. ``deribit:BTC-PERPETUAL`` or ``AAPL``."""

    symbol_raw: str
    """The symbol exactly as the source spelled it."""

    local_ts: int
    """UTC epoch nanoseconds at which we observed the record."""

    asset_class: AssetClass
    """Which market this came from, so an absent field reads as normal or as a defect."""

    source_ts: int | None
    """UTC epoch nanoseconds the source stamped, or ``None`` if it stamped nothing.

    Required, with no default. An adapter must state which of those two happened; a
    default would let one of 108 adapters forget and report a silent ``None`` instead.
    """

    prov: Provenance = Provenance.NATIVE
    """How this record came to exist. Never ``UNAVAILABLE`` — see :class:`Provenance`."""

    prov_basis: str = "native"
    """The registered basis name, a key into the provenance registry."""

    prov_confidence: float = 1.0
    """Sampling adequacy within ``prov``'s level, from the basis's registered formula."""

    prov_inputs: list[str] = []
    """The channels the basis consumed.

    A mutable default is safe here and only here: msgspec copies it per instance rather
    than sharing one list across every record, which a conformance test pins down.
    """


class Trade(_Header, frozen=True, kw_only=True, tag=Channel.TRADE.value, tag_field="channel"):
    id: str
    price: float
    amount: float
    side: Side
    """Aggressor side. Required, and equity is not exempt.

    A tape that does not classify the aggressor writes ``Side.UNKNOWN`` rather than
    omitting the field: that is a claim about the venue, where an absent field is merely
    an adapter that did not say. Same reasoning that keeps ``source_ts`` required.
    """

    liquidation: str | None = None
    l1_gas_fee: float | None = None
    l2_gas_fee: float | None = None
    gas_price: float | None = None
    sender: str | None = None
    is_smart_wallet: bool | None = None
    conditions: list[str] | None = None
    tape: Tape | None = None
    venue: str | None = None
    currency: str | None = None
    """ISO 4217 code of the unit ``price`` is stated in, or ``None`` if the source did not say.

    ``None`` means unknown and is never read as USD. Prices are stored in the unit the source
    published; see ``price_scale``.
    """
    price_scale: int | None = None
    """Power of ten between the stored price and one unit of ``currency``: major-unit value =
    stored value / 10**price_scale. ``0`` for a major-unit quote, ``2`` for a listing quoted in
    pence (GBX/GBp), cents (ZAc) or agorot (ILA). ``None`` when the source did not say. A query
    that ignores it reads a minor-unit listing 100 times too high.
    """


class BookSnapshot(
    _Header, frozen=True, kw_only=True, tag=Channel.BOOK_SNAPSHOT.value, tag_field="channel"
):
    bids: list[Level]
    asks: list[Level]
    depth: int
    sequence_id: int | None = None
    is_snapshot: bool = True


class BookDelta(
    _Header, frozen=True, kw_only=True, tag=Channel.BOOK_DELTA.value, tag_field="channel"
):
    bids: list[Level]
    asks: list[Level]
    seq_id: int | None = None
    prev_seq_id: int | None = None
    is_snapshot: bool = False


class BookTicker(
    _Header, frozen=True, kw_only=True, tag=Channel.BOOK_TICKER.value, tag_field="channel"
):
    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float
    update_id: int | None = None

    @property
    def price(self) -> float:
        return round(math.sqrt(self.bid_px * self.ask_px), 6)


class DerivativeTicker(
    _Header, frozen=True, kw_only=True, tag=Channel.DERIVATIVE_TICKER.value, tag_field="channel"
):
    last_price: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    predicted_funding_rate: float | None = None
    realized_funding_rate: float | None = None
    """The same three-way split :class:`Funding` carries, kept mirrored on purpose.

    Every venue that emits a ``DerivativeTicker`` emits a ``Funding`` beside it off the
    same payload, and the Deribit connector wrote ``funding_8h`` into *both* forward
    fields from one line each. Two records fed by one dict diverging in shape is how one
    of them gets fixed and the other does not.
    """

    funding_timestamp: int | None = None
    open_interest: float | None = None


class OptionsChain(
    _Header, frozen=True, kw_only=True, tag=Channel.OPTIONS_CHAIN.value, tag_field="channel"
):
    """One option contract's quote, greeks and open interest, for either asset class.

    Equity called this ``OptionQuote`` and spoke a different dialect of the same
    instrument: ``type``/``bid``/``ask``/``last``/``implied_volatility`` for what crypto
    calls ``opt_type``/``bid_px``/``ask_px``/``last_price``/``mark_iv``. The crypto names
    win — they distinguish a price from a size, and an equity feed that only publishes a
    single IV per contract is publishing the mark. ``volume`` is the one equity field with
    no crypto counterpart and it arrives unchanged.
    """

    underlying: str
    underlying_price: float | None
    strike: float
    expiry: int
    """Expiry as UTC epoch nanoseconds.

    Equity stored ``YYYY-MM-DD``. Nanoseconds win: it is the unit every other timestamp
    here uses, and a date cannot express the intraday expiry of a 0DTE or a weekly that
    settles at the open rather than the close. Converting a date to nanoseconds is
    total; converting back is not, which is the direction that must not lose anything.
    """

    opt_type: OptType
    mark_price: float | None = None
    mark_iv: float | None = None  # decimal fraction (0.65 == 65%); all *_iv fields are decimal
    bid_px: float | None = None
    bid_sz: float | None = None
    bid_iv: float | None = None  # decimal fraction
    ask_px: float | None = None
    ask_sz: float | None = None
    ask_iv: float | None = None  # decimal fraction
    last_price: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    rho: float | None = None


class Funding(_Header, frozen=True, kw_only=True, tag=Channel.FUNDING.value, tag_field="channel"):
    """One funding observation for a perpetual, for whichever venue published it.

    The three rate fields differ by *when they are about*, and that is the whole of the
    distinction: ``funding_rate`` is the rate in force for the interval this record
    describes, ``predicted_funding_rate`` is an estimate of the next one, and
    ``realized_funding_rate`` is what has already accrued. All three are per-interval
    figures over ``interval_hours``, so ``crocodile.crypto.analytics.funding.apr_from_rate``
    annualizes any of them by the same factor.
    """

    funding_rate: float
    funding_timestamp: int | None = None
    predicted_funding_rate: float | None = None
    """A forward-looking estimate of the *next* interval's rate, or ``None``.

    ``None`` means the venue publishes no estimate, which is common: Deribit's ticker
    does not, and its funding-history endpoint cannot by construction. Deribit's
    connector used to fill this with ``funding_8h``, a figure describing the eight hours
    that had already happened — the one substitution this field cannot survive, since a
    consumer reads it precisely to know what it is about to pay.
    """

    realized_funding_rate: float | None = None
    """Funding that has already accrued, over the window ending at ``funding_timestamp``.

    Distinct from ``funding_rate`` because a venue can publish both and they can differ:
    Deribit's ticker carries ``current_funding`` (the rate in force) beside ``funding_8h``
    (what the last eight hours actually cost), and a carry desk wants both.

    A separate field rather than a note in ``prov``, because the provenance tail is a
    claim about the *record*: ``prov``/``prov_basis``/``prov_confidence`` describe how the
    whole observation was arrived at, and there is nowhere in them to say "this one field
    is about a different span than the field beside it". Marking the record SYNTHETIC to
    excuse one misfiled number would also have downgraded ``funding_rate``, which is a
    plain venue reading. Shape is the only honest place for a distinction about shape.
    """

    interval_hours: int | None = None


class OpenInterest(
    _Header, frozen=True, kw_only=True, tag=Channel.OPEN_INTEREST.value, tag_field="channel"
):
    open_interest: float
    open_interest_value: float | None = None


class Liquidation(
    _Header, frozen=True, kw_only=True, tag=Channel.LIQUIDATION.value, tag_field="channel"
):
    price: float
    amount: float
    side: Side
    id: str | None = None


class OHLCV(_Header, frozen=True, kw_only=True, tag=Channel.OHLCV.value, tag_field="channel"):
    """One aggregated bar, for either asset class.

    Equity's ``Bar`` and equity's own ``OHLCV`` were field-for-field identical, so only
    one of them survives here. But equity and crypto were *not* identical: of equity's two
    extra fields only ``trade_count`` was a rename, arriving as ``num_trades``. ``vwap``
    had no crypto counterpart and is carried below so the equity adapters have somewhere
    to put it.
    """

    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float | None = None
    sell_volume: float | None = None
    """Volume whose aggressor was a buyer / a seller, or ``None`` if nobody split it.

    Optional for the reason ``vwap`` below is, and the zero they replaced was the worse
    half of that argument: ``0.0`` is a *measurement* — no buying happened in this bar —
    and it was standing in for "no path filled this in", which was almost every path.
    Exactly two writers ever set them: ``binance/backfill.py``, off
    ``takerBuyBaseAssetVolume``, and ``corpactions/calculator.py``, which rescales what it
    is handed. Every other bar in the lake — six equity providers, two crypto ones, and
    all three record resamplers — carried a defaulted zero, so a consumer reading
    ``buy_volume == 0.0`` could not tell a quiet bar from an unfilled field, and
    ``sum(buy_volume) / sum(volume)`` over a mixed lake answered with the Binance
    fraction of it.

    ``buy_volume + sell_volume <= volume`` where both are stated: an unclassified print is
    credited to neither side, and the remainder is the volume no source attributed. See
    :func:`crocodile.core.resample.ohlcv._side_volume_sql` for why that is an inequality
    rather than an identity.
    """

    num_trades: int | None = None
    vwap: float | None = None
    """Volume-weighted average price over the bar, or ``None`` if the source omits it.

    Optional rather than defaulted to a number: most crypto venues do not publish a VWAP,
    and ``None`` says "not reported" where ``0.0`` would be a false price.
    """

    currency: str | None = None
    """ISO 4217 code of the unit ``open``, ``high``, ``low``, ``close`` and ``vwap`` are stated
    in, or ``None`` if the source did not say.

    ``None`` means unknown and is never read as USD. Prices are stored in the unit the source
    published; see ``price_scale``.
    """
    price_scale: int | None = None
    """Power of ten between the stored price and one unit of ``currency``: major-unit value =
    stored value / 10**price_scale. ``0`` for a major-unit quote, ``2`` for a listing quoted in
    pence (GBX/GBp), cents (ZAc) or agorot (ILA). ``None`` when the source did not say. A query
    that ignores it reads a minor-unit listing 100 times too high.
    """
    adjustment: PriceAdjustment | None = None
    """Which corporate actions the source had already applied to these prices (see
    :class:`PriceAdjustment`). ``None`` means nobody measured it, which is every source until a
    live check settles it (spec §3.10a). Dividend adjustment on read is the calculator's job,
    from stored actions, not this field's.
    """


class Quote(_Header, frozen=True, kw_only=True, tag=Channel.QUOTE.value, tag_field="channel"):
    """A top-of-book quote as an equity feed publishes it.

    Distinct from :class:`BookTicker`, which is the crypto spelling on the ``book_ticker``
    channel: this one carries the consolidated-tape facts (``is_nbbo``, ``tape``) that a
    venue-native crypto quote has no notion of.
    """

    bid_px: float
    bid_sz: float
    ask_px: float
    ask_sz: float
    is_nbbo: bool = False
    is_consolidated: bool = False
    conditions: list[str] | None = None
    tape: Tape | None = None
    currency: str | None = None
    """ISO 4217 code of the unit ``bid_px`` and ``ask_px`` are stated in, or ``None`` if the
    source did not say.

    ``None`` means unknown and is never read as USD. Prices are stored in the unit the source
    published; see ``price_scale``.
    """
    price_scale: int | None = None
    """Power of ten between the stored price and one unit of ``currency``: major-unit value =
    stored value / 10**price_scale. ``0`` for a major-unit quote, ``2`` for a listing quoted in
    pence (GBX/GBp), cents (ZAc) or agorot (ILA). ``None`` when the source did not say. A query
    that ignores it reads a minor-unit listing 100 times too high.
    """


class DepthProfile(
    _Header, frozen=True, kw_only=True, tag=Channel.DEPTH.value, tag_field="channel"
):
    """Aggregated resting size around a reference price.

    Equity's version carried ``basis: str`` and ``is_synthetic: bool`` — the prototype the
    provenance tail generalized. Both now live in the header as ``prov_basis`` and ``prov``.
    """

    bids: list[Level]
    asks: list[Level]
    reference_price: float
    depth: int
    snapshot_ts: int | None = None
    """The ``local_ts`` of the stored observation this ladder was cut from, when it was cut
    from one rather than built at the moment of the call.

    ``local_ts`` on a re-stamped record is the instant the record *claims*, not the instant
    anything was observed — ``book_snapshot_slice`` answers for an instant the caller names
    and cuts the newest stored book at or before it. That re-stamp destroyed the only
    number the record's own confidence was computed from. ``source_ts`` cannot stand in: it
    is the venue's clock rather than ours, a different number wherever both exist, and
    ``None`` for every Coinbase book because that normalizer has no venue stamp to carry.

    So ``age_ns = local_ts - snapshot_ts`` is recoverable from the row, which is what
    ``book_snapshot_slice`` offers in exchange for taking its freshness denominator from
    the caller: over-declaring the window raises that score, and the answer to a reader who
    thinks a window was too generous is that they can re-grade it. That answer was in the
    registration before the field it needs was.

    ``None`` — the default — says this profile was not cut from a stored observation, and
    that ``local_ts`` therefore means what the header says it means. Both equity halves are
    in that case: ``yahoo_1m_vap`` and ``alpaca_l1`` build a ladder at the moment they are
    called, so there is no second instant to carry.
    """

    @property
    def is_synthetic(self) -> bool:
        """The old spelling of the claim that now lives in ``prov``.

        Python attribute access keeps working: ``record.is_synthetic`` reads what it
        always read. SQL needed more than this property. A property is not a struct
        field, so it is not in the row ``to_row`` flattens — and ``WHERE is_synthetic``
        against a file without the column does not error, it matches nothing, which would
        have returned the pre-merge half of the lake as if it were all of it.

        So the persisted column is derived from ``prov`` at write time, by
        ``crocodile.core.store.parquet_sink._derive_depth_columns``, together with
        ``basis`` from ``prov_basis``. That function restates the predicate below against
        the flattened row; a test writes one profile per provenance level through the
        real sink and compares the column with this property, because two spellings of
        one predicate is a place for them to disagree.
        """
        return self.prov is Provenance.SYNTHETIC


class Auction(_Header, frozen=True, kw_only=True, tag=Channel.AUCTION.value, tag_field="channel"):
    paired_shares: float | None = None
    imbalance_shares: float | None = None
    imbalance_side: Side | None = None
    reference_price: float | None = None
    indicative_price: float | None = None
    auction_type: str | None = None


class TradingStatus(
    _Header, frozen=True, kw_only=True, tag=Channel.TRADING_STATUS.value, tag_field="channel"
):
    status: str
    reason: str | None = None
    limit_up_price: float | None = None
    limit_down_price: float | None = None
    indicator: str | None = None


class Instrument(
    _Header, frozen=True, kw_only=True, tag=Channel.INSTRUMENT.value, tag_field="channel"
):
    """The persisted reference record for a listed security.

    The in-memory identity object the provider layer passes around is
    :class:`crocodile.equity.reference.identity.InstrumentIdentity`. It shared this name
    until the union merge, one import line away and with four fields in common, which is a
    wrong import that type-checks. It is not a record: no tag, no union, never written.
    """

    name: str | None = None
    cik: str | None = None
    figi: str | None = None
    composite_figi: str | None = None
    share_class_figi: str | None = None
    cusip: str | None = None
    exchange: str | None = None
    security_type: SecurityType | None = None
    sic: str | None = None
    shares_outstanding: int | None = None
    listing_date: str | None = None
    status: str | None = None
    mic: str | None = None
    """Operating MIC (ISO 10383) of the market this listing trades on, e.g. ``XIST``.

    The listing grain is the operating MIC (spec §3.4); a segment MIC goes in ``segment_mic``.
    Distinct from ``exchange``, the free-text venue label the source printed.
    """
    segment_mic: str | None = None
    """The segment MIC the source named for this listing (e.g. ``XNGS`` under ``XNAS``), or
    ``None``."""
    isin: str | None = None
    """ISIN of the instrument. One ISIN can have many listings, and its prefix is the country of
    issue rather than of listing, so it never identifies a listing on its own.
    """
    country: str | None = None
    """ISO 3166-1 alpha-2 country of the listing's market (the operating MIC's country).

    This is not the issuer's country of incorporation and not the ISIN prefix.
    """
    currency: str | None = None
    """ISO 4217 trading currency of the listing as observed, or ``None``.

    It is kept on the snapshot, not only on the listing master, because a market's currency
    changes (Croatia 2023, Bulgaria 2026). ``None`` is never read as USD.
    """
    price_scale: int | None = None
    """Power of ten between the stored price and one unit of ``currency``: major-unit value =
    stored value / 10**price_scale. ``0`` for a major-unit quote, ``2`` for a listing quoted in
    pence (GBX/GBp), cents (ZAc) or agorot (ILA). ``None`` when the source did not say. A query
    that ignores it reads a minor-unit listing 100 times too high.
    """


class CorporateAction(
    _Header, frozen=True, kw_only=True, tag=Channel.CORP_ACTION.value, tag_field="channel"
):
    ex_date: str  # YYYY-MM-DD
    type: CorpActionType
    value: float
    currency: str | None = None
    """ISO 4217 code of ``value`` when it is an amount of money (a cash dividend), or ``None``.

    Always ``None`` for a split, whose ``value`` is a ratio. Never read as USD.
    """
    price_scale: int | None = None
    """Power of ten between the stored price and one unit of ``currency``: major-unit value =
    stored value / 10**price_scale. ``0`` for a major-unit quote, ``2`` for a listing quoted in
    pence (GBX/GBp), cents (ZAc) or agorot (ILA). ``None`` when the source did not say. A query
    that ignores it reads a minor-unit listing 100 times too high.
    """


class Fundamental(
    _Header, frozen=True, kw_only=True, tag=Channel.FUNDAMENTAL.value, tag_field="channel"
):
    taxonomy: str
    tag: str
    unit: str
    val: float
    end: str  # period_end
    start: str | None = None  # for duration facts, None for instant facts
    fy: int | None = None
    fp: FundPeriod | None = None  # e.g., Q1, Q2, Q3, Q4, FY, TTM
    form: str | None = None
    filed: str | None = None
    accn: str | None = None
    frame: str | None = None


class InsiderTransaction(
    _Header, frozen=True, kw_only=True, tag=Channel.INSIDER.value, tag_field="channel"
):
    insider_name: str
    position: str
    transaction_type: str
    transaction_date: str  # YYYY-MM-DD
    shares: float | None = None
    price: float | None = None
    value: float | None = None
    ownership: str | None = None  # "D" or "I"
    acquired_disposed: str | None = None  # "A" or "D"
    """Which way the shares moved, from Form 4's ``transactionAcquiredDisposedCode``.

    Optional because the Yahoo scrape that filled this record first cannot report it — that
    page publishes a prose transaction label and no A/D column — and ``None`` says
    "not reported" where either letter would be a guess.

    It is a separate field rather than something a reader infers from
    ``transaction_type``, and the reason is code ``G``. A gift is one transaction code and
    two directions: the donor disposes and the donee acquires, and only this box says which
    one this row is. Codes ``P``/``S``/``A``/``F``/``M`` do imply a direction, so a lookup
    table would be right most of the time and silently wrong on the rest — which is the
    shape of defect ``smart-money`` cannot survive, since its whole output is a signed flow.
    """

    insider_cik: str | None = None
    """The reporting owner's ten-digit SEC identifier, when the source publishes one.

    Carried because it is the identity a watchlist should key on. A person's name is spelled
    however the filing agent typed it — ``COOK TIMOTHY D`` one quarter and ``Cook Timothy
    D.`` the next — while a CIK is assigned once and never re-spelled, so a join on the name
    silently misses filings a join on the CIK catches. This is the field behind the claim in
    ``capabilities/analytics.py``'s ledger that ``label-transfers`` is the one capability
    equities serve *better* than crypto: an unwatched Ethereum address is a hex string and
    nothing else, whereas an unwatched filer arrives already carrying both a stable id and a
    human-readable name.
    """


class Holding13F(
    _Header, frozen=True, kw_only=True, tag=Channel.HOLDING_13F.value, tag_field="channel"
):
    manager_name: str
    issuer_name: str
    cusip: str
    value: float
    shares: float
    shares_type: str
    discretion: str | None = None
    voting_sole: float | None = None
    voting_shared: float | None = None
    voting_none: float | None = None
    report_date: str | None = None
    accession_number: str | None = None
    manager_cik: str | None = None
    """The filing manager's ten-digit SEC identifier, for the reason
    :attr:`InsiderTransaction.insider_cik` carries one.

    ``manager_name`` is free text on the cover page and moves — a manager that renames, or
    an agent that drops ``, LLC``, produces a different string for the same institution
    across two quarters, which is exactly the join ``smart-money`` makes when it differences
    consecutive information tables. The CIK does not move.
    """


class ShortInterest(
    _Header, frozen=True, kw_only=True, tag=Channel.SHORT_INTEREST.value, tag_field="channel"
):
    settlement_date: str
    short_interest: float
    prev_short_interest: float | None = None
    days_to_cover: float | None = None
    change_pct: float | None = None


class ShortVolume(
    _Header, frozen=True, kw_only=True, tag=Channel.SHORT_VOLUME.value, tag_field="channel"
):
    date: str
    short_volume: float
    total_volume: float
    short_exempt_volume: float | None = None


class Filing(_Header, frozen=True, kw_only=True, tag=Channel.FILING.value, tag_field="channel"):
    accession_number: str
    form: str
    filing_date: str
    primary_document: str
    document_url: str
    report_date: str | None = None
    is_xbrl: bool | None = None


class MacroSeries(
    _Header, frozen=True, kw_only=True, tag=Channel.MACRO_SERIES.value, tag_field="channel"
):
    date: str
    value: float | None = None
    realtime_start: str | None = None
    realtime_end: str | None = None


class IndexValue(
    _Header, frozen=True, kw_only=True, tag=Channel.INDEX_VALUE.value, tag_field="channel"
):
    value: float


class FarcasterCorrelation(
    _Header, frozen=True, kw_only=True, tag=Channel.FARCASTER_CORRELATION.value, tag_field="channel"
):
    mentions_24h: int
    dev_activity_score: float
    trending_rank: int


class ReserveDataUpdated(
    _Header, frozen=True, kw_only=True, tag=Channel.RESERVE_DATA_UPDATED.value, tag_field="channel"
):
    reserve: str
    liquidity_rate: float
    stable_borrow_rate: float
    variable_borrow_rate: float
    liquidity_index: int
    variable_borrow_index: int


class LiquidationCall(
    _Header, frozen=True, kw_only=True, tag=Channel.LIQUIDATION_CALL.value, tag_field="channel"
):
    collateral_asset: str
    debt_asset: str
    user: str
    debt_to_cover: float
    liquidated_collateral_amount: float
    liquidator: str
    receive_a_token: bool


class LimitOrderFill(
    _Header, frozen=True, kw_only=True, tag=Channel.LIMIT_ORDER_FILL.value, tag_field="channel"
):
    tx_hash: str
    log_index: int
    protocol: str  # "1inch" | "0x"
    maker: str
    taker: str
    maker_token: str
    taker_token: str
    maker_amount: float
    taker_amount: float
    order_hash: str


class BalanceCorrection(
    _Header, frozen=True, kw_only=True, tag=Channel.BALANCE_CORRECTION.value, tag_field="channel"
):
    holder_address: str
    token_address: str
    local_balance: float
    onchain_balance: float
    correction_amount: float


class PoRUpdate(
    _Header, frozen=True, kw_only=True, tag=Channel.POR_UPDATE.value, tag_field="channel"
):
    feed_address: str
    token_address: str
    reserves: float
    total_supply: float
    backing_ratio: float
    is_backed: bool


Record = (
    Trade
    | BookSnapshot
    | BookDelta
    | BookTicker
    | DerivativeTicker
    | OptionsChain
    | Funding
    | OpenInterest
    | Liquidation
    | OHLCV
    | Quote
    | DepthProfile
    | Auction
    | TradingStatus
    | Instrument
    | CorporateAction
    | Fundamental
    | InsiderTransaction
    | Holding13F
    | ShortInterest
    | ShortVolume
    | Filing
    | MacroSeries
    | IndexValue
    | FarcasterCorrelation
    | ReserveDataUpdated
    | LiquidationCall
    | LimitOrderFill
    | BalanceCorrection
    | PoRUpdate
)
