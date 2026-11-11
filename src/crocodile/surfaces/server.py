"""The deployable REST server: the projection's routes, plus the ones that operate it.

:mod:`crocodile.surfaces.rest` builds *only* the capability routes, because Gate 4 compares
its route table to the registry and a hand-written route in there would read as an invented
capability. This module mounts that app's routes and adds the operational ones beside them,
which is what ``crocodile api`` actually serves.

The operational routes are the ones the surface inventory classified as infrastructure,
minus two that are not being carried across:

``GET /api/events`` emitted ``round(2000 + random.random() * 100, 2)`` every two seconds to
animate the demo dashboard. Porting it would mean porting the fabrication — the inventory's
own words — and the dashboard it animated went with the Node portal.

The x402 *verification* path is gone with the one route it gated; see
:mod:`crocodile.surfaces.payments`. ``simulate-payment`` and ``admin/payments`` stay,
because administering the ledger is a real operation on the installation.

``/docs`` is FastAPI's own Swagger UI now rather than a hand-themed copy of it. Both forks
set ``docs_url=None`` and re-served it with 224 lines of inline CSS pointing at a CDN; the
theme is not a contract, and a hand-rolled ``<head>`` rewrite is a page that stops rendering
the next time that CDN moves.
"""

from __future__ import annotations

import os
import resource
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from crocodile.core.config import Settings
from crocodile.surfaces import mcp, rest
from crocodile.surfaces.payments import PaymentsStore, SlidingWindowRateLimiter

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from fastapi import FastAPI, Request, Response

__all__ = [
    "PaymentSignature",
    "build_server",
    "capabilities",
    "get_all_payments",
    "health",
    "metrics",
    "ready",
    "root_dashboard",
    "simulate_payment",
    "status",
    "version",
]

_STARTED = time.monotonic()
"""Process start, for the uptime gauge. Read at import, which is when the process began."""

_REQUESTS: dict[str, int] = {}
"""Requests served per path, filled by the counting middleware in :func:`build_server`.

Both forks bumped three hand-written globals from inside three handlers, so the other
forty-five routes were invisible to ``/metrics`` and nothing said so.
"""

_PAYMENTS = PaymentsStore()
_RATE_LIMITER = SlidingWindowRateLimiter(window_size=60.0, max_requests=100)


def _lake_dir(settings: Settings | None = None) -> Path:
    """The lake this server reads.

    Takes a ``Settings`` so a caller can hand one in rather than have the environment read
    behind its back — the property ``tests/conformance/test_data_dir_resolution.py`` asserts,
    and the reason sixteen scattered ``os.environ`` reads became one resolver.
    """
    return (settings if settings is not None else Settings.from_env()).data_dir


def _client(request: Request) -> str:
    """Who to rate-limit, trusting ``X-Forwarded-For`` only when told to.

    Trusting it unconditionally makes the limiter free to defeat: a caller sets the header
    to a fresh value per request and every request looks like a new client.
    """
    if os.environ.get("TRUST_FORWARDED_FOR", "false").lower() == "true":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client is not None else "unknown"


def _cors_origins() -> list[str]:
    """Origins a browser may call this server from, empty unless configured.

    Both forks shipped ``allow_origins=["*"]`` beside a payment-gated route, so any page
    could spend a visitor's payment ids from their browser. The wildcard was there for the
    demo dashboard, which was served from this same origin and did not need it, and which
    has gone with the Node portal. Opt in by name.
    """
    configured = os.environ.get("CROCODILE_CORS_ORIGINS", "")
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _health_body(settings: Settings | None = None) -> dict[str, Any]:
    """Liveness body: whether the lake answers, and what build is answering.

    ``ok`` is a statement about the *lake*, not the process — the process is plainly up if
    it replied. That is what lets ``ready`` return 503 off the same body.
    """
    from crocodile import __version__
    from crocodile.core.store.catalog import Catalog

    try:
        with Catalog(_lake_dir(settings)) as catalog:
            channels = len(catalog.list_channels())
    except Exception:
        return {
            "ok": False,
            "version": __version__,
            "lake_channels": 0,
            "error": "lake_unavailable",
        }
    return {"ok": True, "version": __version__, "lake_channels": channels}


