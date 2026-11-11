"""Every registered capability, called on every surface, with the answer inspected.

The three gates that should have caught most of this phase's findings **compare names and
never calls**. ``tests/conformance/test_surfaces.py`` is set equality over strings;
``tests/conformance/test_phase2_surface_parity.py`` admits in its own docstring that a
registered capability is "trusted to be projected rather than measured"; and 36 of the 57
wire names are never named in any test file that drives a surface at all. Under that, a
capability could be listed by every gate and answer ``'()' is not a valid Kind`` on one
surface, an empty ``inputSchema`` on another and 12 000 rows over a published ceiling of
10 000 on the third — which is what they were doing.

So this drives them. For each capability and each asset class it implements it builds a
request from the declared parameter schema, sends it through REST, MCP and the CLI, and
asserts the *answer is well formed* — not that it is correct, which no generic gate can
know, but that the shape a caller receives is one the projection is allowed to produce.

What "well formed" means here, and why each clause is in it:

* **REST** answers a status this projection classifies — 200, or one of the four failures
  ``dispatch`` names — with a JSON body. A 500 means an exception escaped classification,
  which is the finding that turned a missing environment variable into a retry storm.
* **MCP** answers a result and never a JSON-RPC error, carries the request ``id``, and its
  text parses as JSON. A protocol error tells an agent the *call* broke; a dropped id means
  the caller's future never resolves.
* **The CLI** exits 0 or 1 and never with a traceback. Exit 0 over an empty answer is the
  quietest failure this codebase has a history with, so a successful run must also have
  written something to stdout.
* **The three agree** about whether the request was answerable. One surface returning rows
  while another refuses the identical request is the divergence the projection exists to
  end, and it is what nothing was measuring.

**No network.** Sockets are replaced with one that raises :class:`_NetworkBlocked` before
any address is resolved, so a capability that reaches a venue fails deterministically and
in a way this file can recognise. Reaching for the network is not a failure of the
projection — it is out of this gate's scope and is recorded as such rather than being
silently counted as a pass. What *is* in scope is that the reach-out is reported the same
way on all three surfaces.

**Writes are not driven on the CLI**, and the exclusion is measured rather than listed: a
capability the network surfaces *refuse* (403) is one that writes to the lake, and the local
CLI would actually run it. Which ones those are is read off the REST answer in the same
test, so the list cannot go stale.
"""

from __future__ import annotations

import json
import pathlib
import socket
from typing import Any, Literal, Union, get_args, get_origin

import msgspec
import pytest
from typer.testing import CliRunner

from crocodile.core.capability import REGISTRY, Capability, ReturnKind
from crocodile.core.config import Settings
from crocodile.surfaces import cli, dispatch, rest, stdio
from tests.surfaces.conftest import END_NS, START_NS, SYMBOL


class _NetworkBlocked(OSError):
    """Raised instead of opening a socket, so "reached the network" is recognisable.

    A subclass of ``OSError`` because that is what every client library already expects a
    dead network to look like; the distinct type is what lets this file tell a capability
    that went looking for a venue apart from one that produced a badly-formed answer.
    """


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route out of the process, closed.

    ``socket.socket`` covers what ``aiohttp``, ``ccxt``, ``web3`` and ``requests`` all build
    on, and ``getaddrinfo``/``create_connection`` are closed too because a resolver can be
    reached without constructing a socket first.
    """

    real = socket.socket

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise _NetworkBlocked("this gate does not reach the network")

    def _refuse_internet(family: int = socket.AF_INET, *args: Any, **kwargs: Any) -> Any:
        # ``AF_UNIX`` is let through: asyncio builds its event-loop self-pipe out of a local
        # socket pair, and refusing that breaks the *test runner* rather than the capability.
        # Nothing local leaves the machine, which is the property this fixture is about.
        if family in (socket.AF_INET, socket.AF_INET6):
            raise _NetworkBlocked("this gate does not reach the network")
        return real(family, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", _refuse_internet)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


# ---------------------------------------------------------------------------
# A plausible request, built from the declaration
# ---------------------------------------------------------------------------

_BY_NAME: dict[str, Any] = {
    "symbol": SYMBOL,
    "symbols": SYMBOL,
    "underlying": SYMBOL,
    "sql": "SELECT 1 AS one",
    "q": "BTC",
    "channel": "trade",
    "channels": "trade",
    "source": "deribit",
    "sources": "deribit",
    "start_ns": str(START_NS),
    "end_ns": str(END_NS),
    "at_ns": str(START_NS),
    # `calendar` takes ISO dates. The crypto half answers 200 and the equity half, which gets
    # no `mic` here, answers a classified 400 on all three surfaces.
    "start": "2026-01-05",
    "end": "2026-01-09",
}
"""Values that mean something to this lake, keyed by the name the registry already uses.

