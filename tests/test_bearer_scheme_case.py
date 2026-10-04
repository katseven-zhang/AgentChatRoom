"""Task #279: the bearer scheme is case-insensitive on every entry point.

Management authentication compared the header against the literal ``"Bearer "``
while the event-stream path matched it case-insensitively, so the same valid
credential (RFC 7235 makes the scheme case-insensitive) was accepted on one route
and a 401 on the other.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from agentchatroom.api import bearer_token, create_app
from agentchatroom.config import Settings

GUARD_TOKEN = "guard-token-value-0123456789abcdef"
WRONG_TOKEN = GUARD_TOKEN[:-1] + "x"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("BeArEr abc", "abc"),
        ("Bearer   abc  ", "abc"),
        ("bearer", ""),
        ("Basic abc", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_bearer_scheme_is_parsed_case_insensitively(header, expected):
    assert bearer_token(header) == expected


def _enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_ADMIN_TOKEN", GUARD_TOKEN)
    return Settings(data_dir=tmp_path / "enforced", management_auth_required=True)


def test_lowercase_bearer_authenticates_management_routes(tmp_path, monkeypatch, project_dir):
    settings = _enforced(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as client:
        unauthenticated = client.get("/api/v1/projects")
        lowercase = client.get(
            "/api/v1/projects", headers={"Authorization": "bearer " + GUARD_TOKEN}
        )
        uppercase = client.get(
            "/api/v1/projects", headers={"Authorization": "Bearer " + GUARD_TOKEN}
        )
        wrong = client.get(
            "/api/v1/projects", headers={"Authorization": "bearer " + WRONG_TOKEN}
        )

    assert unauthenticated.status_code == 401
    assert lowercase.status_code == 200, lowercase.text
    assert uppercase.status_code == 200
    assert wrong.status_code == 401


def test_both_entry_points_agree_on_the_same_header(tmp_path, monkeypatch, project_dir):
    settings = _enforced(tmp_path, monkeypatch)
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Case Room"},
            headers={"Authorization": "bearer " + GUARD_TOKEN},
        ).json()
        # The event stream accepts the credential, so it must not be the only
        # route that does: drive both with the same lowercase spelling.
        listed = client.get(
            "/api/v1/projects", headers={"Authorization": "bearer " + GUARD_TOKEN}
        )
        stream_anonymous = client.get(
            f"/api/v1/projects/{created['id']}/events/stream",
            headers={"Accept": "application/json"},
        )

    assert listed.status_code == 200
    listed_body = listed.json()
    items = listed_body["projects"] if isinstance(listed_body, dict) else listed_body
    assert [item["id"] for item in items] == [created["id"]]
    # Anonymous stays refused on the stream route; whether the middleware or the
    # stream authoriser refuses first is not this card's concern.
    assert stream_anonymous.status_code == 401
    assert stream_anonymous.json()["error"]["code"] in {
        "unauthorized_sse",
        "management_auth_required",
    }
