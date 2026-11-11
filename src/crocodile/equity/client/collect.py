"""Live collect orchestrator.

Runs N providers concurrently using asyncio.TaskGroup, each fully supervised.
SIGINT / CancelledError -> graceful sink.close(), then the dead letters are drained and
reported (spec 3.9), the same as the crypto collect does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from crocodile.core.ingest.deadletter import report_drained_dlqs
from crocodile.core.sink.base import Sink
from crocodile.equity.providers.base import Provider

log = logging.getLogger(__name__)


async def _run_isolated(provider: Provider, max_reconnects: int) -> None:
    """Run a provider, catching and logging any non-cancellation exception."""
    try:
        await provider.run(max_reconnects=max_reconnects)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error(
            "Provider %r raised an unhandled exception (isolated): %s",
            getattr(provider, "name", repr(provider)),
            exc,
            exc_info=True,
        )


def _report_dead_letters(
    providers: Sequence[Provider],
    report_path: Path | str | None,
    data_dir: Path | str | None,
) -> None:
    """Drain and report the providers' queues; a broken report must not mask the run's outcome."""
    try:
        report_drained_dlqs(providers, report_path=report_path, data_dir=data_dir)
    except Exception as exc:
        log.warning("DLQ drain/report failed (%s)", exc)


async def collect(
    providers: Sequence[Provider],
    sink: Sink,
    *,
    max_reconnects: int = -1,
    dlq_report_path: Path | str | None = None,
    data_dir: Path | str | None = None,
) -> None:
    """Run providers concurrently, writing all emitted Records into sink.

    After the sink is closed, every provider's dead letters are drained and reported to
    ``dlq_report_path``, or to ``<data_dir>/dlq_report.json`` when only ``data_dir`` is given.
    """
    if not providers:
        await sink.close()
        return

    _cancelled = False
    try:
        async with asyncio.TaskGroup() as tg:
            for provider in providers:
                tg.create_task(_run_isolated(provider, max_reconnects))
    except* asyncio.CancelledError:
        _cancelled = True
    except* Exception as eg:
        for exc in eg.exceptions:
            log.error("Unexpected group-level exception from collect(): %s", exc, exc_info=True)
    finally:
        try:
            await sink.close()
        finally:
            _report_dead_letters(providers, dlq_report_path, data_dir)

    if _cancelled:
        raise asyncio.CancelledError()
