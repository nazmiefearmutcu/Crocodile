"""Live collect orchestrator (Task 3.4).

``collect(connectors, sink)`` runs N connectors concurrently using
``asyncio.TaskGroup``, each fully supervised (reconnect logic lives inside
``Connector.run()``).  SIGINT / ``CancelledError`` → graceful ``sink.close()``.

Each connector task is individually isolated: an unhandled exception from one
connector is caught, logged, and does **not** cancel the other tasks.  This is
achieved by wrapping each ``connector.run()`` call in a shielded helper that
swallows non-cancellation exceptions.

Design notes
------------
- ``asyncio.TaskGroup`` is used (Python 3.11+) for structured concurrency.
- Cancellation propagation: when the outer task is cancelled (e.g. SIGINT),
  ``TaskGroup.__aexit__`` cancels all child tasks.  We catch ``CancelledError``
  in the outer ``finally`` block to ensure ``sink.close()`` is called before
  re-raising.
- Connector isolation: each task wraps ``connector.run(max_reconnects=…)`` in a
  ``try/except Exception`` so that a crash in one connector is only logged, not
  propagated to the group.  ``CancelledError`` is **not** caught so that
  external cancellation still terminates everything.
- ``max_reconnects`` (default ``-1``, unlimited) is forwarded to every
  ``Connector.run()`` call.  For live collection the built-in backoff loop
  should remain active; individual permanent failures are still isolated by the
  ``except Exception`` wrapper.  Pass ``max_reconnects=0`` in tests where the
  transport exhausts naturally and you do not want reconnect delays.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.connector import Connector
from crocodile.core.errors import CaptureError
from crocodile.core.ingest.deadletter import report_drained_dlqs
from crocodile.core.sink.base import Sink

log = logging.getLogger(__name__)


async def _run_isolated(connector: Connector, max_reconnects: int) -> None:
    """Run a connector, catching and logging any non-cancellation exception."""
    try:
        await connector.run(max_reconnects=max_reconnects)
    except asyncio.CancelledError:
        raise  # must propagate so TaskGroup can cancel the group
    except CaptureError:
        raise
    except Exception as exc:
        if getattr(connector, "capture", None) is not None:
            raise CaptureError("Captured market source failed") from exc
        log.error(
            "Connector %r raised an unhandled exception (isolated): %s",
            getattr(connector, "name", repr(connector)),
            type(exc).__name__,
        )


async def collect(
    connectors: Sequence[Connector],
    sink: Sink,
    *,
    max_reconnects: int = -1,
    dlq_report_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    capture: CaptureWriter | None = None,
) -> None:
    """Run *connectors* concurrently, writing all emitted Records into *sink*.

    Args:
        connectors:     Sequence of configured ``Connector`` instances.  Each
                        must have a ``transport`` set before calling this
                        function (or rely on its own ``ws_url`` for live
                        connections).
        sink:           Destination sink.  ``sink.close()`` is called when all
                        connectors finish **or** on cancellation (e.g. SIGINT).
        max_reconnects: Forwarded to every ``Connector.run()`` call.
                        ``-1`` (default) = unlimited reconnects — correct for
                        live collection.  ``0`` = no reconnect; the connector
                        raises on the first failure, which is then isolated and
                        logged (useful when transports exhaust naturally in
                        tests).
        dlq_report_path: Optional explicit path for the dead-letter report JSON
                        written on stop when the DLQ is non-empty.
        data_dir:       If set and *dlq_report_path* is not, a non-empty DLQ
                        is written to ``{data_dir}/dlq_report.json``.

    Behaviour on errors:
        If a connector raises an exception it is logged and the remaining
        connectors continue uninterrupted.  ``CancelledError`` is always
        re-raised (so SIGINT / ``task.cancel()`` still terminates the run).

    Behaviour on cancellation:
        The ``CancelledError`` from the outer task is re-raised after
        ``sink.close()`` completes, giving callers the normal asyncio
        cancellation semantics.

    On stop (normal or cancelled), each connector's dead-letter queue is drained.
    If any items were present, a summary is logged and a JSON report may be
    written (see *dlq_report_path* / *data_dir*).
    """
    if capture is not None:
        for connector in connectors:
            connector.capture = capture

    # Check if target sink is a ParquetSink and spin up compactor
    compactor = None
    inferred_data_dir: Path | str | None = data_dir
    try:
        from crocodile.core.store.compactor import ParquetCompactor
        from crocodile.core.store.parquet_sink import ParquetSink

        target_sink = sink
        while hasattr(target_sink, "target"):
            target_sink = target_sink.target

        if isinstance(target_sink, ParquetSink):
            if inferred_data_dir is None:
                inferred_data_dir = target_sink._data_dir
            compactor = ParquetCompactor(
                data_dir=target_sink._data_dir, min_age_seconds=10.0, poll_interval=30.0
            )
            compactor.start()
    except Exception as e:
        log.warning("Could not initialize ParquetCompactor service: %s", e)

    _cancelled = False
    failure: CaptureError | None = None
    try:
        async with asyncio.TaskGroup() as tg:
            watcher = tg.create_task(capture.wait_failure()) if capture is not None else None
            tasks = []
            for connector in connectors:
                tasks.append(tg.create_task(_run_isolated(connector, max_reconnects)))
            await asyncio.gather(*tasks)
            if watcher is not None:
                watcher.cancel()
    except* asyncio.CancelledError:
        _cancelled = True
    except* CaptureError as eg:

        def first_error(group):
            for error in group.exceptions:
                if isinstance(error, CaptureError):
                    return error
                if isinstance(error, BaseExceptionGroup):
                    return first_error(error)
            return CaptureError("Required raw capture failed")

        failure = first_error(eg)
    except* Exception as eg:
        # All non-cancelled exceptions have already been swallowed inside
        # _run_isolated; this branch is a safety net that should never trigger
        # in normal operation.
        for exc in eg.exceptions:
            log.error("Unexpected group-level exception from collect(): %s", exc, exc_info=True)
    finally:

        async def cleanup():
            errors = []
            if compactor:
                try:
                    await compactor.stop()
                except Exception as exc:
                    log.warning("Compactor stop failed (%s)", type(exc).__name__)
            try:
                await sink.close()
            except Exception as exc:
                errors.append(exc)
            if capture is not None:
                try:
                    await capture.close()
                except Exception as exc:
                    errors.append(exc)
            try:
                report_drained_dlqs(
                    connectors, report_path=dlq_report_path, data_dir=inferred_data_dir
                )
            except Exception as exc:
                log.warning("DLQ drain/report failed (%s)", type(exc).__name__)
            if errors:
                if capture is not None:
                    raise CaptureError("Required capture cleanup failed") from errors[0]
                raise errors[0]

        close_task = asyncio.create_task(cleanup(), name="collect-cleanup")
        try:
            await asyncio.wait_for(asyncio.shield(close_task), 10)
        except asyncio.CancelledError:
            _cancelled = True
            try:
                await asyncio.wait_for(asyncio.shield(close_task), 10)
            except Exception as exc:
                failure = failure or CaptureError("Required capture cleanup did not complete")
                failure.__cause__ = exc
        except Exception as exc:
            if capture is None:
                raise
            if failure is None:
                failure = CaptureError("Required capture cleanup did not complete")
                failure.__cause__ = exc
            else:
                failure.add_note(f"Cleanup also failed ({type(exc).__name__})")
        finally:
            # Retrieve any late failure after the bounded caller has returned.
            close_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )

    if failure is not None:
        raise failure
    if _cancelled:
        raise asyncio.CancelledError()
