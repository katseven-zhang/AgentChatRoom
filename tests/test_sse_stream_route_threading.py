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
    # Match the streaming route specifically: the plain /events read is a JSON
    # route on a path that also contains "events". `__name__` is the endpoint's
    # own function name, which the decorators preserve.
    return next(
        route
        for route in app.routes
        if getattr(route, "path", "") == STREAM_PATH
        and getattr(route.endpoint, "__name__", "") == "stream_events"
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
    assert "service.list_events" not in source, (
        "the generator must not read the database itself: #359 moved that read "
        "onto the ProjectEventHub worker thread, which is the stronger form of "
        "the #184 rule (no sync SQLite on the loop at all, rather than hopping "
        "to a thread per poll). Reading here again would undo that."
    )
    assert "event_hub.subscribe" in source
    assert "event_hub.unsubscribe" in source
