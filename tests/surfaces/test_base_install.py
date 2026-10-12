"""The base dependency set must expose the CLI and keep offline helpers usable."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
_BLOCK_OPTIONAL = """
import importlib.abc
import sys

class BaseOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split('.')[0]
        if root in {'web3', 'eth_abi', 'ccxt', 'matplotlib', 'scipy', 'xgboost',
                    'fastapi', 'uvicorn', 'streamlit'}:
            raise ModuleNotFoundError('Optional dependency absent: ' + root, name=root)

sys.meta_path.insert(0, BaseOnly())
"""


def _base_process(code: str, args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    bootstrap = f"import sys; sys.path.insert(0, {str(_SRC)!r})\n" + _BLOCK_OPTIONAL
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", bootstrap + code, *(args or [])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "NO_COLOR": "1"},
    )


@pytest.mark.parametrize("command", ["crocodile", "crypcodile", "stockodile"])
def test_base_cli_help_keeps_all_commands(command: str) -> None:
    if command == "crocodile":
        code = "from crocodile.surfaces.entrypoint import main; main()"
    else:
        code = f"from crocodile._deprecated_cli import {command}_main as main; main()"
    result = _base_process(code, ["--help"])
    assert result.returncode == 0, result.stderr
    for name in ("query", "collect", "onchain-price", "lending-stress"):
        assert name in result.stdout
    if command != "crocodile":
        assert "removed in 0.4" in result.stderr


def test_base_cli_runs_a_lake_query(tmp_path: Path) -> None:
    result = _base_process(
        "from crocodile.surfaces.entrypoint import main; main()",
        ["query", "SELECT 1 AS one", "--asset-class", "crypto", "--data-dir", str(tmp_path)],
    )
    assert result.returncode == 0, result.stderr
    assert "one" in result.stdout


def test_base_offline_lending_and_transfer_helpers() -> None:
    result = _base_process("""
from crocodile.crypto.analytics.lending_stress import lending_stress_test
from crocodile.crypto.analytics.whale_transfers import (
    filter_transfers_by_usd, label_transfer_addresses,
)
stress = lending_stress_test(100, 40, 0.8, 20)
assert stress['current_health_factor'] == 2
assert stress['simulated_health_factor'] == 1.6
rows = filter_transfers_by_usd([{'from': '0xA', 'to': '0xB', 'usd_value': 25}], 20)
assert label_transfer_addresses(rows, {'0xa': 'known'})[0]['from_label'] == 'known'
print('offline helpers available')
""")
    assert result.returncode == 0, result.stderr
    assert "offline helpers available" in result.stdout


def test_missing_transfer_decoder_fails_before_rpc() -> None:
    result = _base_process("""
import asyncio
from types import SimpleNamespace
from crocodile.core.errors import ConfigError
from crocodile.crypto.analytics.whale_transfers import WhaleTransferTracker
tracker = WhaleTransferTracker(SimpleNamespace(to_checksum_address=lambda x: x), '0xA', 1)
try:
    asyncio.run(tracker.get_whale_transfers(1, 2))
except ConfigError as exc:
    assert "pip install 'crocodile[onchain]'" in str(exc)
else:
    raise AssertionError('Missing decoder was silently accepted')
""")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["onchain-price", "--symbol", "WETH-USDC"],
        ["base-market-data", "--token-pair", "WETH/USDC"],
    ],
)
def test_missing_onchain_dependency_has_an_install_hint(args: list[str], tmp_path: Path) -> None:
    result = _base_process(
        "from crocodile.surfaces.entrypoint import main; main()",
        [*args, "--asset-class", "crypto", "--data-dir", str(tmp_path)],
    )
    assert result.returncode == 1, result.stderr
    assert "pip install 'crocodile[onchain]'" in result.stderr
    assert "Traceback" not in result.stderr
