"""Task #273: the SSE handshake runs off the event loop.

``stream_events`` was an ``async def`` route, so its preamble - ``get_project``,
the credential check behind ``authorize_event_stream`` - executed database work
directly on the ASGI loop. A reconnect storm after a restart (up to the 64
connections per Project the limiter allows) therefore stalled every other Agent's
REST/MCP traffic and the streams already open.
"""

from __future__ import annotations

import inspect

from agentchatroom.api import create_app

STREAM_PATH = "/api/v1/projects/{project_id}/events/stream"


def _stream_route(settings):
    app = create_app(settings)
    return next(
        route for route in app.routes if getattr(route, "path", "") == STREAM_PATH
    )


def test_stream_route_is_synchronous_so_the_handshake_runs_off_loop(settings):
    route = _stream_route(settings)

    assert not inspect.iscoroutinefunction(route.endpoint), (
        "an async stream route runs get_project and credential validation on the "
        "event loop, which serialises every other request behind each new "
        "EventSource (#273)"
    )


def test_stream_generator_is_still_asynchronous(settings):
    """Only the handshake moved off the loop; the poll loop keeps its shape."""
    route = _stream_route(settings)
    source = inspect.getsource(route.endpoint)

    assert "async def event_stream" in source
    assert "asyncio.to_thread" in source, "the poll loop must stay off the loop (#184)"
