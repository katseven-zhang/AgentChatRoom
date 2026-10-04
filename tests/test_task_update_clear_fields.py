"""Task #284: an explicit empty string must clear a clearable task field.

The MCP adapter collapsed `""` to None for current_step / blocker_reason /
next_step / description, so an Agent could never clear one -- while REST and CLI
already pass the value through, and the domain treats None as "keep" and "" as
"clear".
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentchatroom import cli, mcp_server
from agentchatroom.api import create_app

CLEARABLE = ("current_step", "blocker_reason", "next_step", "description")


def _owned_task(service, project, owner):
    task = service.create_task(
        project["id"],
        title="#284 clearable fields",
        acceptance_criteria=["fields can be cleared"],
        actor_session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], session_id=owner["agent"]["id"], token=owner["token"]
    )
    return service.update_task(
        project["id"],
        task["id"],
        current_step="writing the step",
        next_step="the next step",
        description="the description",
        session_id=owner["agent"]["id"],
        token=owner["token"],
    )["task"]


def test_mcp_task_update_clears_with_an_explicit_empty_string(
    service, project, joined_agents, monkeypatch
):
    owner, _reviewer = joined_agents
    task = _owned_task(service, project, owner)
    assert task["current_step"] == "writing the step"
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    updated = mcp_server.task_update(
        project["id"],
        task["id"],
        session_id=owner["agent"]["id"],
        token=owner["token"],
        current_step="",
        next_step="",
        description="",
    )

    assert updated["ok"] is True, updated
    stored = service.get_task(project["id"], task["id"])
    assert stored["current_step"] == ""
    assert stored["next_step"] == ""
    assert stored["description"] == ""


def test_mcp_task_update_keeps_fields_that_are_not_supplied(
    service, project, joined_agents, monkeypatch
):
    owner, _reviewer = joined_agents
    task = _owned_task(service, project, owner)
    monkeypatch.setattr(mcp_server, "get_service", lambda: service)

    updated = mcp_server.task_update(
        project["id"],
        task["id"],
        session_id=owner["agent"]["id"],
        token=owner["token"],
        progress_percent=40,
    )

    assert updated["ok"] is True, updated
    stored = service.get_task(project["id"], task["id"])
    assert stored["progress_percent"] == 40
    for field in CLEARABLE:
        assert stored[field] == task[field], field


def test_mcp_schema_marks_the_clearable_fields_optional_and_nullable(
    service, project
):
    """Omitting and clearing have to stay distinguishable in the advertised schema."""
    tool = next(
        item for item in mcp_server.mcp._tool_manager.list_tools()
        if item.name == "task_update"
    )
    properties = tool.parameters["properties"]
    for field in CLEARABLE:
        assert field not in tool.parameters.get("required", [])
        assert "null" in str(properties[field]), field


def test_rest_task_update_clears_with_an_explicit_empty_string(
    settings, service, project, joined_agents
):
    owner, _reviewer = joined_agents
    task = _owned_task(service, project, owner)

    with TestClient(create_app(settings)) as client:
        response = client.patch(
            f"/api/v1/projects/{project['id']}/tasks/{task['id']}",
            json={
                "current_step": "",
                "session_id": owner["agent"]["id"],
                "token": owner["token"],
            },
        )

    assert response.status_code == 200, response.text
    stored = service.get_task(project["id"], task["id"])
    assert stored["current_step"] == ""


def test_cli_task_update_forwards_cleared_and_unsupplied_fields_distinctly(monkeypatch):
    """The CLI already distinguishes them; the adapters must agree with it."""
    captured = {}

    def fake_request_json(base, method, path, body=None, *, request_id=None):
        captured["body"] = body
        return {"ok": True}

    monkeypatch.setattr(cli, "request_json", fake_request_json)

    cli.main(["task-update", "project_example", "task_example", "--current-step", "", "--next-step", ""])
    assert captured["body"]["current_step"] == ""
    assert captured["body"]["next_step"] == ""
    assert captured["body"]["blocker_reason"] is None
    assert captured["body"]["description"] is None
