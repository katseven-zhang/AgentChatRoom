"""Task #281: a plain GET must not mint management sessions.

``GET /api/v1/auth/status`` issued and attached a browser session whenever the
local deployment was trusted and no valid cookie was present. The path sits under
the middleware's ``/api/v1/auth/`` exemption, so any anonymous or cross-origin
simple request (no preflight, no credentials) could insert rows into
``management_sessions`` with a 12-hour TTL, with no rate limit anywhere on the
route - unbounded writes from a read.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentchatroom.api import _same_origin_browser_request, create_app


def _session_rows(settings) -> int:
    from agentchatroom.database import Database

    with Database(settings.database_path).connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS total FROM management_sessions"
            ).fetchone()["total"]
        )


def test_repeated_auth_status_calls_write_nothing(settings, service):
    with TestClient(create_app(settings)) as client:
        first = client.get("/api/v1/auth/status")
        assert first.status_code == 200
        assert "set-cookie" not in {k.lower() for k in first.headers}
        for _ in range(25):
            response = client.get("/api/v1/auth/status")
            assert response.status_code == 200
            assert response.json() == {"required": False, "authenticated": True}

    assert _session_rows(settings) == 0

class _Headers:
    def __init__(self, value):
        self._value = value

    def get(self, name, default=""):
        if name != "sec-fetch-site" or self._value is None:
            return default
        return self._value


class _FakeRequest:
    def __init__(self, value):
        self.headers = _Headers(value)


@pytest.mark.parametrize(
    ("sec_fetch_site", "allowed"),
    [
        ("same-origin", True),
        ("SAME-ORIGIN", True),
        ("none", True),
        ("cross-site", False),
        ("crossorigin", False),
        ("", False),
        (None, False),
    ],
)
def test_local_stream_bypass_only_accepts_the_browsers_own_origin(sec_fetch_site, allowed):
    request = _FakeRequest(sec_fetch_site)
    assert _same_origin_browser_request(request) is allowed


def test_anonymous_local_stream_still_needs_a_credential(settings, project):
    """Removing the issued cookie did not open the stream to strangers."""
    with TestClient(create_app(settings)) as client:
        for headers in ({}, {"Sec-Fetch-Site": "cross-site"}):
            response = client.get(
                f"/api/v1/projects/{project['id']}/events/stream",
                headers={"Accept": "application/json", **headers},
            )
            assert response.status_code == 401, headers
            assert response.json()["error"]["code"] == "unauthorized_sse", headers
