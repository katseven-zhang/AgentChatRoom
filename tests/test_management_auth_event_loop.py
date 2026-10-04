"""Task #271: cookie authentication must not do database work on the loop.

Every management-cookie request passed through ``ManagementAuthASGIMiddleware``,
which called a synchronous ``authenticated`` closure that queried
``management_sessions`` inline - so one database round trip per request ran on the
ASGI event loop and serialised the SSE streams behind it. The bearer/no-auth
short circuits stay inline; the cookie lookup is a thread off-loop.
"""

from __future__ import annotations

import asyncio

import pytest

from agentchatroom.api import ManagementAuthASGIMiddleware


def _scope(path: str = "/api/v1/projects") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8765),
    }


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


class _Send:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


async def _call(middleware, scope, send):
    await middleware(scope, _receive, send)


@pytest.mark.asyncio
async def test_middleware_awaits_an_asyncible_authentication_check():
    calls = []

    async def authenticated(request):
        await asyncio.sleep(0)
        calls.append("authenticated")
        return False

    async def app(scope, receive, send):
        calls.append("app")

    middleware = ManagementAuthASGIMiddleware(app, authenticated=authenticated)
    send = _Send()

    await _call(middleware, _scope(), send)

    assert calls == ["authenticated"]
    assert send.messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_protected_requests_do_not_serialise_behind_authentication():
    """Two cookie requests overlap instead of queueing on the event loop."""
    delay = 0.05

    async def authenticated(request):
        await asyncio.sleep(delay)
        return True

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ManagementAuthASGIMiddleware(app, authenticated=authenticated)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.gather(
        _call(middleware, _scope("/api/v1/projects"), _Send()),
        _call(middleware, _scope("/api/v1/projects"), _Send()),
    )
    elapsed = loop.time() - started

    assert elapsed < delay * 1.8, elapsed


@pytest.mark.asyncio
async def test_public_paths_never_reach_the_authentication_check():
    calls = []

    async def authenticated(request):
        calls.append("authenticated")
        return False

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ManagementAuthASGIMiddleware(app, authenticated=authenticated)
    send = _Send()

    await _call(middleware, _scope("/api/v1/config/public"), send)

    assert calls == []
    assert send.messages[0]["status"] == 200
