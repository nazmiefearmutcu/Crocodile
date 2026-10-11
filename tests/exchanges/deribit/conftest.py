import pytest

from crocodile.core.ingest.transport import FakeTransport


@pytest.fixture
def rpc_transport():
    class RpcTransport(FakeTransport):
        def __init__(self):
            super().__init__([])
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

    return RpcTransport()
