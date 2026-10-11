import pytest
from typer.testing import CliRunner

from crocodile.core.errors import CaptureError
from crocodile.surfaces.cli import build_app


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "1.5"])
def test_invalid_capture_budget_exits_before_constructing_a_connector(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CROCODILE_RAW_CAPTURE", "true")
    monkeypatch.setenv("CROCODILE_RAW_CAPTURE_QUEUE_BYTES", value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid config reached connector factory")

    monkeypatch.setattr("crocodile.crypto.exchanges.factory.make_connector", forbidden)
    result = CliRunner().invoke(
        build_app(),
        [
            "collect",
            "--asset-class",
            "crypto",
            "--sources",
            "binance",
            "--symbols",
            "BTCUSDT",
            "--channels",
            "book_delta",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "RAW_CAPTURE_QUEUE_BYTES" in result.output


def test_capture_failure_is_cli_exit_one(tmp_path, monkeypatch):
    async def failed(*args, **kwargs):
        raise CaptureError("required capture failed")

    monkeypatch.setattr("crocodile.capabilities.ops.collect_crypto", failed)
    result = CliRunner().invoke(
        build_app(),
        [
            "collect",
            "--asset-class",
            "crypto",
            "--sources",
            "binance",
            "--symbols",
            "BTCUSDT",
            "--channels",
            "trade",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "required capture failed" in result.output
