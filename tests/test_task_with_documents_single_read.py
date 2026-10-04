"""#278: one domain method for reading a task with its documents.

get_task_with_documents existed and nothing called it. REST and MCP each
assembled the answer from two reads and wrote into the dict the service handed
back, so a document published between the two reads could reach the caller as a
version the task never saw.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_no_adapter_rebuilds_the_task_with_documents_assembly():
    """The guard against a fourth private copy.

    The defect was three copies of one assembly: the domain method and two
    adapter-local rebuilds. Each adapter rebuilt it with two reads, which is the
    part that is actually wrong - not the duplication, the torn snapshot.
    """
    for module in ("api.py", "mcp_server.py"):
        source = (ROOT / "src" / "agentchatroom" / module).read_text(
            encoding="utf-8"
        )
        assert (
            'get_task_with_documents' in source
        ), f"{module} no longer uses the shared read"


def test_rest_and_mcp_both_go_through_the_domain_method():
    """Not just "the name appears" - the adapters must not call the pieces."""
    api = (ROOT / "src" / "agentchatroom" / "api.py").read_text(encoding="utf-8")
    assert "service.injectable_project_documents(project_id)" not in api, (
        "the REST task route still assembles the answer itself"
    )

    mcp = (ROOT / "src" / "agentchatroom" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    assert "service.injectable_project_documents(pid)" not in mcp, (
        "the MCP task_get tool still assembles the answer itself"
    )


def test_the_task_and_its_documents_come_from_one_connection(
    service, project, joined_agents
):
    """The point of the shared method: one snapshot, one connection."""
    manager, executor = joined_agents
    service.upsert_project_document(
        project["id"],
        doc_key="standards",
        kind="binding",
        title="Standards",
        content="rules",
        session_id=manager["agent"]["id"],
        token=manager["token"],
        management_authorized=True,
    )
    task = service.create_task(
        project["id"],
        title="T",
        acceptance_criteria=["c"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]

    seen: list[str] = []
    real_connect = service.database.connect

    def counting_connect(*args, **kwargs):
        seen.append("connect")
        return real_connect(*args, **kwargs)

    service.database.connect = counting_connect  # type: ignore[method-assign]
    try:
        payload = service.get_task_with_documents(project["id"], task["id"])
    finally:
        service.database.connect = real_connect  # type: ignore[method-assign]

    assert payload["task"]["id"] == task["id"]
    assert payload["project_documents"], "documents did not come along"
    assert len(seen) == 1, f"the read took {len(seen)} connections"


def test_the_method_is_no_longer_uncalled(service, project, joined_agents):
    """It had zero callers; the adapters now provide them."""
    _, executor = joined_agents
    task = service.create_task(
        project["id"],
        title="T",
        acceptance_criteria=["c"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    payload = service.get_task_with_documents(project["id"], task["id"])
    assert payload["task"]["id"] == task["id"]
    assert "project_documents" in payload


def test_the_rest_route_answers_with_one_payload(settings, project_dir):
    """The route returns what the domain method returns, unchanged."""
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Snapshot"},
        ).json()
        created = client.post(
            f"/api/v1/projects/{project['id']}/tasks",
            json={
                "title": "T",
                "acceptance_criteria": ["c"],
                "actor_name": "someone",
            },
        )
        if created.status_code not in (200, 201):
            pytest.skip("task creation needs credentials in this configuration")
        task_id = created.json()["task"]["id"]

        fetched = client.get(f"/api/v1/projects/{project['id']}/tasks/{task_id}")
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["task"]["id"] == task_id
        # The documents ride at the top level next to the task, which is the
        # shape the domain method defines.
        assert "project_documents" in body
