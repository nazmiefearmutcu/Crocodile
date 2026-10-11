import pytest

from crocodile.core.errors import FatalConnectorError
from crocodile.crypto.exchanges.deribit.book import DeribitBookGate
from crocodile.crypto.exchanges.deribit.normalize import normalize_message


def book(kind, change_id, prev=None, symbol="BTC-PERPETUAL", **kwargs):
    data = {
        "instrument_name": symbol,
        "type": kind,
        "change_id": change_id,
        "timestamp": 100,
        "bids": [],
        "asks": [],
        **kwargs,
    }
    if prev is not None:
        data["prev_change_id"] = prev
    return data


def test_initial_snapshot_nonconsecutive_ids_and_delete():
    gate = DeribitBookGate()
    assert gate.evaluate(book("change", 9, 8)) == "drop"
    assert gate.evaluate(book("snapshot", 10, bids=[["new", 100, 3]])) == "snapshot"
    data = book("change", 14, 10, bids=[["change", 100, 2]], asks=[["new", 101, 7]])
    assert gate.evaluate(data) == "apply"
    assert gate.bids["BTC-PERPETUAL"] == {100: 2}
    assert gate.evaluate(book("change", 14, 10)) == "drop"
    deletion = book("change", 18, 14, bids=[["delete", 100, 99]])
    assert gate.evaluate(deletion) == "apply"
    (record,) = normalize_message(
        {"params": {"channel": "book.BTC-PERPETUAL.raw", "data": deletion}}, local_ts=200
    )
    assert record.bids == [(100, 0)]
    assert gate.bids["BTC-PERPETUAL"] == {}


def test_gap_invalidates_only_affected_symbol_until_new_snapshot():
    gate = DeribitBookGate()
    gate.evaluate(book("snapshot", 10))
    gate.evaluate(book("snapshot", 1, symbol="ETH-PERPETUAL"))
    assert gate.evaluate(book("change", 12, 11)) == "gap"
    assert gate.last_change_id("BTC-PERPETUAL") is None
    assert gate.evaluate(book("change", 13, 12)) == "drop"
    assert gate.evaluate(book("change", 2, 1, symbol="ETH-PERPETUAL")) == "apply"
    assert gate.evaluate(book("snapshot", 20)) == "snapshot"
    assert gate.evaluate(book("change", 21, 20)) == "apply"
    gate.reset()
    assert gate.last_change_id("ETH-PERPETUAL") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"bids": [["replace", 100, 2]]},
        {"asks": [["new", float("nan"), 2]]},
        {"change_id": True},
        {"asks": [["change", 100, -1]]},
        {"type": "bad"},
        {"bids": [["new", 100]]},
    ],
)
def test_invalid_book_protocol_is_fatal(changes):
    with pytest.raises(FatalConnectorError):
        DeribitBookGate().evaluate({**book("snapshot", 10), **changes})
