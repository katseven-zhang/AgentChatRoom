"""Knowledge content is bounded like every other user-facing write (#355).

title/body/summary were the one write path with no size limit, while task text
and project documents both had one. These reuse the existing configured limits
rather than adding new settings.
"""

from __future__ import annotations

import pytest

from agentchatroom.errors import DomainError


def test_oversized_knowledge_body_is_refused(service, project, joined_agents):
    executor, _reviewer = joined_agents
    limit = int(getattr(service.settings, "project_document_max_bytes", 262144))
    payload = "x" * (limit + 1)

    with pytest.raises(DomainError) as too_large:
        service.submit_knowledge_candidate(
            project["id"],
            session_id=executor["agent"]["id"],
            token=executor["token"],
            title="Bounded body",
            body=payload,
            kind="decision",
        )

    assert too_large.value.code == "knowledge_content_too_large"
    assert too_large.value.status_code == 422
    assert too_large.value.details["field"] == "body"
    assert too_large.value.details["size"] > limit


def test_oversized_knowledge_title_and_summary_are_refused(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    limit = int(getattr(service.settings, "task_text_max_length", 32768))

    for field, payload in (
        ("title", "t" * (limit + 1)),
        ("summary", "s" * (limit + 1)),
    ):
        with pytest.raises(DomainError) as too_long:
            service.submit_knowledge_candidate(
                project["id"],
                session_id=executor["agent"]["id"],
                token=executor["token"],
                title=payload if field == "title" else "ok",
                body="body",
                summary=payload if field == "summary" else "ok",
                kind="decision",
            )
        assert too_long.value.code == "knowledge_content_too_long"
        assert too_long.value.details["field"] == field


def test_ordinary_knowledge_content_is_unaffected(service, project, joined_agents):
    """A normal candidate must still be accepted after the bound is added."""
    executor, _reviewer = joined_agents
    created = service.submit_knowledge_candidate(
        project["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        title="Bounded knowledge",
        body="A perfectly ordinary body.",
        summary="ordinary",
        kind="decision",
    )
    assert created["status"] == "candidate"
