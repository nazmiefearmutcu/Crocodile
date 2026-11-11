"""The one socket path of the universal sources (task 10a, step 3).

Every test here is offline: the real ``aiohttp.ClientSession`` is replaced either by the
``network_forbidden`` fixture (which must never be reached) or by a recording fake that
captures the request arguments and returns canned responses.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import pytest
from yarl import URL

from crocodile.equity.universal.base import SourceUnservableError, UnservableReason
from crocodile.equity.universal.http import (
    USER_AGENT,
    AiohttpFetcher,
    HttpRequest,
    HttpResponse,
    raise_if_unservable,
    unservable_reason,
)


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str) -> None:
        self.status = status
        self.headers = headers
        self._body = body
        self.url = url

    async def read(self) -> bytes:
        return self._body


class _RequestContext:
    def __init__(self, session: _FakeSession, method: str, url: str, kwargs: dict[str, Any]):
        self._session = session
        self._call = (method, url, kwargs)

    async def __aenter__(self) -> _FakeResponse:
        if self._session.error is not None:
            raise self._session.error
        self._session.calls.append(self._call)
        assert self._session.response is not None
        return self._session.response

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.close_awaited = False
        self.response: _FakeResponse | None = None
        self.error: BaseException | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> _RequestContext:
        return _RequestContext(self, method, url, kwargs)

    async def close(self) -> None:
        self.close_awaited = True


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: _FakeResponse | None = None,
    error: BaseException | None = None,
) -> list[_FakeSession]:
    created: list[_FakeSession] = []

    def factory(*args: object, **kwargs: object) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)
        session.response = response
        session.error = error
        created.append(session)
        return session

    monkeypatch.setattr("aiohttp.ClientSession", factory)
    return created


def _request(**overrides: Any) -> HttpRequest:
    fields: dict[str, Any] = {
        "source": "yahoo_chart",
        "method": "GET",
        "url": "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS",
    }
    fields.update(overrides)
    return HttpRequest(**fields)


def test_the_fetcher_opens_no_session_until_called(network_forbidden) -> None:
    AiohttpFetcher()


def test_the_fetcher_is_the_only_socket_path(network_forbidden) -> None:
    fetcher = AiohttpFetcher()
    with pytest.raises(AssertionError, match="Offline replay attempted network construction"):
        asyncio.run(fetcher(_request()))


def test_requests_carry_the_generic_user_agent_and_do_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(
        200,
        {"Content-Type": "application/json; charset=utf-8", "X-Mixed-Case": "v"},
        b'{"chart": {}}',
        "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS?interval=1d",
    )
    created = _install_fake_session(monkeypatch, response=response)
    fetcher = AiohttpFetcher()
    request = _request(
        method="GET",
        params=(("period1", "1"), ("interval", "1d")),
        headers=(("Accept", "application/json"),),
    )

    result = asyncio.run(fetcher(request))

    assert len(created) == 1
    ((method, url, kwargs),) = created[0].calls
    assert method == "GET"
    assert url == request.url
    assert kwargs["headers"]["User-Agent"] == "Mozilla/5.0 (crocodile)" == USER_AGENT
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["allow_redirects"] is False
    assert list(kwargs["params"]) == [("period1", "1"), ("interval", "1d")]
    assert result.status == 200
    assert result.headers == {
        "content-type": "application/json; charset=utf-8",
        "x-mixed-case": "v",
    }
    assert result.body == b'{"chart": {}}'
    assert result.url == response.url


def test_the_json_body_is_forwarded_on_post(monkeypatch: pytest.MonkeyPatch) -> None:
    created = _install_fake_session(
        monkeypatch, response=_FakeResponse(200, {}, b"[]", "https://example.invalid/p")
    )
    request = _request(
        method="POST", url="https://example.invalid/p", json_body={"symbols": ["THYAO"]}
    )
    asyncio.run(AiohttpFetcher()(request))
    ((method, _url, kwargs),) = created[0].calls
    assert method == "POST"
    assert kwargs["json"] == {"symbols": ["THYAO"]}


def test_a_timeout_is_unservable_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_session(monkeypatch, error=TimeoutError("slow"))
    with pytest.raises(SourceUnservableError) as caught:
        asyncio.run(AiohttpFetcher()(_request()))
    assert caught.value.source == "yahoo_chart"
    assert caught.value.reason is UnservableReason.TIMEOUT


def test_a_connection_error_is_unservable_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_session(monkeypatch, error=aiohttp.ClientConnectionError("connection reset"))
    with pytest.raises(SourceUnservableError) as caught:
        asyncio.run(AiohttpFetcher()(_request()))
    assert caught.value.reason is UnservableReason.UNREACHABLE


def test_a_new_event_loop_gets_a_new_session(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    response = _FakeResponse(200, {}, b"{}", "https://example.invalid/x")
    created = _install_fake_session(monkeypatch, response=response)
    fetcher = AiohttpFetcher()

    with caplog.at_level(logging.WARNING):
        asyncio.run(fetcher(_request()))
        asyncio.run(fetcher(_request()))

    assert len(created) == 2
    assert created[0].close_awaited is True
    assert len(created[0].calls) == 1
    assert len(created[1].calls) == 1
    misuse = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(misuse) == 1


def test_a_cookie_set_by_one_response_is_not_replayed_on_the_next_request() -> None:
    """Regression (fix round 1): the default aiohttp CookieJar stores Set-Cookie answers and
    replays them. The fetcher must use a dummy jar so no consent or bot cookie carries over."""
    url = URL("https://query2.finance.yahoo.com/v8/finance/chart/AAPL")
    cookie = {"consent": "yes"}

    async def scenario() -> tuple[object, int, int]:
        control = aiohttp.CookieJar()
        control.update_cookies(cookie, url)
        stored_by_default = len(control.filter_cookies(url))  # the probe really stores cookies

        fetcher = AiohttpFetcher()
        session = await fetcher._session_for_running_loop("yahoo_chart")
        try:
            jar = session.cookie_jar
            jar.update_cookies(cookie, url)
            return jar, stored_by_default, len(jar.filter_cookies(url))
        finally:
            await fetcher.close()

    jar, stored_by_default, replayed = asyncio.run(scenario())
    assert stored_by_default == 1
    assert isinstance(jar, aiohttp.DummyCookieJar)
    assert replayed == 0


def test_two_calls_inside_one_event_loop_share_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(200, {}, b"{}", "https://example.invalid/x")
    created = _install_fake_session(monkeypatch, response=response)
    fetcher = AiohttpFetcher()

    async def two_calls() -> None:
        await fetcher(_request())
        await fetcher(_request())

    asyncio.run(two_calls())

    assert len(created) == 1
    assert len(created[0].calls) == 2
    assert created[0].close_awaited is False


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            HttpResponse(302, {"location": "https://consent.example/"}, b"", "u"),
            UnservableReason.BOT_CHALLENGE,
        ),
        (HttpResponse(403, {}, b"", "u"), UnservableReason.FORBIDDEN),
        (HttpResponse(401, {}, b"", "u"), UnservableReason.FORBIDDEN),
        (HttpResponse(451, {}, b"", "u"), UnservableReason.GEO_BLOCKED),
        (
            HttpResponse(200, {"content-type": "text/html"}, b"<html>", "u"),
            UnservableReason.BOT_CHALLENGE,
        ),
        (
            HttpResponse(200, {"content-type": "application/json"}, b"<html>", "u"),
            UnservableReason.BOT_CHALLENGE,
        ),
        (HttpResponse(200, {"content-type": "application/json"}, b'{"chart": {}}', "u"), None),
        (HttpResponse(404, {"content-type": "application/json"}, b"{}", "u"), None),
    ],
)
@pytest.mark.parametrize("expect", ["json", "text"])
def test_unservable_reason_classifies_walls(
    response: HttpResponse, expected: UnservableReason | None, expect: str
) -> None:
    assert unservable_reason(response, expect=expect) is expected  # type: ignore[arg-type]


def test_raise_if_unservable_names_the_source_and_reason() -> None:
    response = HttpResponse(403, {}, b"", "https://query2.finance.yahoo.com/v8/x")
    with pytest.raises(SourceUnservableError) as caught:
        raise_if_unservable(response, source="yahoo_chart", expect="json")
    assert caught.value.reason is UnservableReason.FORBIDDEN
    assert str(caught.value).startswith("yahoo_chart is unservable (forbidden):")


def test_raise_if_unservable_passes_a_clean_response() -> None:
    raise_if_unservable(
        HttpResponse(200, {"content-type": "application/json"}, b"{}", "u"),
        source="yahoo_chart",
        expect="json",
    )


@pytest.mark.parametrize(
    "module",
    [
        "crocodile.equity.universal.http",
        "crocodile.equity.universal.base",
        "crocodile.equity.universal.yahoo_chart",
    ],
)
def test_each_universal_module_imports_cold_in_a_fresh_interpreter(module: str) -> None:
    """Regression: importing base or http first used to fail on the providers/factory cycle."""
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
