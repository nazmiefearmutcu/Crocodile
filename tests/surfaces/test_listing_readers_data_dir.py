"""The listing-master readers (``universe``, ``markets``) honour a surface's ``--data-dir``.

``universe-sync --data-dir X`` writes the listing master under X, so ``universe`` on the same lake
must read it back (surface finding I2; the ruling makes the surface's data dir the lake root for
these two readers only, leaving the writers on CROCODILE_DATA_DIR as the README states).
"""

from __future__ import annotations

import json
import pathlib

import pytest
from typer.testing import CliRunner

from crocodile.core.markets.master import ListingMaster
from crocodile.surfaces import entrypoint, mcp
from tests.equity.universal.fakes import make_observation, seed_master

pytestmark = pytest.mark.usefixtures("network_forbidden")


def _master_under(lake: pathlib.Path) -> None:
    master = ListingMaster.for_data_dir(lake)
    try:
        seed_master(
            master,
            [
                make_observation(
                    "tradingview", "BIST:THYAO", "XIST", "THYAO", currency="TRY", price_scale=0
                )
            ],
        )
    finally:
        master.close()


def test_the_cli_universe_reads_the_listing_master_of_its_data_dir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CROCODILE_DATA_DIR", raising=False)
    lake = tmp_path / "lake"
    _master_under(lake)
    result = CliRunner().invoke(
        entrypoint.build_app(),
        ["universe", "--asset-class", "equity", "--mic", "XIST", "--data-dir", str(lake)],
    )
    assert result.exit_code == 0, result.output
    assert "THYAO" in result.output


def test_the_mcp_universe_reads_the_listing_master_of_its_data_dir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CROCODILE_DATA_DIR", raising=False)
    lake = tmp_path / "lake"
    _master_under(lake)
    body = mcp.call_tool("universe", {"asset_class": "equity", "mic": "XIST"}, data_dir=lake)
    assert "THYAO" in json.dumps(body, default=str)
