"""The REST surface, projected: one route per capability under ``/api/v1``.

Replaces 3 273 lines of hand-written crypto routes and 1 500 of equity ones. It also
replaces ``GET /api/v1/capabilities``, which was a hand-copied list of route strings
sitting beside the routes it described — fifty-odd names maintained by remembering to.
Here the same route is the registry, so it cannot be stale.

FastAPI is imported inside :func:`build_app`, not at module scope. It lives in the ``web``
extra, and a module that cannot be imported on the base install would break the import gate
in ``tests/conformance/test_import_safety.py`` — an optional dependency is allowed to be
absent, but not at import time.

Trust posture: this is the network surface. Raw SQL is vetted and reads are capped at
:data:`~crocodile.surfaces.dispatch.NETWORK_ROW_LIMIT`, and the cap is published in the
provenance block so a truncated answer reads as a ceiling rather than as the whole lake.

The response envelope
---------------------
Every route answers ``{"rows": [...]}`` or ``{"result": ...}``, plus ``provenance`` and,
when the answer is not a native observation, ``warning``. Some legacy routes returned the
payload bare — ``GET /api/v1/slippage`` was a one-element array, so ``resp.json()[0]`` used
to work and now raises ``KeyError: 0``. The envelope stays, because ``provenance`` and the
SYNTHETIC banner are the whole point of the projection and a bare array has nowhere to put
them: an answer that does not say it was modelled is the failure this package exists to
end. The place a caller is told is the published schema — :func:`_openapi_responses` puts
the envelope in ``responses.200`` for every route, derived from :class:`ReturnKind` — and
the ``/docs`` page renders it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from crocodile.core.capability import REGISTRY, AssetClass, Capability
from crocodile.core.config import Settings
from crocodile.core.store.catalog import Catalog
from crocodile.surfaces import dispatch

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Callable, Coroutine

    from fastapi import FastAPI, Request

__all__ = [
    "API_PREFIX",
    "build_app",
    "methods_for",
    "publish_one_operation_id_per_method",
    "route_methods",
    "route_paths",
]

API_PREFIX = "/api/v1"
"""The prefix both forks already served on. Kept so existing callers do not move."""


def path_for(wire: str) -> str:
    """The route a capability answers on. One rule, no table."""
    return f"{API_PREFIX}/{wire}"


def methods_for(cap: Capability) -> list[str]:
    """Which HTTP methods this capability answers on, derived from its parameters.

    Every capability answers ``POST``, because a body is the only transport with no length
    limit and the caller is the one who knows their statement is 20 kB. Six routes the forks
    served on ``POST`` were ``GET``-only here and answered 405 — the frozen fixture records
    them, and the parity scanner strips the method before comparing, which is why no gate
    saw it. Retrying ``query`` as a GET is not a workaround: nginx's default
    ``large_client_header_buffers`` is 8 k, so a real statement 414s, and whatever does fit
    is written to access logs and browser history.

    A capability additionally answers ``GET`` when every parameter can be spelled in a URL.
    The four that cannot — ``gas-vol``, ``mev-sandwich``, ``smart-money``,
    ``label-transfers``, whose parameters are arrays of objects — are ``POST``-only, so a
    GET gets a 405 naming the method that works instead of a 400 about a field the caller
    had no way to send. That is also exactly what both forks served for them.

    :func:`dispatch.structured_fields` is the whole rule and it reads the parameter
    declaration. A list of capability names in this file would be the fourth copy of the
    registry, and it would be wrong the first time a parameter changed type.

    :class:`ReturnKind` was the other candidate and is deliberately not used. ``STREAM`` is
    the one thing the registry can identify as not-a-read, but a ``STREAM`` on this surface
    is refused by the read-only posture before it can hold anything, and ``backfill`` and
    ``export`` write too without being one — so keying on it would buy an inconsistency
    rather than a rule. What would settle it properly is a *capability* that declares it
    mutates the lake, which the registry does not have and this projection cannot invent.
    """
    return ["POST"] if dispatch.structured_fields(cap) else ["GET", "POST"]


def _openapi_parameters(cap: Capability) -> list[dict[str, Any]]:
    """Describe the query string from ``cap.params``, for the generated OpenAPI document.

    The same schema MCP publishes as its ``inputSchema``, reshaped into OpenAPI's parameter
    list. Two surfaces, one description of what a caller may send — which is half of what
    "full API symmetry" means and the half that is easiest to lose by hand.

    A field a URL cannot carry is left out rather than listed with its JSON Schema type: a
    published query parameter is a promise about what a caller may put in a query string,
    and ``?trades=[{…}]`` is a promise the transport cannot keep. Those fields are in
    :func:`_openapi_request_body` instead, which is where they can be sent.
    """
    schema = dispatch.params_schema(cap)
    required = set(schema.get("required", []))
    structured = dispatch.structured_fields(cap)
    properties = schema.get("properties", {})
    parameters: list[dict[str, Any]] = [
        _query_parameter(name, subschema, required=name in required)
        for name, subschema in properties.items()
        if name not in structured
    ]
    parameters.append(
        {
            "name": "asset_class",
            "in": "query",
            # Mandatory on every route whose capability has two implementations and no
            # symbol to infer from: `resolve_asset_class` refuses outright there, so
            # `required: false` was describing a request that can only answer 400. The enum
            # is narrowed to what this capability implements for the same reason — a route
            # publishing a class it has no implementation for describes a 501.
            "required": dispatch.requires_explicit_asset_class(cap),
            "schema": {"type": "string", "enum": dispatch.asset_class_option_values(cap)},
            "description": (
                "Which market answers. Inferred from the symbol's source when omitted, "
                "where the capability takes one."
            ),
        }
    )
    return parameters


ARRAY_SERIALISATION: dict[str, Any] = {"style": "form", "explode": True}
"""How this surface serialises an array in a query string: ``?k=a&k=b``.

