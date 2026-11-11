"""Which failures are the caller's, which are policy, and which are ours.

Three answers, one classification, three surfaces reading it. Getting this wrong is not a
cosmetic problem: a 500 is retried by every client that backs off on 5xx and pages whoever
owns the alerting, so serving a deliberate refusal or a SQL typo as one turns a caller's
mistake into an operator's night.

Two failures were measured at the exit review:

* ``_refuse_readonly`` raises ``PermissionError`` *because* "a REST projection maps it to
  403 and a caller must not retry" — and no surface caught it, so ``backfill`` and
  ``collect`` answered 500 and MCP answered ``-32603``.
* A DuckDB error in ``query`` went 400 in the legacy server and 500 here, because
  ``duckdb.CatalogException`` is neither ``CrocodileError`` nor ``ValueError``. Every user
  SQL typo became a 5xx.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from crocodile.core.capability import REGISTRY, Capability, Impl
from crocodile.core.config import Settings
from crocodile.surfaces import cli, dispatch, mcp, rest, stdio
from tests.surfaces.conftest import END_NS, START_NS, SYMBOL


def _settings(lake: pathlib.Path) -> Settings:
    return Settings(data_dir=lake)


def _client(lake: pathlib.Path):  # starlette's TestClient, imported lazily
    from starlette.testclient import TestClient

    return TestClient(rest.build_app(settings=_settings(lake)), raise_server_exceptions=False)


_BACKFILL = {
    "source": "deribit",
    "channel": "trade",
    "symbols": SYMBOL,
    "start_ns": str(START_NS),
    "end_ns": str(END_NS),
    "asset_class": "crypto",
}
_COLLECT = {
    "sources": "deribit",
    "symbols": SYMBOL,
    "channels": "trade",
    "duration_seconds": "1",
    "asset_class": "crypto",
}


# ---------------------------------------------------------------------------
# A refusal is not a crash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "arguments"), [("backfill", _BACKFILL), ("collect", _COLLECT)])
def test_a_write_refused_by_the_surfaces_posture_is_403(
    lake: pathlib.Path, name: str, arguments: dict[str, str]
) -> None:
    """The parameters were fine; this surface is not trusted to run the capability."""
    response = _client(lake).get(f"/api/v1/{name}", params=arguments)
    assert response.status_code == 403, response.text
    assert "read-only" in response.json()["detail"]


@pytest.mark.parametrize(("name", "arguments"), [("backfill", _BACKFILL), ("collect", _COLLECT)])
def test_mcp_reports_a_refusal_as_a_tool_error_rather_than_a_protocol_error(
    lake: pathlib.Path, name: str, arguments: dict[str, str]
) -> None:
    """``-32603 Internal error`` tells an agent the call broke, not that it was refused."""
    response = stdio.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": name, "arguments": dict(arguments)}},
        data_dir=lake,
    )
    assert "error" not in response, response
    assert "read-only" in response["result"]["content"][0]["text"]


def test_the_cli_reports_a_refusal_instead_of_a_traceback(
    lake: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI is not read-only, so the refusal it can meet is the filesystem's."""
    original = REGISTRY["catalog-summary"]

    def _refuse(ctx: Any, params: Any) -> dict[str, Any]:
        raise PermissionError("the lake directory is not readable by this account")

    monkeypatch.setitem(
        REGISTRY,
        "catalog-summary",
        Capability(
            name=original.name,
            summary=original.summary,
            params=original.params,
            returns=original.returns,
            aliases=original.aliases,
            impls={
                asset_class: Impl(fn=_refuse, prov=impl.prov, basis=impl.basis)
                for asset_class, impl in original.impls.items()
            },
        ),
    )
    result = CliRunner().invoke(
        cli.build_app(),
        ["catalog-summary", "--asset-class", "crypto", "--data-dir", str(lake)],
    )
    assert result.exit_code == 1
    assert "not readable" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# ---------------------------------------------------------------------------
# SQL that does not compile against this lake
# ---------------------------------------------------------------------------


_BAD_SQL = {"sql": "SELECT * FROM no_such_table", "asset_class": "crypto"}


def test_sql_that_does_not_compile_is_a_bad_request(lake: pathlib.Path) -> None:
    """The legacy server answered 400 here; 500 trips alerting for a caller's typo."""
    response = _client(lake).get("/api/v1/query", params=_BAD_SQL)
    assert response.status_code == 400, response.text
    assert "no_such_table" in response.json()["detail"]


def test_the_cli_reports_bad_sql_instead_of_a_traceback(lake: pathlib.Path) -> None:
    result = CliRunner().invoke(
        cli.build_app(),
        ["query", "--sql", "SELECT * FROM no_such_table", "--asset-class", "crypto",
         "--data-dir", str(lake)],
    )
    assert result.exit_code == 1, result.output
    assert "no_such_table" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_mcp_reports_bad_sql_as_a_tool_error(lake: pathlib.Path) -> None:
    response = stdio.handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "query", "arguments": dict(_BAD_SQL)}},
        data_dir=lake,
    )
    assert "error" not in response, response
    assert "no_such_table" in response["result"]["content"][0]["text"]


