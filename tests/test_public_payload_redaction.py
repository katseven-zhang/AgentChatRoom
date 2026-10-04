"""#348 step 2: the shared policy must apply where payloads are actually read.

Step 1 created the single policy but a grep for it inside services.py's
_event_dict body found nothing, so every surface a consumer reads - GET /events,
the SSE stream, audit_query - still handed out the raw payload_json. A policy
nobody applies at the exit is not a policy. These tests read a secret-bearing
event back through each of those paths.

Reviewer policy, kept in view: this is a local desktop tool, so server.log and
GUI diagnostics must stay readable and log redaction is deliberately not
widened. Privacy enforcement belongs on the read surface.
"""

from __future__ import annotations

import json

import pytest

from agentchatroom.errors import REQUIRED_ACTIONS, DomainError


def _join(service, project_id, name="reader"):
    return service.join_room(
        project_id,
        software_key=name,
        name=name,
        client="pytest",
        model="unknown",
    )


def _post_secret(service, project_id, agent, secret):
    """Post a message carrying values in every shape the card lists."""
    return service.post_message(
        project_id,
        body=(
            f"sk-proj-{secret} sk-svcacct-{secret} "
            f"ghu_{secret} ghs_{secret} ghr_{secret} "
            f"acr.project01.{secret} acrb.v1.{secret} "
            f"token={secret} plain"
        ),
        # A changed-file list is the evidence a Work Report rests on, so it is
        # not redacted wholesale - doing so would break the feature. Only a
        # credential shape inside an entry would be, which redact_text does.
        files=["src/agentchatroom/services.py", "README.md"],
        session_id=agent["agent"]["id"],
        token=agent["token"],
        model_display_name="pytest",
    )


# --- AC1: the public read surfaces redact ------------------------------------


def test_reading_an_event_back_does_not_hand_out_the_secret(service, project):
    project_id = project["id"]
    agent = _join(service, project_id)
    # The real shapes the card lists. A bare alphanumeric string is not one:
    # acr.<id>.<secret> has two dot-separated segments, and a run of letters
    # is not a credential by anyone's definition.
    # A synthetic fixture, not a credential. "secret" appears in it so the
    # release auditor reads it as the placeholder it is - that rule has no
    # per-line exemption the way SECRET_PATTERNS does.
    secret = "secretabcdefgh12345678ijkl"
    _post_secret(service, project_id, agent, secret)

    with service.database.connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM events WHERE project_id = ?
              AND event_type LIKE 'message.%'
            ORDER BY id
            """,
            (project_id,),
        ).fetchall()
        assert rows, "the message produced no event"

        for row in rows:
            payload = service._event_dict(row)
            body = json.dumps(payload, ensure_ascii=False)
            assert secret not in body, f"secret survived in {payload}"
            assert "[REDACTED]" in body


def test_every_adapter_reads_the_same_redacted_projection(service, project):
    """GET /events, audit_query and the task history must agree.

    If one of them still joined the raw row it would be the leak the unified
    exit was supposed to close.
    """
    project_id = project["id"]
    agent = _join(service, project_id)
    secret = "secretzyxwvuts98765432rqpo"
    _post_secret(service, project_id, agent, secret)

    events = service.list_events(project_id)
    audit = service.query_audit(project_id)
    history = service.list_task_history(
        project_id, service.create_task(
            project_id,
            title="T",
            acceptance_criteria=["c"],
            actor_session_id=agent["agent"]["id"],
            token=agent["token"],
        )["task"]["id"],
    )

    for name, blob in (
        ("list_events", events),
        ("query_audit", audit),
        ("task_history", history),
    ):
        text = json.dumps(blob, ensure_ascii=False)
        assert secret not in text, f"{name} leaked the secret"


def test_a_redacted_event_still_carries_the_public_number(service, project):
    """A consumer reading event.id must not get undefined.

    The key used to be absent whenever the row carried no project_seq, which is
    indistinguishable from a projection bug at the call site.
    """
    project_id = project["id"]
    agent = _join(service, project_id)
    service.post_message(
        project_id,
        body="hello",
        session_id=agent["agent"]["id"],
        token=agent["token"],
        model_display_name="pytest",
    )
    with service.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()

        shaped = dict(row)
        shaped["project_seq"] = None
        projected = service._event_dict(shaped)
        assert "id" in projected, "the id key disappeared again"
        assert projected["id"] is None


# --- AC3: the failure codes answer with a next step ------------------------


@pytest.mark.parametrize(
    "code",
    [
        "not_task_owner",
        "invalid_transition",
        "insufficient_work_evidence",
        "acceptance_criteria_not_satisfied",
        "task_not_ready_for_integration",
    ],
)
def test_the_failure_codes_the_card_names_carry_a_required_action(code):
    assert code in REQUIRED_ACTIONS, f"{code} has no remedy"
    error = DomainError(code, "message")
    assert error.details["required_action"] == REQUIRED_ACTIONS[code]
    # And it reaches the caller, not just the exception object.
    assert error.as_dict()["error"]["details"]["required_action"]


def test_an_explicit_required_action_is_not_overruled():
    """A call site that knows more must win over the table."""
    error = DomainError(
        "invalid_transition",
        "message",
        details={"required_action": "do_the_specific_thing"},
    )
    assert error.details["required_action"] == "do_the_specific_thing"


def test_an_unmapped_code_gets_no_field_rather_than_a_wrong_one():
    """A missing remedy must be absent, not invented."""
    error = DomainError("some_code_nobody_mapped", "message")
    assert "required_action" not in error.details


def test_a_caller_that_hits_not_task_owner_gets_one_end_to_end(service, project):
    """The code the card singled out, through a real call."""
    project_id = project["id"]
    owner = _join(service, project_id, "owner")
    other = _join(service, project_id, "other")
    task = service.create_task(
        project_id,
        title="T",
        acceptance_criteria=["c"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    service.claim_task(
        project_id,
        task["id"],
        session_id=owner["agent"]["id"],
        token=owner["token"],
    )

    with pytest.raises(DomainError) as refused:
        service.release_task(
            project_id,
            task["id"],
            reason_code="user_requested",
            session_id=other["agent"]["id"],
            token=other["token"],
        )
    assert refused.value.code == "not_task_owner"
    assert refused.value.details["required_action"] == (
        "release_or_reclaim_the_task_before_changing_it"
    )
