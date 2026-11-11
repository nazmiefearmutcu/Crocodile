"""The one socket path of the universal sources (spec section 3.10).

Every universal source sends its requests through :class:`AiohttpFetcher`. The fetcher sends a
generic User-Agent, never follows a redirect (a consent or bot wall must surface as a 3xx), maps
timeouts and connection failures to :class:`SourceUnservableError`, and retries nothing: retries
and backoff are the source client's decision. It never adds Origin, Referer or cookie headers
that would impersonate a browser.

Ruling (interfaces ruling 28): an aiohttp session belongs to the event loop that created it. A
fetcher used from a different running loop closes its old session best-effort and opens a new
one on the running loop; callers should still not share a fetcher across ``asyncio.run`` calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import aiohttp

from crocodile.equity.universal.base import SourceUnservableError, UnservableReason

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "USER_AGENT",
    "AiohttpFetcher",
    "Fetcher",
    "HttpRequest",
    "HttpResponse",
    "raise_if_unservable",
    "unservable_reason",
]

log = logging.getLogger(__name__)

USER_AGENT: Final[str] = "Mozilla/5.0 (crocodile)"
DEFAULT_TIMEOUT_S: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class HttpRequest:
    source: str  # source name, used in SourceUnservableError
    method: Literal["GET", "POST"]
    url: str
    params: tuple[tuple[str, str], ...] = ()
    json_body: Mapping[str, Any] | None = None
    headers: tuple[tuple[str, str], ...] = ()  # extra headers; User-Agent is the fetcher's


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]  # keys lower-cased by the fetcher
    body: bytes
    url: str

    @property
    def content_type(self) -> str:
        """Media type only, lower-case; "" when the header is absent."""
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value.split(";", 1)[0].strip().lower()
        return ""

    def looks_like_html(self) -> bool:
        return self.content_type == "text/html" or self.body.lstrip()[:1] == b"<"


type Fetcher = Callable[[HttpRequest], Awaitable[HttpResponse]]


class AiohttpFetcher:
    """The shared fetcher: lazy session, generic User-Agent, no redirects, no retries."""

    def __init__(
        self, *, user_agent: str = USER_AGENT, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self._user_agent = user_agent
        self._timeout_s = timeout_s
        self._session: aiohttp.ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __call__(self, request: HttpRequest) -> HttpResponse:
        session = await self._session_for_running_loop(request.source)
        headers = {**dict(request.headers), "User-Agent": self._user_agent}
        try:
            async with session.request(
                request.method,
                request.url,
                params=list(request.params) or None,
                json=request.json_body,
                headers=headers,
                allow_redirects=False,
            ) as response:
                body = await response.read()
                response_headers = {
                    str(key).lower(): str(value) for key, value in response.headers.items()
                }
                return HttpResponse(
                    status=response.status,
                    headers=response_headers,
                    body=body,
                    url=str(response.url),
                )
        except TimeoutError as exc:
            raise SourceUnservableError(
                request.source, UnservableReason.TIMEOUT, str(exc) or "request timed out"
            ) from exc
        except aiohttp.ClientError as exc:
            raise SourceUnservableError(
                request.source, UnservableReason.UNREACHABLE, str(exc) or type(exc).__name__
            ) from exc

    async def close(self) -> None:
        session, self._session, self._loop = self._session, None, None
        if session is not None:
            await session.close()

    async def _session_for_running_loop(self, source: str) -> aiohttp.ClientSession:
        loop = asyncio.get_running_loop()
        if self._session is not None and self._loop is loop:
            return self._session
        if self._session is not None:
            log.warning(
                "AiohttpFetcher for %s was used on a different event loop; closing the session "
                "of the earlier loop and opening one on the running loop. Do not share one "
                "fetcher across asyncio.run calls.",
                source,
            )
            old, self._session = self._session, None
            with contextlib.suppress(Exception):
                await old.close()
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout_s))
        self._loop = loop
        return self._session


def unservable_reason(
    response: HttpResponse, *, expect: Literal["json", "text"]
) -> UnservableReason | None:
    """300-399 -> BOT_CHALLENGE; 401/403 -> FORBIDDEN; 451 -> GEO_BLOCKED;
    2xx that looks_like_html() -> BOT_CHALLENGE (for both expectations); else None."""
    del expect  # both expectations classify the same walls
    status = response.status
    if 300 <= status <= 399:
        return UnservableReason.BOT_CHALLENGE
    if status in (401, 403):
        return UnservableReason.FORBIDDEN
    if status == 451:
        return UnservableReason.GEO_BLOCKED
    if 200 <= status <= 299 and response.looks_like_html():
        return UnservableReason.BOT_CHALLENGE
    return None


def raise_if_unservable(
    response: HttpResponse, *, source: str, expect: Literal["json", "text"]
) -> None:
    reason = unservable_reason(response, expect=expect)
    if reason is not None:
        raise SourceUnservableError(
            source,
            reason,
            f"HTTP {response.status}, content-type {response.content_type or 'none'}",
        )
