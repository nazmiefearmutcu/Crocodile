"""The MCP surface, projected: one tool per capability.

Replaces 2 600 lines of hand-written crypto tool handlers and 800 of equity ones, and the
``_CAPABILITIES_MCP_TOOLS_HINT`` list that was a third hand-maintained copy of the same
names sitting inside the REST server.

Deliberately transport-free. This module produces the two things a JSON-RPC server needs —
a ``tools/list`` payload and a ``tools/call`` implementation — and nothing that speaks a
socket. A projection whose only entry point is a running server can only be tested by
starting one, which is how the legacy MCP handlers ended up with almost no coverage.

Trust posture: MCP is a network surface and is treated exactly as REST is — raw SQL vetted,
reads capped, cap published. The two agreeing is the point; they disagreed before, with
MCP passing ``readonly=True`` and REST wrapping a ``LIMIT``, each having independently
solved half the problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import msgspec

from crocodile.core.capability import REGISTRY, AssetClass, Capability
from crocodile.core.config import Settings
from crocodile.core.store.catalog import Catalog
from crocodile.surfaces import dispatch

__all__ = ["call_tool", "tool_definitions", "tool_names"]


def _definition(cap: Capability, wire: str) -> dict[str, Any]:
    """One MCP tool declaration.

    ``inputSchema`` is the params struct's own schema with the top-level ``$ref`` followed,
    because ``$ref`` at the root of an ``inputSchema`` is a form MCP clients do not read: the
    properties they offer a model come from ``properties``, and the previous version wrote
    ``setdefault("properties", {})`` beside the ``$ref`` — inserting an **empty object** next
    to the reference rather than resolving it. All 57 tools therefore published a tool with no
    inputs at all. ``$defs`` is carried through unchanged so any nested reference inside the
    followed schema still resolves; the reference is followed once, in
    :func:`dispatch.params_schema`, which is the same function the REST projection uses for
    its query parameters, so this is not a second description of the parameters.

    ``asset_class`` is added on top, because it is the one input a caller may supply that
    is not a capability parameter: it selects the implementation rather than describing the
    request. It was in neither the schema, the description nor the error text, and for the 31
    wire names with two implementations and no symbol field it is the *only* thing that makes
    the call answerable — so an agent reading the published contract could not call them, and
    found out with a 400. It is listed as required for exactly those.
    """
    schema: dict[str, Any] = dispatch.params_schema(cap)
    defs = msgspec.json.schema(cap.params).get("$defs")
    if defs:
        schema["$defs"] = defs
    schema["type"] = "object"
    properties = dict(schema.get("properties", {}))
    accepted = dispatch.asset_class_option_values(cap)
    mandatory = dispatch.requires_explicit_asset_class(cap)
    properties["asset_class"] = {
        "type": "string",
        "enum": accepted,
        "description": (
            "Which market answers. "
            + (
                "Required: this tool has an implementation for each of these and no symbol "
                "parameter to infer one from."
                if mandatory
                else "Inferred from the symbol's source when omitted."
            )
        ),
    }
    schema["properties"] = properties
    if mandatory:
        schema["required"] = [*schema.get("required", []), "asset_class"]
    return {
        "name": wire,
        "description": cap.summary if wire == cap.name else f"{cap.summary} (alias of {cap.name})",
        "inputSchema": schema,
        "assetClasses": accepted,
    }


def tool_definitions() -> list[dict[str, Any]]:
    """The ``tools/list`` payload: one entry per capability name and per alias."""
    return [
        _definition(REGISTRY[name], wire) for wire, name in sorted(dispatch.wire_names().items())
    ]


def tool_names() -> set[str]:
    """The tool names this projection actually publishes."""
    return {tool["name"] for tool in tool_definitions()}


def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one tool and return its response body.

    The body carries the same ``provenance`` block REST serves and, when the
    implementation's ceiling is not :attr:`Provenance.NATIVE`, the same human-readable
    ``warning``. That warning generalises the synthetic-depth banner the equity REST server
    already shipped for exactly one route: an agent reading a modelled answer is told so,
    for every capability that can produce one, without anyone writing fifty banners.

    Raises:
        KeyError: no tool of that name.
        ValueError: the arguments do not fit the capability's schema, or the
            implementation refused them.
        CapabilityUnavailable: no implementation for the resolved asset class.
    """
    cap = dispatch.resolve(name)
    supplied = dict(arguments or {})
    explicit_raw = supplied.pop("asset_class", None)
    explicit = AssetClass(explicit_raw) if explicit_raw else None

    params = dispatch.build_params(cap, supplied)
    resolved = dispatch.resolve_asset_class(
        cap, explicit=explicit, symbols=dispatch.symbol_hints(params)
    )
    resolved_settings = settings if settings is not None else Settings.from_env()

    with Catalog(dispatch.data_dir_for(resolved_settings, data_dir)) as catalog:
        ctx = dispatch.build_context(
            catalog,
            resolved,
            settings=dispatch.surface_settings(cap, resolved_settings, data_dir),
            readonly=True,
            row_limit=dispatch.NETWORK_ROW_LIMIT,
        )
        result = dispatch.drive(dispatch.invoke(cap, ctx, params), row_limit=ctx.row_limit)
        body = dispatch.payload(cap, result, row_limit=ctx.row_limit)
        body["provenance"] = dispatch.provenance_block(cap, ctx)
        warning = dispatch.warning_for(cap, ctx)
        if warning:
            body["warning"] = warning
        return body
