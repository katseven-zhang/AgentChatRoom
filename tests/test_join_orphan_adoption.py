"""Task #343: joining must not adopt another identity's ownerless Session.

An ownerless Session carries its tasks and leases, so adopting it assigns a
durable identity to somebody else's in-flight work. The adoption used to match on
the client string alone - enough for any identity configured with the same client
name to inherit an unrelated orphan and then "legally" reclaim it as the same
software identity.
"""

from __future__ import annotations

import json

import pytest

from agentchatroom.errors import DomainError
from tests.conftest import join_room_with_workspace


def _join(service, project, *, key, client, name=None):
    return join_room_with_workspace(
        service,
        project,
        agent_key=key,
        software_key=key,
        name=name or key,
        client=client,
        model="test-model",
        role="executor",
    )


def _orphan_session(service, project, *, agent_key, client, legacy_agent_key=None):
    """A Session that has lost its member reference, as a legacy row would.

    Modern Sessions store ``agent_key`` as the generated member id; pre-identity
    rows stored the software key, which is the shape adoption has to match.
    """
    session = _join(service, project, key=agent_key, client=client)
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET member_id = NULL WHERE id = ?",
            (session["agent"]["id"],),
        )
        if legacy_agent_key is not None:
            connection.execute(
                "UPDATE agent_sessions SET agent_key = ? WHERE id = ?",
                (legacy_agent_key, session["agent"]["id"]),
            )
    return session


def _joined_event_payload(service, project_id, session_id):
    with service.database.connect() as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM events
            WHERE project_id = ? AND event_type = 'agent.joined'
              AND actor_session_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (project_id, session_id),
        ).fetchone()
    return json.loads(row["payload_json"])


def test_join_does_not_adopt_an_orphan_of_a_different_identity(
    service, project
):
    orphan = _orphan_session(
        service, project, agent_key="orphan-a", client="zcode",
        legacy_agent_key="zcode-laptop",
    )
    joiner = _join(service, project, key="zcode-office", client="zcode")

    with service.database.connect() as connection:
        stored = connection.execute(
            "SELECT member_id FROM agent_sessions WHERE id = ?",
            (orphan["agent"]["id"],),
        ).fetchone()
    assert stored["member_id"] is None
    assert _joined_event_payload(service, project["id"], joiner["agent"]["id"])[
        "adopted_sessions"
    ] == []


def test_join_of_the_matching_identity_adopts_and_reports_it(service, project):
    orphan = _orphan_session(
        service, project, agent_key="orphan-b", client="zcode",
        legacy_agent_key="zcode-office",
    )
    joiner = _join(service, project, key="zcode-office", client="zcode")

    with service.database.connect() as connection:
        stored = connection.execute(
            "SELECT member_id FROM agent_sessions WHERE id = ?",
            (orphan["agent"]["id"],),
        ).fetchone()
    assert stored["member_id"] == joiner["agent"]["member_id"]

    payload = _joined_event_payload(service, project["id"], joiner["agent"]["id"])
    adopted = payload["adopted_sessions"]
    assert [item["session_id"] for item in adopted] == [orphan["agent"]["id"]]
    assert adopted[0]["previous_member_id"] is None
    assert adopted[0]["member_id"] == joiner["agent"]["member_id"]


def test_an_adopted_orphan_cannot_be_reached_by_the_other_identity(
    service, project
):
    orphan = _orphan_session(
        service, project, agent_key="orphan-c", client="zcode",
        legacy_agent_key="zcode-laptop",
    )
    task = service.create_task(
        project["id"],
        title="Owned by the orphan session",
        acceptance_criteria=["Cross-identity reclaim stays refused"],
        actor_session_id=orphan["agent"]["id"],
        token=orphan["token"],
    )["task"]
    service.claim_task(
        project["id"],
        task["id"],
        session_id=orphan["agent"]["id"],
        token=orphan["token"],
    )
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE agent_sessions SET token_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", orphan["agent"]["id"]),
        )
    joiner = _join(service, project, key="zcode-office", client="zcode")

    with pytest.raises(DomainError) as error:
        service.claim_task(
            project["id"],
            task["id"],
            session_id=joiner["agent"]["id"],
            token=joiner["token"],
            reclaim=True,
        )

    assert error.value.code == "task_reclaim_forbidden"
