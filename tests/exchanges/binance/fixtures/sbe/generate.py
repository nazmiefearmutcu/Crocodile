"""Fixture bytes from the pinned XML field table. Imports no production code."""

from pathlib import Path
from struct import pack

ROOT = Path(__file__).parent
EVENT = 1700000000000000
SYMBOL = b"\x07BTCUSDT"


def header(block, template):
    return pack("<HHHH", block, template, 1, 0)


def levels(entries, *, extension=b""):
    return pack("<HH", 16 + len(extension), len(entries)) + b"".join(
        pack("<qq", price, qty) + extension for price, qty in entries
    )


(ROOT / "trades.bin").write_bytes(
    header(18, 10000)
    + pack("<qqbb", EVENT, EVENT + 1000, -2, -3)
    + pack("<HI", 25, 1)
    + pack("<qqqB", 1, 3000012, 1250, 1)
    + SYMBOL
)
(ROOT / "best_bid_ask.bin").write_bytes(
    header(50, 10001) + pack("<qqbbqqqq", EVENT, 100, -2, -3, 3000012, 1250, 3000013, 2000) + SYMBOL
)
(ROOT / "depth_snapshot.bin").write_bytes(
    header(18, 10002)
    + pack("<qqbb", EVENT, 100, -2, -3)
    + levels([(3000012, 1250)])
    + levels([])
    + SYMBOL
)
(ROOT / "depth_diff.bin").write_bytes(
    header(26, 10003)
    + pack("<qqqbb", EVENT, 99, 101, -2, -3)
    + levels([(3000012, 0)])
    + levels([(3000013, 2000)])
    + SYMBOL
)
