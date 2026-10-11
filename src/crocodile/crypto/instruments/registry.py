from enum import StrEnum

import msgspec


class Kind(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURE = "future"
    OPTION = "option"


class Instrument(msgspec.Struct, frozen=True):
    canonical: str
    exchange: str
    symbol_raw: str
    kind: Kind
    base: str
    quote: str
    strike: float | None = None
    expiry: int | None = None
    opt_type: str | None = None
    tick_size: float | None = None
    contract_size: float | None = None
    settlement_currency: str | None = None
    oi_unit: str | None = None


class InstrumentRegistry:
    def __init__(self) -> None:
        self._by_raw: dict[tuple[str, str], Instrument] = {}
        self._by_canonical: dict[str, Instrument] = {}

    def add(self, inst: Instrument) -> None:
        self._by_raw[(inst.exchange, inst.symbol_raw)] = inst
        self._by_canonical[inst.canonical] = inst

    def by_raw(self, exchange: str, symbol_raw: str) -> Instrument:
        return self._by_raw[(exchange, symbol_raw)]

    def by_canonical(self, canonical: str) -> Instrument:
        return self._by_canonical[canonical]

    def get_raw(self, exchange: str, symbol_raw: str) -> Instrument | None:
        return self._by_raw.get((exchange, symbol_raw))

    def instruments(self) -> tuple[Instrument, ...]:
        """Snapshot the public metadata used by normalizers."""
        return tuple(self._by_canonical.values())