def test_the_classification_names_the_statement_and_not_the_environment() -> None:
    """A DuckDB failure is only the caller's when it is about their statement.

    The legacy route caught ``Exception`` and answered 400 for all of them, which is the
    opposite failure: a lake that cannot be read is not a bad request, and reporting it as
    one tells the caller to fix a query that was fine.
    """
    assert issubclass(duckdb.CatalogException, dispatch.BAD_REQUEST)
    assert issubclass(duckdb.ParserException, dispatch.BAD_REQUEST)
    assert issubclass(duckdb.ConversionException, dispatch.BAD_REQUEST)
    assert not issubclass(duckdb.IOException, dispatch.BAD_REQUEST)
    assert not issubclass(duckdb.OutOfMemoryException, dispatch.BAD_REQUEST)


def test_a_bug_in_an_implementation_is_still_a_500(
    lake: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, and the reason none of this is ``except Exception``.

    Widening the catch until every failure is the caller's is how a broken server reports
    itself as a bad request and nobody is paged.
    """
    original = REGISTRY["catalog-summary"]

    def _break(ctx: Any, params: Any) -> dict[str, Any]:
        raise RuntimeError("a genuine bug")

    monkeypatch.setitem(
        REGISTRY,
        "catalog-summary",
        Capability(
            name=original.name,
            summary=original.summary,
            params=original.params,
            returns=original.returns,
            aliases=original.aliases,
            impls={
                asset_class: Impl(fn=_break, prov=impl.prov, basis=impl.basis)
                for asset_class, impl in original.impls.items()
            },
        ),
    )
    response = _client(lake).get("/api/v1/catalog-summary", params={"asset_class": "crypto"})
    assert response.status_code == 500, response.text
    with pytest.raises(RuntimeError, match="a genuine bug"):
        mcp.call_tool("catalog-summary", {"asset_class": "crypto"}, settings=_settings(lake))


# ---------------------------------------------------------------------------
# A JSON-RPC response the caller can match to its request
# ---------------------------------------------------------------------------


_UNMATCHED_COLUMNS = {
    # `gas-vol` correlates two caller-supplied series and needs `local_ts` on each. A
    # document without it is the caller's own array being wrong, and polars says so with
    # `ColumnNotFoundError` — which is neither `CrocodileError` nor `ValueError`, so it
    # escaped `handle_request` entirely.
    "gas": '[{"ts": 1, "gwei": 2}]',
    "vol": '[{"x": 1}]',
    "asset_class": "crypto",
}


def _one_line(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    """Feed the read loop exactly one line and then EOF, so ``serve_stdio`` returns.

    A ``BytesIO`` rather than a pipe: the loop reads ``sys.stdin.buffer`` directly, and an
    empty read is how it learns the peer has gone.
    """
    import io

    monkeypatch.setattr(
        stdio.sys, "stdin", type("_Pipe", (), {"buffer": io.BytesIO(payload)})()
    )


def test_a_failing_tool_answers_with_the_id_the_caller_is_waiting_on(
    lake: pathlib.Path,
) -> None:
    """A JSON-RPC client matches responses to requests by id and has nothing else.

    Before: the failure escaped to the read loop, whose handler built
    ``{'jsonrpc', 'error'}`` — no ``id`` — so the caller's future never resolved. On a
    long-lived stdio session it never would.
    """
    response = stdio.handle_request(
        {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
         "params": {"name": "gas-vol", "arguments": dict(_UNMATCHED_COLUMNS)}},
        data_dir=lake,
    )
    assert response["id"] == 17
    assert "error" not in response, response
    assert response["result"]["isError"] is True
    assert "local_ts" in response["result"]["content"][0]["text"]


def test_a_success_is_not_marked_as_an_error(lake: pathlib.Path) -> None:
    """``isError`` has to distinguish, which means it must be absent on the happy path."""
    response = stdio.handle_request(
        {"jsonrpc": "2.0", "id": 18, "method": "tools/call",
         "params": {"name": "catalog-summary", "arguments": {"asset_class": "crypto"}}},
        data_dir=lake,
    )
    assert response["id"] == 18
    assert "isError" not in response["result"], response


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("gas-vol", _UNMATCHED_COLUMNS),
        ("query", {"sql": "SELECT * FROM no_such_table", "asset_class": "crypto"}),
        ("no-such-tool", {}),
    ],
    ids=["escaping-library-error", "caller-sql", "unknown-tool"],
)
def test_no_tool_failure_reaches_the_caller_as_a_protocol_error(
    lake: pathlib.Path, name: str, arguments: dict[str, str]
) -> None:
    """``-32603`` is the code for "this server broke", and every client may retry it.

    ``_REPORTED`` was deciding two things and only one was its own: it chose the wording, and
    it also chose whether the agent heard anything at all rather than a transport fault. It
    keeps the first. Three shapes are driven — a library exception it never named, one it
    did, and a name that is not a tool — because the property is that *none* of them is a
    protocol error, not that the list got one entry longer.
    """
    response = stdio.handle_request(
        {"jsonrpc": "2.0", "id": 19, "method": "tools/call",
         "params": {"name": name, "arguments": dict(arguments)}},
        data_dir=lake,
    )
    assert response["id"] == 19
    assert "error" not in response, response
    assert response["result"]["isError"] is True


async def test_the_read_loop_carries_the_id_even_when_the_handler_itself_fails(
    lake: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last line of defence, driven by breaking the handler rather than by inspection.

    ``handle_request`` is not supposed to raise. The loop's ``except`` exists because it
    might, and it was building a response with no ``id`` — which is exactly the case where a
    client most needs one, since the alternative is a request that never completes.
    """
    def _explode(request: dict[str, Any], *, data_dir: Any = None) -> dict[str, Any]:
        raise RuntimeError("the handler itself is broken")

    monkeypatch.setattr(stdio, "handle_request", _explode)
    _one_line(monkeypatch, b'{"jsonrpc":"2.0","id":23,"method":"tools/list"}\n')
    await stdio.serve_stdio(data_dir=lake)

    written = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert written, "the loop answered nothing at all"
    answer = json.loads(written[0])
    assert answer["id"] == 23
    assert answer["error"]["code"] == -32603


async def test_a_line_that_is_not_json_is_a_parse_error_with_a_null_id(
    lake: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one case where there is no id to carry, which is why the parse is separate."""
    _one_line(monkeypatch, b"{not json\n")
    await stdio.serve_stdio(data_dir=lake)

    written = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    answer = json.loads(written[0])
    assert answer["id"] is None
    assert answer["error"]["code"] == -32700


# ---------------------------------------------------------------------------
# A deployment that was never configured is not a broken server
# ---------------------------------------------------------------------------


_NEEDS_A_CONTACT_STRING = {"asset_class": "equity"}
"""``census``/equity counts the US reference universe, which reads SEC EDGAR, and SEC blocks a
request with no identifying User-Agent. (``markets``/equity used to read it too and no longer does:
it reads the bundled exchange table, so it needs no contact string.)

``Settings.sec_user_agent`` is deliberately undefaulted — an invented address passes the
string check and gives the regulator a dead mailbox — so an unconfigured install raises
``ConfigError`` before contacting anything.
"""


def _unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CROCODILE_SEC_USER_AGENT", raising=False)


def test_a_configuration_error_is_an_actionable_501_rather_than_a_bare_500(
    lake: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: ``500 Internal Server Error`` with no body, from the same exception the CLI
    printed the fix for.

    5xx is the one status class every backing-off client retries, so an unset environment
    variable became a retry storm against an endpoint whose answer will never change until an
    operator acts. 501 is what this projection already serves for a capability with no
    implementation, and it is the same sentence.
    """
    _unconfigured(monkeypatch)
    response = _client(lake).get("/api/v1/census", params=_NEEDS_A_CONTACT_STRING)
    assert response.status_code == 501, response.text
    assert "CROCODILE_SEC_USER_AGENT" in response.json()["detail"]


def test_the_three_surfaces_say_the_same_thing_about_an_unconfigured_deployment(
    lake: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property, rather than the status code: every surface names the variable to set."""
    _unconfigured(monkeypatch)

    from_cli = CliRunner().invoke(
        cli.build_app(),
        ["census", "--asset-class", "equity", "--data-dir", str(lake)],
    )
    assert from_cli.exit_code == 1
    assert "CROCODILE_SEC_USER_AGENT" in from_cli.output

    from_rest = _client(lake).get("/api/v1/census", params=_NEEDS_A_CONTACT_STRING)
    assert "CROCODILE_SEC_USER_AGENT" in from_rest.json()["detail"]

    from_mcp = stdio.handle_request(
        {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
         "params": {"name": "census", "arguments": dict(_NEEDS_A_CONTACT_STRING)}},
        data_dir=lake,
    )
    assert "error" not in from_mcp, from_mcp
    assert "CROCODILE_SEC_USER_AGENT" in from_mcp["result"]["content"][0]["text"]


def test_a_connector_fault_is_still_ours_and_still_a_5xx(lake: pathlib.Path) -> None:
    """The line the category is drawn on, stated so widening it later is a decision.

    ``ConfigError`` is "detected before any source is contacted" and cannot fix itself.
    A venue that timed out is a fault, a retry is the right behaviour, and 5xx is what says
    so — which is why the category names one family and not ``CrocodileError``.
    """
    from crocodile.core.errors import ConfigError, CrocodileError, TransientConnectorError

    assert issubclass(ConfigError, dispatch.NOT_CONFIGURED)
    assert not issubclass(TransientConnectorError, dispatch.NOT_CONFIGURED)
    assert not issubclass(CrocodileError, dispatch.NOT_CONFIGURED)
