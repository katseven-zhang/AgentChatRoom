from __future__ import annotations

import sqlite3

import pytest

from agentchatroom.database import Database, SCHEMA, SCHEMA_VERSION
from agentchatroom.errors import DomainError


KNOWLEDGE_TABLES = {
    "knowledge_assets",
    "knowledge_asset_versions",
    "knowledge_reviews",
}


def _schema_without_knowledge_tables() -> str:
    return SCHEMA.split("CREATE TABLE IF NOT EXISTS knowledge_assets")[0]


def test_fresh_database_creates_knowledge_tables(settings):
    database = Database(settings.database_path)
    database.initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert KNOWLEDGE_TABLES <= tables


def test_v12_database_upgrades_to_knowledge_schema(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executescript(_schema_without_knowledge_tables())
        connection.execute("INSERT INTO schema_meta(version) VALUES (12)")

    Database(settings.database_path).initialize()

    with sqlite3.connect(settings.database_path) as connection:
        version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version_rows = connection.execute(
            "SELECT id, project_id, asset_id, version FROM knowledge_asset_versions"
        ).fetchall()

    assert version == SCHEMA_VERSION
    assert KNOWLEDGE_TABLES <= tables
    assert version_rows == []


def _submit_candidate(service, project, executor, **overrides):
    payload = {
        "session_id": executor["agent"]["id"],
        "token": executor["token"],
        "title": "Use SQLite WAL mode",
        "body": "Enable WAL mode before running concurrent acceptance tests.",
        "kind": "decision",
        "summary": "WAL avoids database locked errors",
        "tags": ["database", "testing"],
        "source_type": "manual",
    }
    payload.update(overrides)
    return service.submit_knowledge_candidate(project["id"], **payload)


def _review_asset(service, project, reviewer, asset_id, verdict="approved", **overrides):
    payload = {
        "reviewer_session_id": reviewer["agent"]["id"],
        "token": reviewer["token"],
        "verdict": verdict,
        "criteria": [
            {"criterion": "Content is accurate", "status": "passed"},
            {"criterion": "Provenance is traceable", "status": "passed"},
        ],
    }
    payload.update(overrides)
    return service.submit_knowledge_review(project["id"], asset_id, **payload)


def test_candidate_creates_first_version_with_provenance(service, project, joined_agents):
    executor, _reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    assert created["status"] == "candidate"
    assert created["revision"] is False
    assert created["version"] == 1
    asset = created["asset"]
    assert asset["kind"] == "decision"
    assert asset["status"] == "candidate"
    assert asset["owner_kind"] == "agent_key"
    assert asset["owner_id"] == "builder-main"
    assert asset["current_version"]["title"] == "Use SQLite WAL mode"
    assert asset["current_version"]["tags"] == ["database", "testing"]

    detail = service.get_knowledge_asset(project["id"], asset["id"])
    assert len(detail["versions"]) == 1
    version = detail["versions"][0]
    assert version["source_type"] == "manual"
    assert version["created_by_session_id"] == executor["agent"]["id"]
    assert version["supersedes_version_id"] is None
    assert version["content_hash"]
    assert detail["version"]["id"] == version["id"]


def test_body_edits_append_new_versions_and_preserve_history(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    asset_id = created["asset"]["id"]
    first_version_id = created["version_id"]

    revised = _submit_candidate(
        service,
        project,
        executor,
        asset_id=asset_id,
        body="Enable WAL mode and busy_timeout before concurrent tests.",
        title="Use SQLite WAL mode",
        summary="Updated guidance",
    )
    assert revised["revision"] is True
    assert revised["version"] == 2
    assert revised["status"] == "candidate"
    assert revised["asset"]["current_version"]["summary"] == "Updated guidance"

    detail = service.get_knowledge_asset(project["id"], asset_id)
    assert [version["version"] for version in detail["versions"]] == [1, 2]
    assert detail["versions"][1]["supersedes_version_id"] == first_version_id
    assert detail["versions"][0]["body"] == (
        "Enable WAL mode before running concurrent acceptance tests."
    )
    assert detail["versions"][0]["content_hash"] != detail["versions"][1]["content_hash"]

    historic = service.get_knowledge_asset(
        project["id"], asset_id, version_id=first_version_id
    )
    assert historic["version"]["version"] == 1
    assert historic["version"]["body"] == (
        "Enable WAL mode before running concurrent acceptance tests."
    )


def test_knowledge_review_requires_independent_reviewer(service, project, joined_agents):
    executor, _reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    with pytest.raises(DomainError) as failure:
        _review_asset(
            service,
            project,
            executor,
            created["asset"]["id"],
            criteria=[{"criterion": "ok", "status": "passed"}],
        )
    assert failure.value.code == "knowledge_reviewer_not_independent"

    same_identity = service.join_room(
        project["id"],
        agent_key="builder-main",
        name="Builder Again",
        client="codex",
        model="test-model",
    )
    with pytest.raises(DomainError) as identity_failure:
        _review_asset(
            service,
            project,
            same_identity,
            created["asset"]["id"],
            criteria=[{"criterion": "ok", "status": "passed"}],
        )
    assert identity_failure.value.code == "knowledge_reviewer_not_independent"


def test_review_verdict_drives_status_and_records_criteria(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    asset_id = created["asset"]["id"]

    with pytest.raises(DomainError) as empty_criteria:
        _review_asset(service, project, reviewer, asset_id, criteria=[])
    assert empty_criteria.value.code == "missing_review_criteria"

    with pytest.raises(DomainError) as bad_criteria:
        _review_asset(
            service,
            project,
            reviewer,
            asset_id,
            criteria=[{"criterion": "ok", "status": "maybe"}],
        )
    assert bad_criteria.value.code == "invalid_review_criteria"

    rejected = _review_asset(
        service,
        project,
        reviewer,
        asset_id,
        verdict="changes_requested",
        criteria=[{"criterion": "Content is accurate", "status": "failed"}],
    )
    assert rejected["status"] == "rejected"
    assert rejected["review"]["verdict"] == "changes_requested"

    revised = _submit_candidate(
        service, project, executor, asset_id=asset_id, title="Use SQLite WAL mode"
    )
    assert revised["status"] == "candidate"

    approved = _review_asset(service, project, reviewer, asset_id)
    assert approved["status"] == "approved"
    assert approved["asset"]["status"] == "approved"
    assert approved["review"]["verdict"] == "approved"
    assert approved["review"]["reviewer_session_id"] == reviewer["agent"]["id"]

    detail = service.get_knowledge_asset(project["id"], asset_id)
    assert len(detail["reviews"]) == 2
    assert detail["reviews"][0]["criteria"][0]["criterion"] == "Content is accurate"

    with pytest.raises(DomainError) as not_reviewable:
        _review_asset(service, project, reviewer, asset_id)
    assert not_reviewable.value.code == "knowledge_not_reviewable"


def test_rejected_candidate_can_return_to_review_cycle(service, project, joined_agents):
    executor, reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    asset_id = created["asset"]["id"]
    rejected = _review_asset(
        service,
        project,
        reviewer,
        asset_id,
        verdict="changes_requested",
        criteria=[{"criterion": "Content is accurate", "status": "failed"}],
    )
    assert rejected["status"] == "rejected"

    revised = _submit_candidate(
        service, project, executor, asset_id=asset_id, title="Use SQLite WAL mode"
    )
    assert revised["status"] == "candidate"
    assert revised["version"] == 2
    approved = _review_asset(service, project, reviewer, asset_id)
    assert approved["status"] == "approved"


def test_status_transitions_are_enforced(service, project, joined_agents):
    executor, reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    asset_id = created["asset"]["id"]

    with pytest.raises(DomainError) as premature:
        service.supersede_knowledge_asset(
            project["id"],
            asset_id,
            session_id=executor["agent"]["id"],
            token=executor["token"],
        )
    assert premature.value.code == "knowledge_invalid_transition"

    _review_asset(service, project, reviewer, asset_id)
    with pytest.raises(DomainError) as revise_approved:
        _submit_candidate(
            service,
            project,
            executor,
            asset_id=asset_id,
            title="Use SQLite WAL mode",
            body="Approved content must be superseded, not silently revised.",
        )
    assert revise_approved.value.code == "knowledge_invalid_transition"

    superseded = service.supersede_knowledge_asset(
        project["id"],
        asset_id,
        session_id=executor["agent"]["id"],
        token=executor["token"],
        reason="Replaced by a newer decision",
    )
    assert superseded["status"] == "superseded"

    archived = service.archive_knowledge_asset(
        project["id"],
        asset_id,
        session_id=executor["agent"]["id"],
        token=executor["token"],
        reason="Obsolete",
    )
    assert archived["status"] == "archived"

    with pytest.raises(DomainError) as edit_after_archive:
        _submit_candidate(
            service, project, executor, asset_id=asset_id, title="Zombie", body="No"
        )
    assert edit_after_archive.value.code == "knowledge_asset_archived"


def test_assets_are_isolated_per_project(
    service, project, tmp_path, joined_agents
):
    executor, _reviewer = joined_agents
    other_path = tmp_path / "other-project"
    other_path.mkdir()
    other_project = service.create_project(root_path=str(other_path))
    created = _submit_candidate(service, project, executor)

    listed = service.list_knowledge_assets(other_project["id"])
    assert listed == []

    other_executor = service.join_room(
        other_project["id"],
        agent_key="other-builder-main",
        name="Other Builder",
        client="codex",
        model="test-model",
        role="reviewer",
    )

    with pytest.raises(DomainError) as cross_read:
        service.get_knowledge_asset(other_project["id"], created["asset"]["id"])
    assert cross_read.value.code == "knowledge_asset_not_found"

    with pytest.raises(DomainError) as cross_write:
        service.submit_knowledge_review(
            other_project["id"],
            created["asset"]["id"],
            reviewer_session_id=other_executor["agent"]["id"],
            token=other_executor["token"],
            verdict="approved",
            criteria=[{"criterion": "ok", "status": "passed"}],
        )
    assert cross_write.value.code == "knowledge_asset_not_found"


def test_candidate_submit_is_idempotent_per_request(service, project, joined_agents):
    executor, _reviewer = joined_agents
    request_id = "knowledge-idem-1"
    first = _submit_candidate(
        service, project, executor, request_id=request_id
    )
    replay = _submit_candidate(
        service, project, executor, request_id=request_id
    )
    assert replay["asset"]["id"] == first["asset"]["id"]
    assert replay["version_id"] == first["version_id"]
    listed = service.list_knowledge_assets(project["id"])
    assert len(listed) == 1

    second = _submit_candidate(
        service, project, executor, request_id="knowledge-idem-2"
    )
    assert second["asset"]["id"] != first["asset"]["id"]


def test_export_includes_knowledge_assets(service, project, joined_agents):
    executor, reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    asset_id = created["asset"]["id"]
    revised = _submit_candidate(
        service, project, executor, asset_id=asset_id, title="Use SQLite WAL mode"
    )
    assert revised["version"] == 2
    _review_asset(service, project, reviewer, asset_id)

    exported = service.export_project(project["id"])
    exported_assets = exported["knowledge_assets"]
    assert len(exported_assets) == 1
    exported_asset = exported_assets[0]
    assert exported_asset["id"] == asset_id
    assert len(exported_asset["versions"]) == 2
    assert exported_asset["reviews"][0]["verdict"] == "approved"
    assert exported_asset["current_version"]["version"] == 2
    assert exported_asset["versions"][0]["body"] == (
        "Enable WAL mode before running concurrent acceptance tests."
    )


def test_invalid_kind_and_sources_are_rejected(service, project, joined_agents):
    executor, _reviewer = joined_agents
    with pytest.raises(DomainError) as bad_kind:
        _submit_candidate(service, project, executor, kind="daydream")
    assert bad_kind.value.code == "invalid_knowledge_kind"

    with pytest.raises(DomainError) as bad_source_type:
        _submit_candidate(service, project, executor, source_type="hallucination")
    assert bad_source_type.value.code == "invalid_knowledge_source_type"

    with pytest.raises(DomainError) as missing_task:
        _submit_candidate(
            service, project, executor, source_task_id="task_does_not_exist"
        )
    assert missing_task.value.code == "knowledge_source_not_found"


def test_task_linked_approval_requires_verified_task(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="Ship feature",
        acceptance_criteria=["Feature works"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Done",
        files=["src/feature.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    created = _submit_candidate(
        service,
        project,
        executor,
        source_type="task_result",
        source_task_id=task["id"],
    )
    asset_id = created["asset"]["id"]

    with pytest.raises(DomainError) as unverified:
        _review_asset(service, project, reviewer, asset_id)
    assert unverified.value.code == "knowledge_task_not_verified"

    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "Feature works", "status": "passed"}],
    )
    approved = _review_asset(service, project, reviewer, asset_id)
    assert approved["status"] == "approved"


def test_list_filters_and_invalid_status(service, project, joined_agents):
    executor, _reviewer = joined_agents
    first = _submit_candidate(service, project, executor)
    _submit_candidate(
        service, project, executor, title="Second asset", body="Body", kind="procedure"
    )
    assert len(service.list_knowledge_assets(project["id"])) == 2
    assert len(service.list_knowledge_assets(project["id"], kind="procedure")) == 1
    assert len(service.list_knowledge_assets(project["id"], status="candidate")) == 2
    assert service.list_knowledge_assets(project["id"], status="approved") == []

    with pytest.raises(DomainError) as bad_status:
        service.list_knowledge_assets(project["id"], status="floating")
    assert bad_status.value.code == "invalid_knowledge_status"

    events = service.list_events(project["id"], after=0)["events"]
    submitted = [event for event in events if event["event_type"] == "knowledge.candidate_submitted"]
    assert len(submitted) == 2
    assert submitted[0]["payload"]["asset_id"] == first["asset"]["id"]


def test_rest_adapter_reuses_domain_service(settings, project_dir):
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(project_dir), "name": "Rest Project"},
        ).json()
        project_id = project["id"]
        executor = client.post(
            f"/api/v1/projects/{project_id}/agents/join",
            json={
                "agent_key": "rest-builder-main",
                "name": "Rest Builder",
                "client": "codex",
                "model": "test-model",
                "role": "executor",
            },
        ).json()
        reviewer = client.post(
            f"/api/v1/projects/{project_id}/agents/join",
            json={
                "agent_key": "rest-reviewer-main",
                "name": "Rest Reviewer",
                "client": "qoder",
                "model": "test-model",
                "role": "reviewer",
            },
        ).json()

        public_config = client.get("/api/v1/config/public").json()
        knowledge = public_config["domain"]["knowledge"]
        assert knowledge["schema_version"] == 1
        assert "decision" in knowledge["kinds"]
        assert knowledge["asset_statuses"] == [
            "approved",
            "archived",
            "candidate",
            "rejected",
            "superseded",
        ]
        assert knowledge["review_verdicts"] == ["approved", "changes_requested"]

        submitted = client.post(
            f"/api/v1/projects/{project_id}/knowledge/assets",
            json={
                "session_id": executor["agent"]["id"],
                "token": executor["token"],
                "title": "Prefer advisory leases",
                "body": "Advisory lease policy keeps collaborating agents unblocked.",
                "kind": "preference",
                "tags": ["coordination"],
            },
        )
        assert submitted.status_code == 201
        asset_id = submitted.json()["asset"]["id"]
        assert submitted.json()["version"] == 1

        listed = client.get(f"/api/v1/projects/{project_id}/knowledge/assets")
        assert listed.status_code == 200
        assert len(listed.json()["assets"]) == 1

        reviewed = client.post(
            f"/api/v1/projects/{project_id}/knowledge/assets/{asset_id}/reviews",
            json={
                "reviewer_session_id": reviewer["agent"]["id"],
                "token": reviewer["token"],
                "verdict": "approved",
                "criteria": [
                    {"criterion": "Accurate", "status": "passed"},
                    {"criterion": "Traceable", "status": "passed"},
                ],
            },
        )
        assert reviewed.status_code == 201
        assert reviewed.json()["status"] == "approved"

        superseded = client.post(
            f"/api/v1/projects/{project_id}/knowledge/assets/{asset_id}/supersede",
            json={
                "session_id": executor["agent"]["id"],
                "token": executor["token"],
                "reason": "Policy changed",
            },
        )
        assert superseded.status_code == 200
        assert superseded.json()["status"] == "superseded"

        detail = client.get(f"/api/v1/projects/{project_id}/knowledge/assets/{asset_id}")
        assert detail.status_code == 200
        detail_json = detail.json()
        assert detail_json["status"] == "superseded"
        assert detail_json["versions"][0]["tags"] == ["coordination"]
        assert detail_json["reviews"][0]["verdict"] == "approved"

        archived = client.post(
            f"/api/v1/projects/{project_id}/knowledge/assets/{asset_id}/archive",
            json={
                "session_id": executor["agent"]["id"],
                "token": executor["token"],
                "reason": "Retired",
            },
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"

        missing = client.get(
            f"/api/v1/projects/{project_id}/knowledge/assets/kasset_missing"
        )
        assert missing.status_code == 404


def test_mcp_tools_reuse_domain_service(monkeypatch, service, project_dir):
    from agentchatroom import mcp_server

    monkeypatch.setattr(mcp_server, "service", service)
    service.create_project(root_path=str(project_dir))
    executor = mcp_server.room_join(
        str(project_dir), "mcp-builder-main", "Mcp Builder", "codex", "test-model"
    )["result"]
    reviewer = mcp_server.room_join(
        str(project_dir),
        "mcp-reviewer-main",
        "Mcp Reviewer",
        "qoder",
        "test-model",
        role="reviewer",
    )["result"]
    project_id = executor["project"]["id"]

    created = mcp_server.knowledge_candidate_submit(
        project_id,
        executor["agent"]["id"],
        executor["token"],
        title="Pin the review gate",
        body="Knowledge needs an independent review before it is approved.",
        kind="procedure",
        tags=["review"],
    )
    assert created["ok"] is True
    asset_id = created["result"]["asset"]["id"]

    listed = mcp_server.knowledge_list(project_id, status="candidate")
    assert listed["ok"] is True
    assert [asset["id"] for asset in listed["result"]] == [asset_id]

    self_review = mcp_server.knowledge_review(
        project_id,
        asset_id,
        executor["agent"]["id"],
        executor["token"],
        verdict="approved",
        criteria=[{"criterion": "ok", "status": "passed"}],
    )
    assert self_review["ok"] is False
    assert self_review["error"]["code"] == "knowledge_reviewer_not_independent"

    reviewed = mcp_server.knowledge_review(
        project_id,
        asset_id,
        reviewer["agent"]["id"],
        reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "ok", "status": "passed"}],
    )
    assert reviewed["ok"] is True
    assert reviewed["result"]["status"] == "approved"

    superseded = mcp_server.knowledge_supersede(
        project_id, asset_id, executor["agent"]["id"], executor["token"]
    )
    assert superseded["ok"] is True
    assert superseded["result"]["status"] == "superseded"

    detail = mcp_server.knowledge_get(project_id, asset_id)
    assert detail["ok"] is True
    assert detail["result"]["reviews"][0]["verdict"] == "approved"