Keyed by name rather than by type because a plausible ``start_ns`` and a plausible ``period``
are both integers and only one of them can be ``1``. The registry's naming is consistent
enough for this to be a small table — which is itself evidence that the parameter vocabulary
survived the merge — and anything not named here falls through to the type.
"""


def _by_type(declared: Any) -> Any:
    """A value of the right shape for a type this table has no name for."""
    arms = [arm for arm in get_args(declared) if arm is not type(None)]
    if get_origin(declared) in (Union, type(int | str)) and arms:
        declared = arms[0]
    if get_origin(declared) is Literal:
        return str(get_args(declared)[0])
    origin = get_origin(declared) or declared
    if isinstance(origin, type) and issubclass(origin, (list, tuple, set, frozenset)):
        # A sequence of objects is a JSON document; a sequence of scalars is comma text.
        item = (get_args(declared) or (str,))[0]
        return "[]" if get_origin(item) or item is dict else "1"
    if isinstance(origin, type) and issubclass(origin, dict):
        return "{}"
    if origin is bool:
        return "false"
    return "1"


def _request(cap: Capability, tmp_path: pathlib.Path) -> dict[str, str]:
    """Every required parameter of ``cap``, filled with something it could plausibly get.

    Only the required ones: a default that a surface mangles is the subject of its own gate
    (``test_no_synthesised_option_carries_a_sequence_as_its_click_default``), and leaving
    them out is what makes *this* gate exercise the defaults rather than paper over them.
    """
    supplied: dict[str, str] = {}
    for field in msgspec.structs.fields(cap.params):
        if not field.required:
            continue
        if field.name == "dest":
            supplied[field.name] = str(tmp_path / "out.parquet")
        elif field.name in _BY_NAME:
            supplied[field.name] = _BY_NAME[field.name]
        else:
            supplied[field.name] = _by_type(field.type)
    return supplied


def _blocked(error: BaseException | None) -> bool:
    """Whether this failure is the fixture refusing to let the process reach a venue."""
    while error is not None:
        if isinstance(error, _NetworkBlocked):
            return True
        error = error.__cause__ or error.__context__
    return False


def _mentions_the_block(text: str) -> bool:
    return "this gate does not reach the network" in text or "_NetworkBlocked" in text


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

_CLASSIFIED_STATUSES = {200, 400, 403, 501}
"""Every status this projection is allowed to produce. A 500 is an escaped exception."""


def _targets() -> list[tuple[str, str]]:
    """Every wire name against every asset class it implements — 89 calls, not 57."""
    dispatch.wire_names()
    return sorted(
        (wire, asset_class.value)
        for wire, name in dispatch.wire_names().items()
        for asset_class in REGISTRY[name].impls
    )


def test_there_is_something_to_drive() -> None:
    """A sweep over an empty registry is the vacuous gate this file exists to replace."""
    targets = _targets()
    assert len(targets) > 80, targets


@pytest.mark.parametrize(("wire", "asset_class"), _targets(), ids=lambda value: str(value))
def test_every_capability_answers_well_formed_on_every_surface(
    lake: pathlib.Path, tmp_path: pathlib.Path, wire: str, asset_class: str
) -> None:
    from starlette.testclient import TestClient

    settings = Settings(data_dir=lake)
    cap = dispatch.resolve(wire)
    arguments = {**_request(cap, tmp_path), "asset_class": asset_class}

    # --- REST -------------------------------------------------------------
    # ``raise_server_exceptions=True`` on purpose: an unclassified exception is re-raised
    # here instead of being flattened into a bodyless "Internal Server Error", which is the
    # only way to tell "this capability went looking for a venue" from "this projection let
    # something escape". A 4xx is still returned rather than raised, so nothing classified
    # changes shape.
    client = TestClient(rest.build_app(settings=settings))
    method = "get" if "GET" in rest.methods_for(cap) else "post"
    reached_out = False
    response = None
    try:
        response = (
            client.get(f"{rest.API_PREFIX}/{wire}", params=arguments)
            if method == "get"
            else client.post(f"{rest.API_PREFIX}/{wire}", json=arguments)
        )
    except BaseException as exc:  # re-raised below unless it is this file's own block
        if not _blocked(exc):
            raise AssertionError(
                f"{wire}/{asset_class} let {type(exc).__name__} escape classification "
                f"on REST: {exc}"
            ) from exc
        reached_out = True
    if response is not None:
        assert response.status_code in _CLASSIFIED_STATUSES, (
            f"{wire}/{asset_class} answered {response.status_code}: {response.text[:400]}"
        )
        body = response.json()
        if response.status_code == 200:
            provenance = body["provenance"]
            assert provenance["capability"] == cap.name
            key = "rows" if cap.returns is ReturnKind.TABLE else "result"
            assert key in body, f"{wire} declares {cap.returns} and answered {sorted(body)}"
            assert provenance["row_limit"] == dispatch.NETWORK_ROW_LIMIT
            if cap.returns is ReturnKind.TABLE:
                assert len(body["rows"]) <= dispatch.NETWORK_ROW_LIMIT
        else:
            assert isinstance(body["detail"], str) and body["detail"], body

    # --- MCP --------------------------------------------------------------
    answer = stdio.handle_request(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
         "params": {"name": wire, "arguments": dict(arguments)}},
        data_dir=lake,
    )
    assert answer["id"] == 11, answer
    assert "error" not in answer, answer
    text = answer["result"]["content"][0]["text"]
    parsed = json.loads(text)
    mcp_failed = bool(answer["result"].get("isError"))
    if not mcp_failed:
        assert parsed["provenance"]["capability"] == cap.name
        assert ("rows" in parsed) is (cap.returns is ReturnKind.TABLE)

    # --- the two network surfaces agree ------------------------------------
    if response is not None and not _mentions_the_block(text):
        assert (response.status_code == 200) is not mcp_failed, (
            f"{wire}/{asset_class}: REST said {response.status_code} and MCP "
            f"{'failed' if mcp_failed else 'succeeded'} — {text[:300]}"
        )

    # --- CLI ---------------------------------------------------------------
    # A capability the network surfaces refuse is one that writes, and the CLI would run it
    # for real. Read off the answer above rather than listed, so it cannot go stale.
    if reached_out or (response is not None and response.status_code == 403):
        return
    result = CliRunner().invoke(
        cli.build_app(),
        [wire, *_command_line(arguments), "--data-dir", str(lake)],
    )
    if _blocked(result.exception):
        return
    assert result.exit_code in (0, 1), result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"{wire}/{asset_class} left a traceback: {result.exception!r}"
    )
    if result.exit_code == 0:
        assert result.stdout.strip(), f"{wire}/{asset_class} exited 0 and printed nothing"


def _command_line(arguments: dict[str, str]) -> list[str]:
    """The same request, spelled as options."""
    return [
        part
        for name, value in arguments.items()
        for part in (f"--{name.replace('_', '-')}", value)
    ]


def test_the_sweep_notices_a_surface_that_stops_answering(
    lake: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate, broken on purpose, because a gate that has never failed proves nothing.

    Three shapes of breakage, each one a defect this phase actually found: an envelope that
    forgets its provenance block, a projector that lets an exception escape classification,
    and a JSON-RPC response that drops the caller's id.
    """
    from starlette.testclient import TestClient

    settings = Settings(data_dir=lake)
    arguments = {"asset_class": "crypto"}

    monkeypatch.setattr(dispatch, "provenance_block", lambda cap, ctx: {})
    body = TestClient(rest.build_app(settings=settings)).get(
        f"{rest.API_PREFIX}/catalog-summary", params=arguments
    ).json()
    with pytest.raises(KeyError):
        assert body["provenance"]["capability"]
    monkeypatch.undo()

    def _escape(cap: Any, ctx: Any, params: Any) -> Any:
        raise RuntimeError("nothing classifies this")

    monkeypatch.setattr(dispatch, "invoke", _escape)
    response = TestClient(
        rest.build_app(settings=settings), raise_server_exceptions=False
    ).get(f"{rest.API_PREFIX}/catalog-summary", params=arguments)
    assert response.status_code not in _CLASSIFIED_STATUSES
    assert not _mentions_the_block(response.text)
    monkeypatch.undo()

    monkeypatch.setattr(
        stdio, "handle_request", lambda request, **_: {"jsonrpc": "2.0", "error": {}}
    )
    assert "id" not in stdio.handle_request({}, data_dir=lake)