async def health() -> dict[str, Any]:
    """Liveness probe: always 200, with the lake's reachability in the body."""
    return _health_body()


async def status() -> dict[str, Any]:
    """The same body as ``health``, under the second name both forks served it as."""
    return _health_body()


async def ready(response: Response) -> dict[str, Any]:
    """Readiness probe: ``health``'s body, 503 when the lake is unreachable.

    Separate from ``health`` because they answer different questions: liveness is "is this
    process alive", which it plainly is if it replied, and readiness is "should traffic be
    routed here", which it should not be while the lake is unreachable.
    """
    body = _health_body()
    if not body.get("ok"):
        response.status_code = 503
    return body


async def version() -> dict[str, str]:
    """The build answering, and nothing that touches a lake."""
    from crocodile import __version__

    return {"version": __version__}


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


async def metrics() -> Any:
    """Prometheus exposition of process telemetry and the payment ledger's shape."""
    from fastapi import Response

    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak_rss *= 1024  # Linux reports kilobytes; macOS reports bytes.

    by_status: dict[str, int] = {}
    for record in (await _PAYMENTS.all()).values():
        name = str(record.get("status", "unknown"))
        by_status[name] = by_status.get(name, 0) + 1

    lines = [
        "# HELP process_cpu_seconds_total Total CPU time spent in seconds.",
        "# TYPE process_cpu_seconds_total counter",
        f"process_cpu_seconds_total {time.process_time():.6f}",
        "",
        "# HELP process_resident_memory_peak_bytes Peak resident memory size in bytes.",
        "# TYPE process_resident_memory_peak_bytes gauge",
        f"process_resident_memory_peak_bytes {peak_rss}",
        "",
        "# HELP crocodile_uptime_seconds Uptime of the API server in seconds.",
        "# TYPE crocodile_uptime_seconds gauge",
        f"crocodile_uptime_seconds {time.monotonic() - _STARTED:.2f}",
        "",
        "# HELP crocodile_api_requests_total Requests served, per path.",
        "# TYPE crocodile_api_requests_total counter",
        *(
            f'crocodile_api_requests_total{{path="{path}"}} {count}'
            for path, count in sorted(_REQUESTS.items())
        ),
        "",
        "# HELP crocodile_payments_total Payment ledger rows, per status.",
        "# TYPE crocodile_payments_total gauge",
        # Every status present, rather than the two the forks named — one of which,
        # `verified`, is a status nothing in either tree ever wrote, so it read zero forever
        # while the `paid` and `spent` rows went uncounted.
        *(
            f'crocodile_payments_total{{status="{name}"}} {count}'
            for name, count in sorted(by_status.items())
        ),
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")


# ---------------------------------------------------------------------------
# The operational route table, which is also the self-description
# ---------------------------------------------------------------------------

_OPERATIONAL: tuple[tuple[str, str, Callable[..., Any]], ...] = (
    ("/api/v1/health", "health", health),
    ("/api/v1/status", "status", status),
    ("/api/v1/ready", "ready", ready),
    ("/api/v1/version", "version", version),
    ("/metrics", "metrics", metrics),
)
"""``(path, name, handler)`` for the routes that describe the process.

One table, read twice: :func:`build_server` registers from it and :func:`capabilities`
describes from it. That is the whole fix for the route this replaces — two hand-maintained
lists beside the routes they claimed to describe, which had already drifted.
"""


async def capabilities() -> dict[str, list[str]]:
    """Every route and tool this build serves.

    Answered from :func:`crocodile.surfaces.rest.route_paths`, :data:`_OPERATIONAL` and
    :func:`crocodile.surfaces.mcp.tool_names` — the same three things that are actually
    mounted — rather than from a hand-copied list. Both forks answered it from two Python
    lists maintained by remembering to, and they had already drifted: the MCP hint named 36
    tools while ``TOOLS`` declared 37, so ``list_all_exchanges`` existed and the one route
    whose job is saying what exists did not mention it.

    ``mcp_tools_hint`` keeps its key because callers parse it, but it is no longer a hint.

    Omitted for the reason the forks omitted them: ``/`` and ``/docs`` are pages rather than
    queries, and ``simulate-payment`` and ``admin/payments`` are ledger administration
    behind a key.
    """
    # The method comes off the built route rather than being written here: four capabilities
    # take parameters a query string cannot carry and are POST-only, and announcing `GET` for
    # every one of them would be exactly the stale hand-written claim this route replaced.
    paths = {
        f"{method} {path}" for path, methods in rest.route_methods().items() for method in methods
    }
    paths |= {f"GET {path}" for path, _name, _handler in _OPERATIONAL}
    paths.add("GET /api/v1/capabilities")
    return {"rest": sorted(paths), "mcp_tools_hint": sorted(mcp.tool_names())}


# ---------------------------------------------------------------------------
# x402 ledger administration
# ---------------------------------------------------------------------------


def _recover_signer(payment_id: str, signature: str) -> str:
    """Recover the address that signed ``payment_id``.

    Raises:
        ValueError: the signature is malformed or does not recover. Both are the caller's
            fault and the route reports them as one 400, because telling an unauthenticated
            caller which of the two it was is a probing oracle.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    body = signature.removeprefix("0x")
    if len(body) not in (128, 130):
        raise ValueError("Malformed signature.")
    try:
        bytes.fromhex(body)
        recovered = Account.recover_message(encode_defunct(text=payment_id), signature=signature)
    except Exception as exc:
        raise ValueError("Malformed signature.") from exc
    if not recovered:
        raise ValueError("Malformed signature.")
    return str(recovered)


def _allow_simulation() -> bool:
    """Simulation is off unless asked for. It marks a payment paid without one being made."""
    return os.environ.get("ALLOW_SIMULATION", "false").lower() == "true"


class PaymentSignature(msgspec.Struct, frozen=True):
    """The body ``simulate-payment`` takes: which payment, which transfer, and who signed.

    A declared body rather than the ``dict[str, str]`` it would otherwise be, because
    ``payment_id`` is the message the signature is recovered against — a request that
    reaches the recovery step with a missing field would recover a signer for the empty
    string, which is a valid address.
    """

    payment_id: str
    tx_hash: str
    signature: str


async def root_dashboard() -> Any:
    """Say what this is and where the two machine-readable answers are.

    It replaces the x402 demo dashboard, whose markup and JavaScript lived in the Node
    portal under ``crypto/legacy/api_portal/`` and animated one route that no longer exists.
    A landing page that links to the generated schema cannot go stale.
    """
    from fastapi.responses import HTMLResponse

    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Crocodile</title>"
        "<h1>Crocodile</h1>"
        "<p>Deterministic market data for crypto and equities, US and international.</p>"
        "<ul>"
        '<li><a href="/docs">/docs</a> &mdash; the generated API reference</li>'
        '<li><a href="/api/v1/capabilities">/api/v1/capabilities</a> &mdash; '
        "every route and tool this build serves</li>"
        '<li><a href="/api/v1/health">/api/v1/health</a> &mdash; liveness</li>'
        "</ul>"
    )


async def simulate_payment(payload: dict[str, str], request: Request) -> dict[str, Any]:
    """Mark a pending payment id paid, for exercising the ledger without paying.

    The order of the checks is the contract and is testable: rate limit, then the
    simulation gate, then the body, then the signature, then the ledger — so a caller with
    simulation disabled learns nothing about which payment ids exist.

    The body arrives as a mapping and is converted to :class:`PaymentSignature` here rather
    than annotated as one, because FastAPI builds its body model out of pydantic and this
    codebase's wire type is ``msgspec``. Annotating the struct directly raises at route
    registration; converting keeps one declaration of what the body is.
    """
    from fastapi import HTTPException

    if _RATE_LIMITER.check_rate_limit(_client(request)):
        raise HTTPException(status_code=429, detail="Too Many Requests")
    if not _allow_simulation():
        raise HTTPException(status_code=400, detail="Simulation mode is disabled.")

    try:
        body = msgspec.convert(payload, type=PaymentSignature)
    except msgspec.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        signer = _recover_signer(body.payment_id, body.signature)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with _PAYMENTS.lock:
        ledger = await _PAYMENTS.all()
        record = ledger.get(body.payment_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Payment ID not found.")
        if record.get("status") != "pending":
            raise HTTPException(status_code=400, detail="Payment already processed.")
        if any(
            other != body.payment_id and other_record.get("tx_hash") == body.tx_hash
            for other, other_record in ledger.items()
        ):
            raise HTTPException(status_code=400, detail="Transaction hash already processed.")
        record |= {"status": "paid", "tx_hash": body.tx_hash, "sender": signer}
        await _PAYMENTS.set(body.payment_id, record)
    return {"status": "success", "payment_id": body.payment_id, "payment_record": record}


async def get_all_payments(request: Request) -> dict[str, Any]:
    """Dump the ledger, behind ``ADMIN_API_KEY``.

    With no key configured the route answers 404 rather than 401: a 401 tells an
    unauthenticated caller the endpoint is there and worth guessing at.
    """
    import hmac

    from fastapi import HTTPException

    expected = os.environ.get("ADMIN_API_KEY")
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    supplied = request.headers.get("x-admin-key") or ""
    if not supplied:
        authorization = request.headers.get("authorization") or ""
        supplied = authorization.removeprefix("Bearer ").removeprefix("bearer ").strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await _PAYMENTS.all()


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_server(*, settings: Settings | None = None, data_dir: Path | None = None) -> FastAPI:
    """Return the app ``crocodile api`` serves: every capability, plus the operator's routes.

    The capability routes are taken from :func:`crocodile.surfaces.rest.build_app` rather
    than rebuilt, so there is exactly one place a capability becomes a route and this module
    cannot serve a different set than Gate 4 measured.
    """
    from fastapi import FastAPI, Response
    from fastapi import Request as LiveRequest
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse

    from crocodile import __version__

    # FastAPI resolves a handler's parameters through ``get_type_hints`` against this
    # module's globals, and ``from __future__ import annotations`` turns every one of them
    # into a string. ``Request`` and ``Response`` are imported under TYPE_CHECKING because
    # FastAPI lives in the ``web`` extra, so the lookup would fail and FastAPI would fall
    # back to treating them as ordinary query parameters — every route answering
    # 422 "Field required: query.request". Binding the live classes here is what lets the
    # deferred import and the annotations coexist; ``rest.py`` does the same thing for the
    # same reason.
    globals()["Request"] = LiveRequest
    globals()["Response"] = Response

    resolved = settings if settings is not None else Settings.from_env()
    app = FastAPI(
        title="Crocodile",
        description="Every capability, projected from one registry.",
        version=__version__,
    )
    origins = _cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"]
        )

    for route in rest.build_app(settings=resolved, data_dir=data_dir).routes:
        if str(getattr(route, "path", "")).startswith(f"{rest.API_PREFIX}/"):
            app.routes.append(route)

    # The capability routes serve two methods each and FastAPI writes one operationId per
    # route, so this app needs the same correction the projection's own app applies.
    rest.publish_one_operation_id_per_method(app)

    for path, name, handler in _OPERATIONAL:
        app.add_api_route(path, handler, methods=["GET"], name=name)
    app.add_api_route(
        "/api/v1/capabilities", capabilities, methods=["GET"], name="capabilities"
    )

    @app.middleware("http")
    async def count_requests(request: Request, call_next: Callable[..., Any]) -> Any:
        _REQUESTS[request.url.path] = _REQUESTS.get(request.url.path, 0) + 1
        return await call_next(request)

    app.add_api_route(
        "/",
        root_dashboard,
        methods=["GET"],
        include_in_schema=False,
        response_class=HTMLResponse,
    )
    app.add_api_route("/api/v1/simulate-payment", simulate_payment, methods=["POST"])
    app.add_api_route(
        "/api/v1/admin/payments", get_all_payments, methods=["GET"], include_in_schema=False
    )
    return app
