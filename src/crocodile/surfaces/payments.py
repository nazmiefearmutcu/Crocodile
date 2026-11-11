"""The x402 payment ledger and the rate limiter, which are process concerns, not queries.

Both forks carried a copy of this — an on-disk JSON ledger of payment ids, a sliding-window
limiter, and one paid route gated on a signature recovered from the payment id. It is here,
once, because ``simulate-payment`` and ``admin/payments`` are the two routes the surface
inventory classified as infrastructure rather than capabilities, and they need somewhere to
keep the ledger they administer.

What did **not** come across is the on-chain half: ``get_market_data`` verified a USDC
transfer on Base by fetching the receipt over RPC with a five-attempt failover, matching the
Transfer topic against a hardcoded contract address, and comparing the amount to
``PRICE_USDC``. That verification existed to gate exactly one route — the Base DEX price —
and that route is not in the registry, so the verifier now has nothing to admit anyone to.
Keeping four hundred lines of chain-state matching for a door with no room behind it is how
a codebase grows a second, unexercised copy of its trust model. The ledger stays because the
two administration routes stay; the door does not.

The store deliberately re-reads the file on every access rather than caching. Two processes
share one ledger — the REST server and whatever marks a payment paid — and a cached view is
how the same payment id gets served twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Final

__all__ = [
    "PaymentRecord",
    "PaymentsStore",
    "SlidingWindowRateLimiter",
    "payments_path",
]

PaymentRecord = dict[str, Any]
"""One ledger row: ``status`` plus whatever the route that wrote it recorded."""

_TEST_LEDGER: Final = "crocodile_payments_test.json"


def payments_path() -> Path:
    """Where the ledger lives.

    ``PAYMENTS_FILE`` wins, then a per-run temporary file under pytest, then
    ``~/.crypcodile/payments_db.json``. The pytest branch is not a convenience: without it a
    test run writes into the operator's real ledger, which is how a suite comes to depend on
    rows a previous suite left behind.

    The home directory is still ``~/.crypcodile`` because renaming it would orphan a live
    deployment's ledger; that is a migration with its own decision, and not this phase's.
    """
    override = os.environ.get("PAYMENTS_FILE")
    if override:
        return Path(override)
    if "pytest" in sys.modules:
        return Path(tempfile.gettempdir()) / _TEST_LEDGER
    home = os.path.join(os.path.expanduser("~"), ".crypcodile")
    return Path(home) / "payments_db.json"


class SlidingWindowRateLimiter:
    """Count requests per client over a rolling window.

    Returns ``True`` when the caller is *over* the limit, which is the sense both forks
    used and the sense every call site here reads.
    """

    def __init__(self, window_size: float = 60.0, max_requests: int = 100) -> None:
        self.window_size = window_size
        self.max_requests = max_requests
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check_rate_limit(self, client: str) -> bool:
        """Record one request from ``client`` and say whether it exceeds the window."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep > self.window_size:
                self._sweep(now)
                self._last_sweep = now
            cutoff = now - self.window_size
            recent = [seen for seen in self._seen.get(client, ()) if seen > cutoff]
            if len(recent) >= self.max_requests:
                self._seen[client] = recent
                return True
            recent.append(now)
            self._seen[client] = recent
            return False

    def _sweep(self, now: float) -> None:
        """Drop clients with nothing left in the window, so the table cannot grow forever."""
        cutoff = now - self.window_size
        for client in list(self._seen):
            recent = [seen for seen in self._seen[client] if seen > cutoff]
            if recent:
                self._seen[client] = recent
            else:
                del self._seen[client]


class PaymentsStore:
    """The ledger, read from and written to disk on every operation.

    Writes go through a temporary file and :func:`os.replace`, so a reader never observes a
    half-written ledger. The lock is this process's; the atomic rename is what makes the
    file safe against another one.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def lock(self) -> asyncio.Lock:
        """Held by a caller that reads, decides and writes as one step.

        ``simulate-payment`` is exactly that: it checks a payment is still ``pending`` and
        marks it ``paid``, and without the lock two concurrent calls both see ``pending``.
        """
        return self._lock

    async def all(self) -> dict[str, PaymentRecord]:
        return await asyncio.to_thread(self._read)

    async def get(self, payment_id: str) -> PaymentRecord | None:
        return (await self.all()).get(payment_id)

    async def set(self, payment_id: str, record: PaymentRecord) -> None:
        def write() -> None:
            ledger = self._read()
            ledger[payment_id] = record
            self._write(ledger)

        await asyncio.to_thread(write)

    def _read(self) -> dict[str, PaymentRecord]:
        path = payments_path()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return {}
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            # A corrupt ledger reads as empty rather than raising: the two routes that use
            # it administer payments, and refusing to start is a worse answer than saying
            # there are none. The file is left alone so it can be inspected.
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write(self, ledger: dict[str, PaymentRecord]) -> None:
        path = payments_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(ledger), encoding="utf-8")
        os.replace(tmp, path)
