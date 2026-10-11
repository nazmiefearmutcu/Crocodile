"""The clock both asset classes share.

``ms_to_ns`` and ``us_to_ns`` had silently diverged: crypto computed
``int(ms) * 1e6``, truncating to whole milliseconds *before* scaling, while
equity computed ``int(ms * 1e6)`` and kept the fraction. Same name, same
signature, different number — ``ms_to_ns(2.5)`` was 2 000 000 on one side and
2 500 000 on the other.

The merge takes equity's. The evidence decides it rather than seniority: the
crypto suite only ever asserts integer inputs, where both forms agree, so no
crypto expectation covers the difference; the equity suite pins the fractional
case directly. Truncating discards precision a venue actually sent, and nothing
depended on the discarding.
"""

import time
from datetime import UTC, datetime


def ms_to_ns(ms: int | float) -> int:
    """Milliseconds to nanoseconds, keeping any sub-millisecond fraction."""
    return int(ms * 1_000_000)


def us_to_ns(us: int | float) -> int:
    """Microseconds to nanoseconds, keeping any sub-microsecond fraction."""
    return int(us * 1_000)


def now_ns() -> int:
    """Capture clock for local_ts. Realtime so it's comparable to source_ts."""
    if hasattr(time, "clock_gettime_ns") and hasattr(time, "CLOCK_REALTIME"):
        return int(time.clock_gettime_ns(getattr(time, "CLOCK_REALTIME", 0)))
    return time.time_ns()


def rfc3339_to_ns(dt_str: str) -> int:
    """Parse an RFC-3339 timestamp with arbitrary subsecond precision to nanoseconds.

    Carried over from the equity side, which is the only one that receives
    string timestamps: crypto venues send epoch integers, REST providers send
    RFC-3339. ``datetime.fromisoformat`` caps at microseconds, so the fraction
    is parsed by hand to keep nanosecond inputs exact.
    """
    offset_str = "+00:00"
    dt_part = dt_str

    if dt_str.endswith("Z"):
        offset_str = "+00:00"
        dt_part = dt_str[:-1]
    else:
        t_idx = dt_str.find("T")
        if t_idx != -1:
            plus_idx = dt_str.rfind("+", t_idx)
            minus_idx = dt_str.rfind("-", t_idx)
            idx = max(plus_idx, minus_idx)
            if idx != -1:
                offset_str = dt_str[idx:]
                dt_part = dt_str[:idx]

    if "." in dt_part:
        base, frac = dt_part.split(".", 1)
        frac = frac[:9].ljust(9, "0")
        subseconds_ns = int(frac)
    else:
        base = dt_part
        subseconds_ns = 0

    dt_with_offset = datetime.fromisoformat(f"{base}{offset_str}")
    secs = int(dt_with_offset.astimezone(UTC).timestamp())
    return secs * 1_000_000_000 + subseconds_ns