Stated on the parameter rather than left to OpenAPI's default — which is the same thing —
because a default nobody wrote down is a default nothing can be checked against, and the
disagreement this fixes was exactly that. The document said ``{"type": "array"}`` and the
handler said ``dict(request.query_params)``, which keeps the **last** value of a repeated
key: ``?rates=0.001&rates=0.002&rates=0.003&rates=0.004`` answered 200 with
``predicted_funding_rate: 0.004`` and ``n_history: 1``. A client that read the published
schema and did what it said got a wrong number and no error.

The wire is what was made to agree with the schema, not the other way round: repeated keys
are what the OpenAPI default *means*, every generated client already emits them, and the
comma form the two forks served — ``?symbols=BTC,ETH`` — keeps working underneath because
:func:`dispatch.build_params` splits a single string on commas. So a repeated key and a
comma list are both accepted and both mean the same thing, and only one of them has to be
published to be true.

:func:`test_every_published_array_parameter_is_parsed_the_way_it_is_published` is what stops
the two drifting again: it reads this declaration off the generated document and drives the
route with it.
"""


def _query_parameter(name: str, subschema: dict[str, Any], *, required: bool) -> dict[str, Any]:
    """One entry in the published parameter list, carrying its serialisation when it needs one."""
    parameter: dict[str, Any] = {
        "name": name,
        "in": "query",
        "required": required,
        "schema": subschema,
    }
    if _is_array(subschema):
        parameter |= ARRAY_SERIALISATION
        parameter["description"] = (
            "Repeat the key once per value. A single comma-separated value is also accepted."
        )
    return parameter


def _is_array(subschema: dict[str, Any]) -> bool:
    """Whether this published parameter is a sequence, through a nullable union or not.

    ``msgspec.json.schema`` writes an optional sequence as ``anyOf`` with a ``null`` arm, so
    reading only the top-level ``type`` would leave every optional array — which is most of
    them — published without a serialisation and parsed with one.
    """
    if subschema.get("type") == "array":
        return True
    return any(
        isinstance(arm, dict) and arm.get("type") == "array"
        for arm in subschema.get("anyOf", ())
    )


def _openapi_request_body(cap: Capability) -> dict[str, Any]:
    """The same parameters again, as the JSON object a ``POST`` carries.

    One operation object covers both methods — FastAPI writes a single route into the path
    item under each of its methods — so a route that answers GET and POST publishes both a
    parameter list and a body. That is what the endpoint accepts, and describing only one of
    them would leave a caller guessing which.
    """
    return {
        "required": bool(dispatch.structured_fields(cap)),
        "content": {"application/json": {"schema": dispatch.params_schema(cap)}},
    }


def _openapi_responses(cap: Capability) -> dict[str, Any]:
    """The envelope, published rather than discovered.

    See the module docstring: the payload is wrapped, some legacy routes returned it bare,
    and this is where a caller finds that out. Derived from :attr:`Capability.returns`, so
    it cannot describe a shape the projection does not actually serve.
    """
    from crocodile.core.capability import ReturnKind

    payload: dict[str, Any] = (
        {
            "rows": {"type": "array", "items": {"type": "object"}},
            "truncated": {
                "type": "boolean",
                "description": (
                    "Present and true when the answer was cut at provenance.row_limit. "
                    "Absent means the rows are the whole answer — which is what tells a "
                    "caller who received exactly row_limit rows which of the two they have."
                ),
            },
        }
        if cap.returns is ReturnKind.TABLE
        else {"result": {"description": "The single value or object this capability returns."}}
    )
    return {
        "200": {
            "description": (
                "The capability's answer, wrapped with the provenance of the implementation "
                "that produced it."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            **payload,
                            "provenance": {
                                "type": "object",
                                "description": (
                                    "Which implementation answered, its provenance ceiling, "
                                    "the registered basis of its inputs, and the row ceiling "
                                    "this surface applied."
                                ),
                            },
                            "warning": {
                                "type": "string",
                                "description": (
                                    "Present when the answer is not a venue-reported "
                                    "observation. Read it before the numbers."
                                ),
                            },
                        },
                        "required": ["provenance"],
                    }
                }
            },
        }
    }


def _query_values(request: Request) -> dict[str, Any]:
    """The query string as parameter values, keeping every value of a repeated key.

    ``dict(request.query_params)`` was silently lossy: Starlette's ``QueryParams`` is a
    multidict and collapsing it keeps only the last value, so the published array form —
    ``?rates=0.001&rates=0.002&rates=0.003&rates=0.004``, which is what OpenAPI's default
    serialisation and therefore every generated client emits — arrived as one number. The
    response was ``200`` with ``n_history: 1``: a wrong answer, in range, with no error on it.
    See :data:`ARRAY_SERIALISATION`.

    A key that appears **once** stays a string rather than becoming a one-element list, and
    that is deliberate rather than incidental: it is what keeps the comma form working, since
    :func:`dispatch.build_params` splits a string for a sequence field and does not split a
    list. So ``?symbols=BTC,ETH`` and ``?symbols=BTC&symbols=ETH`` reach the implementation
    as the same tuple, which is the only outcome under which the two forks' existing callers
    and a schema-following client are both right.

    Repeating a key that is *not* declared as a sequence is left to
    :func:`dispatch.build_params` to reject against the real type, rather than being resolved
    here by taking one of them — a surface quietly choosing which of two values a caller meant
    is the shape of failure this whole finding is.
    """
    collected: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key not in collected:
            collected[key] = value
        elif isinstance(collected[key], list):
            collected[key].append(value)
        else:
            collected[key] = [collected[key], value]
    return collected


async def _body_values(request: Request) -> dict[str, Any]:
    """The request body as parameter values, or nothing at all.

    Absent and empty are the same thing here — a GET has no body and a POST may legitimately
    put everything in the query string — but *malformed* is not: a body that is not JSON, or
    that is JSON but not an object, is a caller error and says so. Falling back to an empty
    dict would answer a broken request as though it had asked for nothing, which is how a
    typo comes back as the whole lake.

    Raises:
        HTTPException: 400, the body is not a JSON object.
    """
    import msgspec
    from fastapi import HTTPException

    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        decoded = msgspec.json.decode(raw)
    except msgspec.DecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"request body is not valid JSON: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                f"request body must be a JSON object naming this capability's parameters, "
                f"not a {type(decoded).__name__}"
            ),
        )
    return decoded


def _handler(
    cap: Capability, settings: Settings, data_dir: Path | None
) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """Return the endpoint for one capability. The whole of the per-capability code."""
    from fastapi import HTTPException, Request
    from starlette.concurrency import run_in_threadpool

    def serve(supplied: dict[str, Any]) -> dict[str, Any]:
        explicit_raw = supplied.pop("asset_class", None)
        try:
            asset_class = AssetClass(explicit_raw) if explicit_raw else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            params = dispatch.build_params(cap, supplied)
            resolved = dispatch.resolve_asset_class(
                cap, explicit=asset_class, symbols=dispatch.symbol_hints(params)
            )
        except dispatch.UNAVAILABLE as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except dispatch.NOT_CONFIGURED as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except dispatch.BAD_REQUEST as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        with Catalog(dispatch.data_dir_for(settings, data_dir)) as catalog:
            ctx = dispatch.build_context(
                catalog,
                resolved,
                settings=settings,
                readonly=True,
                row_limit=dispatch.NETWORK_ROW_LIMIT,
            )
            try:
                result = dispatch.drive(
                    dispatch.invoke(cap, ctx, params), row_limit=ctx.row_limit
                )
            except dispatch.UNAVAILABLE as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except dispatch.NOT_CONFIGURED as exc:
                # The message names the variable to set. It reached the CLI and not this
                # surface, which answered a bare 500 — and 5xx is the one class every
                # backing-off client retries, so an unset environment variable became a
                # retry storm against an endpoint whose answer will never change.
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except dispatch.REFUSED as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except dispatch.BAD_REQUEST as exc:
                # An unknown indicator, a symbol with no stored book, SQL that does not
                # compile against this lake: a bad request, not a broken server. The legacy
                # routes mapped these the same way.
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            body = dispatch.payload(cap, result, row_limit=ctx.row_limit)
            body["provenance"] = dispatch.provenance_block(cap, ctx)
            warning = dispatch.warning_for(cap, ctx)
            if warning:
                body["warning"] = warning
            return body

    async def endpoint(request: Request) -> dict[str, Any]:
        # The query string and the body are read into one request rather than one winning.
        # ``asset_class`` is a query parameter on every surface and the fields that do not
        # fit in a URL can only be in the body, so a caller routinely needs both. The body
        # takes precedence on a collision because it is the richer of the two encodings —
        # a query string only ever has strings in it.
        supplied = _query_values(request)
        supplied |= await _body_values(request)
        # Off the event loop, for two reasons that are really one. Every capability here is
        # blocking — DuckDB, Parquet, HTTP — so serving it inline stalls every other request
        # on the process; and `dispatch.drive` calls `asyncio.run` for a capability that
        # handed back an unstarted coroutine, which raises outright inside a running loop.
        # A worker thread has no loop of its own, so both stop being true at once.
        return await run_in_threadpool(serve, supplied)

    # FastAPI resolves a route's parameters through ``get_type_hints`` against the module
    # globals. ``from __future__ import annotations`` turns ``request: Request`` into the
    # string ``"Request"``, and ``Request`` is imported inside this function rather than at
    # module scope (see the module docstring on the ``web`` extra) — so the lookup fails,
    # FastAPI falls back to treating it as an ordinary query parameter, and every route
    # answers 422 "Field required: query.request". Binding the live class here is what
    # makes the deferred import and the annotation coexist.
    endpoint.__annotations__ = {"request": Request, "return": dict[str, Any]}
    return endpoint


def build_app(*, settings: Settings | None = None, data_dir: Path | None = None) -> FastAPI:
    """Return a FastAPI app holding one route per capability and per alias.

    Infrastructure routes — ``/health``, ``/ready``, ``/version``, ``/metrics``, ``/docs``,
    ``/``, ``/api/events``, the x402 payment routes — are deliberately absent. They are not
    capabilities: they have no asset class, no parameter schema and no provenance, so
    registering them would put them in front of the symmetry gate where the only possible
    answer is an exemption. They stay hand-written on the server that mounts this app.
    """
    from fastapi import FastAPI

    resolved_settings = settings if settings is not None else Settings.from_env()
    app = FastAPI(
        title="Crocodile",
        description="Every capability, projected from one registry.",
    )
    for wire, name in sorted(dispatch.wire_names().items()):
        cap = REGISTRY[name]
        summary = cap.summary if wire == name else f"{cap.summary}  [alias of {name}]"
        app.add_api_route(
            path_for(wire),
            _handler(cap, resolved_settings, data_dir),
            # One route with both methods, never two routes with one each: Gate 4 counts the
            # paths this app serves and a second registration of the same path reads as the
            # same capability projected twice.
            methods=methods_for(cap),
            name=wire,
            summary=summary,
            openapi_extra={
                "parameters": _openapi_parameters(cap),
                "requestBody": _openapi_request_body(cap),
                "responses": _openapi_responses(cap),
            },
        )
    publish_one_operation_id_per_method(app)
    return app


_METHOD_SUFFIXES = ("_get", "_post", "_put", "_patch", "_delete", "_head", "_options")


def publish_one_operation_id_per_method(app: FastAPI) -> None:
    """Give each method of a two-method route its own ``operationId``.

    OpenAPI requires ``operationId`` to be unique across every operation in a document, and
    FastAPI derives one per *route* — so a route serving GET and POST writes the same id
    under both, and warns once per route that it has done so. A generated client would come
    out with two methods of one name, or one silently overwriting the other.

    Two routes per capability would avoid it and cannot be had: Gate 4 counts the paths this
    app serves and a second registration of the same path reads as the same capability
    projected twice. So the document is corrected after it is generated, which is the only
    point at which the method is known.

    Applied by :func:`build_app` and by the server that mounts these routes beside its own,
    because the schema is generated per app and each one would otherwise write its own
    invalid copy.
    """
    import warnings

    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        with warnings.catch_warnings():
            # Suppressed because the next four lines are the fix for exactly what it warns
            # about, and only for that message: anything else FastAPI has to say is kept.
            warnings.filterwarnings("ignore", message="Duplicate Operation ID")
            schema = original()
        for item in schema.get("paths", {}).values():
            for method, operation in item.items():
                identifier = operation.get("operationId") if isinstance(operation, dict) else None
                if not isinstance(identifier, str):
                    continue
                for suffix in _METHOD_SUFFIXES:
                    identifier = identifier.removesuffix(suffix)
                operation["operationId"] = f"{identifier}_{method}"
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def route_paths() -> set[str]:
    """The paths this projection actually serves.

    Read off the built app, so the parity gate measures the surface rather than re-deriving
    the answer it is supposed to be checking.
    """
    paths = {str(getattr(route, "path", "")) for route in build_app().routes}
    return {path for path in paths if path.startswith(f"{API_PREFIX}/")}


def route_methods() -> dict[str, list[str]]:
    """Each served path with the methods it answers on, read off the built app.

    The methods are half of what a caller needs to reach a route, and the self-description
    route was announcing ``GET`` for everything — which was true until four capabilities
    became ``POST``-only, and would then have been the same kind of stale hand-written claim
    that route exists to make impossible.
    """
    found: dict[str, list[str]] = {}
    for route in build_app().routes:
        path = str(getattr(route, "path", ""))
        methods = getattr(route, "methods", None) or ()
        if path.startswith(f"{API_PREFIX}/"):
            found[path] = sorted(m for m in methods if m not in {"HEAD", "OPTIONS"})
    return found
