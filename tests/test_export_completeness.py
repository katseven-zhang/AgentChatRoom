"""Task #251: an export must not be a snapshot page wearing a full-history label.

`export_project` paged events to the cursor but took reports, reviews, and
acknowledgements from `snapshot`, whose lists are bounded by
`coordination.snapshot_recent_limit`, so every report older than that window
silently disappeared from the downloaded archive.
"""

from __future__ import annotations

from dataclasses import replace

from agentchatroom.config import Settings
from agentchatroom.database import Database
from agentchatroom.services import AgentChatRoomService

WINDOW = 3
ROWS = 6


def _service(tmp_path):
    settings = replace(
        Settings(data_dir=tmp_path / "data"),
        snapshot_recent_limit=WINDOW,
    )
    instance = AgentChatRoomService(Database(settings.database_path), settings)
    instance.initialize()
    return instance, settings


def _seed(tmp_path):
    service, _settings = _service(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    project = service.create_project(root_path=str(root), name="Export Room")
    host = service.register_workspace(
        project["id"], host_key="h", host_name="H", local_path=str(root)
    )
    joined = service.join_room(
        project["id"],
        name="Builder",
        client="codex",
        model="test-model",
        worktree=str(root),
        host_id=host["host"]["id"],
        workspace_id=host["workspace"]["id"],
    )
    task = service.create_task(
        project["id"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
        title="Ship the exporter",
        acceptance_criteria=["all rows export"],
    )["task"]
    session_id = joined["agent"]["id"]
    with service.database.connect(write=True) as connection:
        for index in range(ROWS):
            stamp = f"2026-01-0{index + 1}T00:00:00+00:00"
            connection.execute(
                "INSERT INTO work_reports (id, project_id, task_id, session_id, summary,"
                " files_json, tests_json, system_evidence_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"report_{index}",
                    project["id"],
                    task["id"],
                    session_id,
                    f"report {index}",
                    '["src/a.py"]',
                    '[{"command": "pytest", "exit_code": 0}]',
                    '{"checks": 1}',
                    stamp,
                ),
            )
            connection.execute(
                "INSERT INTO reviews (id, project_id, task_id, reviewer_session_id,"
                " verdict, criteria_json, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"review_{index}",
                    project["id"],
                    task["id"],
                    session_id,
                    "approved",
                    '[{"criterion": "all rows export", "outcome": "met"}]',
                    "",
                    stamp,
                ),
            )
    acknowledgements = 0
    for index in range(ROWS):
        message = service.post_message(
            project["id"],
            session_id=session_id,
            token=joined["token"],
            body=f"status {index}",
            model_display_name="test-model",
            requires_ack=True,
        )
        service.acknowledge_event(
            project["id"],
            event_id=message["event_id"],
            session_id=session_id,
            token=joined["token"],
        )
        acknowledgements += 1
    return service, project, acknowledgements


def test_export_contains_every_report_and_review(tmp_path):
    service, project, _ = _seed(tmp_path)

    snapshot = service.snapshot(project["id"])
    exported = service.export_project(project["id"])

    assert len(snapshot["reports"]) == WINDOW
    assert len(exported["reports"]) == ROWS
    assert [row["id"] for row in exported["reports"]] == [
        f"report_{index}" for index in range(ROWS)
    ]
    assert len(exported["reviews"]) == ROWS
    assert exported["export_schema_version"] == 2


def test_export_contains_every_acknowledgement(tmp_path):
    service, project, acknowledgements = _seed(tmp_path)

    exported = service.export_project(project["id"])

    assert len(exported["acknowledgements"]) == acknowledgements
    assert len(service.snapshot(project["id"])["acknowledgements"]) == WINDOW


def test_export_counts_agree_with_the_payload(tmp_path):
    service, project, acknowledgements = _seed(tmp_path)

    exported = service.export_project(project["id"])
    counts = exported["counts"]

    for key in ("reports", "reviews", "acknowledgements", "events", "knowledge_assets"):
        assert counts[key] == len(exported[key]), key
    assert counts["reports"] == ROWS
    assert counts["acknowledgements"] == acknowledgements


def test_exported_rows_keep_the_snapshot_shape(tmp_path):
    service, project, _ = _seed(tmp_path)

    exported = service.export_project(project["id"])
    report = exported["reports"][0]
    review = exported["reviews"][0]

    assert report["files"] == ["src/a.py"]
    assert report["tests"] == [{"command": "pytest", "exit_code": 0}]
    assert report["system_evidence"] == {"checks": 1}
    assert review["criteria"] == [{"criterion": "all rows export", "outcome": "met"}]
    for key in ("files_json", "tests_json", "system_evidence_json"):
        assert key not in report
    assert "criteria_json" not in review
    assert all("physical_id" not in item for item in exported["acknowledgements"])


def test_snapshot_projection_is_unchanged_by_the_shared_mappers(tmp_path):
    service, project, _ = _seed(tmp_path)

    snapshot = service.snapshot(project["id"])

    assert len(snapshot["reports"]) == WINDOW
    assert snapshot["reports"][0]["files"] == ["src/a.py"]
    assert "files_json" not in snapshot["reports"][0]