def test_knowledge_settings_defaults_and_overrides(monkeypatch, tmp_path):
    from agentchatroom.config import load_settings
    from agentchatroom.contracts import KNOWLEDGE_DEFAULT_KINDS

    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AGENTCHATROOM_KNOWLEDGE_KINDS", raising=False)
    monkeypatch.delenv("AGENTCHATROOM_KNOWLEDGE_REQUIRE_VERIFIED_TASK", raising=False)

    defaults = load_settings()
    assert defaults.knowledge_kinds == KNOWLEDGE_DEFAULT_KINDS
    assert defaults.knowledge_require_verified_task is True

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[application]
schema_version = 1

[knowledge]
kinds = ["decision", "runbook"]
require_verified_task = false
""".strip(),
        encoding="utf-8",
    )
    configured = load_settings(config_path)
    assert configured.knowledge_kinds == ("decision", "runbook")
    assert configured.knowledge_require_verified_task is False

    monkeypatch.setenv("AGENTCHATROOM_KNOWLEDGE_KINDS", "decision, playbook")
    monkeypatch.setenv("AGENTCHATROOM_KNOWLEDGE_REQUIRE_VERIFIED_TASK", "true")
    overridden = load_settings(config_path)
    assert overridden.knowledge_kinds == ("decision", "playbook")
    assert overridden.knowledge_require_verified_task is True


def test_knowledge_settings_reject_invalid_kinds(monkeypatch, tmp_path):
    from agentchatroom.config import load_settings

    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    for invalid in ("Uppercase", "with space", "-leading-dash", ""):
        monkeypatch.setenv("AGENTCHATROOM_KNOWLEDGE_KINDS", invalid)
        with pytest.raises(ValueError):
            load_settings()
    monkeypatch.setenv("AGENTCHATROOM_KNOWLEDGE_KINDS", "decision, ,procedure")
    with pytest.raises(ValueError):
        load_settings()


def _task_with_pending_report(service, project, executor):
    task = service.create_task(
        project["id"],
        title="Harden knowledge provenance",
        acceptance_criteria=["Gate enforced"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], executor["agent"]["id"], executor["token"]
    )
    service.update_task(
        project["id"],
        task["id"],
        status="in_progress",
        session_id=executor["agent"]["id"],
        token=executor["token"],
    )
    report = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="Implemented",
        files=["src/agentchatroom/services.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    return task, report


def test_report_only_provenance_derives_task_and_requires_verification(
    service, project, joined_agents
):
    executor, reviewer = joined_agents
    task, report = _task_with_pending_report(service, project, executor)

    created = _submit_candidate(
        service,
        project,
        executor,
        source_type="task_result",
        source_report_id=report["report_id"],
    )
    detail = service.get_knowledge_asset(project["id"], created["asset"]["id"])
    assert detail["versions"][0]["source_task_id"] == task["id"]
    assert detail["versions"][0]["source_report_id"] == report["report_id"]

    with pytest.raises(DomainError) as blocked:
        _review_asset(service, project, reviewer, created["asset"]["id"])
    assert blocked.value.code == "knowledge_task_not_verified"

    service.submit_review(
        project["id"],
        task["id"],
        reviewer_session_id=reviewer["agent"]["id"],
        token=reviewer["token"],
        verdict="approved",
        criteria=[{"criterion": "Gate enforced", "status": "passed"}],
    )
    approved = _review_asset(service, project, reviewer, created["asset"]["id"])
    assert approved["status"] == "approved"


def test_task_result_without_task_reference_is_rejected(
    service, project, joined_agents
):
    executor, _reviewer = joined_agents
    with pytest.raises(DomainError) as failure:
        _submit_candidate(service, project, executor, source_type="task_result")
    assert failure.value.code == "knowledge_source_task_required"


def test_revision_kind_change_is_rejected(service, project, joined_agents):
    executor, _reviewer = joined_agents
    created = _submit_candidate(service, project, executor)
    with pytest.raises(DomainError) as failure:
        _submit_candidate(
            service,
            project,
            executor,
            asset_id=created["asset"]["id"],
            kind="pitfall",
            title="Revised title",
            body="Revised body.",
        )
    assert failure.value.code == "knowledge_kind_immutable"
    detail = service.get_knowledge_asset(project["id"], created["asset"]["id"])
    assert detail["kind"] == "decision"


def test_cli_knowledge_review_serializes_failed_criteria():
    from agentchatroom.cli import parse_knowledge_criteria

    parsed = parse_knowledge_criteria(
        ["Docs match implementation::passed", "Gate enforced::failed"]
    )
    assert parsed == [
        {"criterion": "Docs match implementation", "status": "passed"},
        {"criterion": "Gate enforced", "status": "failed"},
    ]
    assert parse_knowledge_criteria(["Plain criterion"]) == [
        {"criterion": "Plain criterion", "status": "passed"}
    ]
    with pytest.raises(SystemExit):
        parse_knowledge_criteria(["::failed"])
