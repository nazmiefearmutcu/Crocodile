from pathlib import Path

import pytest


@pytest.fixture
def sbe_fixture():
    return lambda name: (Path(__file__).parent / "fixtures" / "sbe" / name).read_bytes()
