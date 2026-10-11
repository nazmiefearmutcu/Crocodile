"""Order application decisions and sink batches across a shared capture."""

from collections.abc import Sequence

from crocodile.core.capture.schema import ApplicationDecision
from crocodile.core.capture.writer import CaptureWriter
from crocodile.core.schema.records import Record
from crocodile.core.sink.base import Sink


class CaptureEmitter:
    def __init__(self, writer: CaptureWriter, out: Sink):
        self.writer, self.out = writer, out

    async def emit(self, decision: ApplicationDecision, records: Sequence[Record]) -> None:
        async with self.writer.emission_lock:
            await self.writer.append(decision)
            for record in records:
                await self.out.put(record)
