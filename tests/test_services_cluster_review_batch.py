"""Regression coverage for services-cluster review batch (#175-#200)."""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from agentchatroom.config import load_settings
from agentchatroom.database import SCHEMA, SCHEMA_VERSION, Database
from agentchatroom.errors import DomainError
from agentchatroom.project_registration import (
    logical_paths_equal,
    validate_logical_path,
)
from agentchatroom.services import AgentChatRoomService, iso_now, utc_now
from agentchatroom.task_history import history_detail_kind


@pytest.fixture()
def service(settings):
    instance = AgentChatRoomService(Database(settings.database_path), settings)
    instance.initialize()
    return instance


@pytest.fixture()
def project(service, tmp_path):
    root = tmp_path / "batch-project"
    root.mkdir()
    return service.create_project(root_path=str(root), name="Batch")


@pytest.fixture()
def joined(service, project):
    return service.join_room(
        project["id"], name="Agent", client="codex", model="unknown"
    )


# ---------------------------------------------------------------------------
# #175 list / document / intake volume limits
# ---------------------------------------------------------------------------


def test_acceptance_criteria_and_depends_on_count_limits(service, project, joined):
    limit = service.settings.list_input_max_count
    with pytest.raises(DomainError) as criteria:
        service.create_task(
            project["id"],
            title="too many criteria",
            acceptance_criteria=[f"c{i}" for i in range(limit + 1)],
            actor_session_id=joined["agent"]["id"],
            token=joined["token"],
        )
    assert criteria.value.code == "list_input_too_long"
    assert criteria.value.status_code == 422
    assert criteria.value.details["field"] == "acceptance_criteria"

    ok = service.create_task(
        project["id"],
        title="boundary criteria",
        acceptance_criteria=[f"c{i}" for i in range(limit)],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    assert len(ok["acceptance_criteria"]) == limit

    with pytest.raises(DomainError) as depends:
        service.create_task(
            project["id"],
            title="too many deps",
            acceptance_criteria=["ok"],
            depends_on=[f"task_missing_{i}" for i in range(limit + 1)],
            actor_session_id=joined["agent"]["id"],
            token=joined["token"],
        )
    assert depends.value.code == "list_input_too_long"
    assert depends.value.details["field"] == "depends_on"


def test_message_mentions_and_files_count_limits(service, project, joined):
    limit = service.settings.list_input_max_count
    with pytest.raises(DomainError) as mentions:
        service.post_message(
            project["id"],
            body="hi",
            session_id=joined["agent"]["id"],
            token=joined["token"],
            model_display_name="test",
            mentions=[f"@a{i}" for i in range(limit + 1)],
        )
    assert mentions.value.code == "list_input_too_long"
    assert mentions.value.details["field"] == "mentions"

    with pytest.raises(DomainError) as files:
        service.post_message(
            project["id"],
            body="hi",
            session_id=joined["agent"]["id"],
            token=joined["token"],
            model_display_name="test",
            files=[f"f{i}.py" for i in range(limit + 1)],
        )
    assert files.value.code == "list_input_too_long"
    assert files.value.details["field"] == "files"


def test_test_evidence_shape_rejects_extra_keys_and_long_notes(service, project, joined):
    limit = service.settings.list_input_max_count
    notes_limit = service.settings.test_entry_notes_max_length
    task = service.create_task(
        project["id"],
        title="report limits",
        acceptance_criteria=["report is valid"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    service.claim_task(
        project["id"], task["id"], joined["agent"]["id"], joined["token"]
    )

    with pytest.raises(DomainError) as extra:
        service.submit_work_report(
            project["id"],
            task["id"],
            session_id=joined["agent"]["id"],
            token=joined["token"],
            summary="s",
            files=["a.py"],
            tests=[{"command": "pytest", "exit_code": 0, "secret": "x"}],
        )
    assert extra.value.code == "invalid_test_evidence"
    assert extra.value.status_code == 422

    with pytest.raises(DomainError) as notes:
        service.submit_work_report(
            project["id"],
            task["id"],
            session_id=joined["agent"]["id"],
            token=joined["token"],
            summary="s",
            files=["a.py"],
            tests=[
                {
                    "command": "pytest",
                    "exit_code": 0,
                    "notes": "n" * (notes_limit + 1),
                }
            ],
        )
    assert notes.value.code == "invalid_test_evidence"
    assert notes.value.status_code == 422

    with pytest.raises(DomainError) as count:
        service.submit_work_report(
            project["id"],
            task["id"],
            session_id=joined["agent"]["id"],
            token=joined["token"],
            summary="s",
            files=["a.py"],
            tests=[{"command": "pytest", "exit_code": 0}] * (limit + 1),
        )
    assert count.value.code == "list_input_too_long"


def test_knowledge_tags_event_ids_and_review_criteria_limits(service, project, joined):
    limit = service.settings.list_input_max_count
    with pytest.raises(DomainError) as tags:
        service.submit_knowledge_candidate(
            project["id"],
            session_id=joined["agent"]["id"],
            token=joined["token"],
            title="t",
            body="b",
            kind="decision",
            tags=[f"t{i}" for i in range(limit + 1)],
        )
    assert tags.value.code == "list_input_too_long"
    assert tags.value.details["field"] == "tags"

    with pytest.raises(DomainError) as events:
        service.submit_knowledge_candidate(
            project["id"],
            session_id=joined["agent"]["id"],
            token=joined["token"],
            title="t",
            body="b",
            kind="decision",
            source_event_ids=list(range(1, limit + 2)),
        )
    assert events.value.code == "list_input_too_long"
    assert events.value.details["field"] == "source_event_ids"

    submitted = service.submit_knowledge_candidate(
        project["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
        title="Reviewable",
        body="Body",
        kind="decision",
    )
    asset_id = submitted["asset"]["id"]
    limit = service.settings.list_input_max_count
    with pytest.raises(DomainError) as criteria:
        service.submit_knowledge_review(
            project["id"],
            asset_id,
            reviewer_session_id=joined["agent"]["id"],
            token=joined["token"],
            verdict="approved",
            criteria=[
                {"criterion": f"c{i}", "status": "passed"}
                for i in range(limit + 1)
            ],
        )
    assert criteria.value.code == "list_input_too_long"
    assert criteria.value.details["field"] == "criteria"


def test_project_document_content_byte_limit(service, project):
    max_bytes = service.settings.project_document_max_bytes
    with pytest.raises(DomainError) as oversized:
        service.upsert_project_document(
            project["id"],
            doc_key="standards",
            kind="binding",
            title="Standards",
            content="x" * (max_bytes + 1),
        )
    assert oversized.value.code == "project_document_too_large"
    assert oversized.value.status_code == 422
    assert oversized.value.details["limit"] == max_bytes

    service.upsert_project_document(
        project["id"],
        doc_key="standards",
        kind="binding",
        title="Standards",
        content="x" * min(64, max_bytes),
    )


def test_task_intake_raw_description_length_limit(service, project, joined):
    limit = service.settings.task_text_max_length
    member_id = joined["agent"]["member_id"]
    with pytest.raises(DomainError) as raw:
        service.submit_task_intake(
            project["id"],
            raw_description="y" * (limit + 1),
            target_member_id=member_id,
            created_by_session_id=joined["agent"]["id"],
            token=joined["token"],
        )
    assert raw.value.code == "task_text_too_long"
    assert raw.value.status_code == 422


def test_list_input_config_env_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_LIST_INPUT_MAX_COUNT", "7")
    monkeypatch.setenv("AGENTCHATROOM_PROJECT_DOCUMENT_MAX_BYTES", "1024")
    monkeypatch.setenv("AGENTCHATROOM_IDEMPOTENCY_RETENTION_DAYS", "0")
    tuned = load_settings()
    assert tuned.list_input_max_count == 7
    assert tuned.project_document_max_bytes == 1024
    assert tuned.idempotency_retention_days == 0

    monkeypatch.setenv("AGENTCHATROOM_LIST_INPUT_MAX_COUNT", "0")
    with pytest.raises(ValueError, match="list_input_max_count must be positive"):
        load_settings()


# ---------------------------------------------------------------------------
# #182 restore race
# ---------------------------------------------------------------------------


def test_restore_serializes_with_create_backup_and_detects_stale_under_lock(
    service, project
):
    service.post_message(project["id"], body="before backup")
    created = service.create_backup()

    holder = sqlite3.connect(service.database.path, timeout=0.1)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(DomainError) as busy:
            service.restore_backup(created["output"], confirm="REPLACE")
        assert busy.value.code == "database_busy"
    finally:
        holder.rollback()
        holder.close()

    service.post_message(project["id"], body="after backup")
    with pytest.raises(DomainError) as stale:
        service.restore_backup(created["output"], confirm="REPLACE")
    assert stale.value.code == "backup_stale"
    assert stale.value.details["current_latest_event_id"] >= stale.value.details[
        "backup_latest_event_id"
    ]
    assert service._backup_restore_lock is not None


def test_create_backup_and_restore_share_process_lock(service, project):
    service.post_message(project["id"], body="x")
    created = service.create_backup()
    restored = service.restore_backup(
        created["output"], confirm="REPLACE", allow_data_loss=True
    )
    assert restored["restored"] is True


# ---------------------------------------------------------------------------
# #183 snapshot bounds + #186 room_sync writes
# ---------------------------------------------------------------------------


def test_snapshot_reports_reviews_acks_are_bounded_with_totals(service, project, joined):
    settings = replace(service.settings, snapshot_recent_limit=2)
    service.settings = settings
    for index in range(5):
        service.post_message(
            project["id"],
            body=f"m{index}",
            session_id=joined["agent"]["id"],
            token=joined["token"],
            model_display_name="test",
        )
    snapshot = service.snapshot(project["id"])
    assert "totals" in snapshot
    assert "page_info" in snapshot
    assert snapshot["page_info"]["limit"] == 2
    assert snapshot["page_info"]["reports"]["total"] >= 0
    # reports/reviews/acks arrays never exceed recent limit
    assert len(snapshot["reports"]) <= 2
    assert len(snapshot["reviews"]) <= 2
    assert len(snapshot["acknowledgements"]) <= 2
    # compatibility: existing keys retained
    for key in ("reports", "reviews", "acknowledgements", "tasks", "cursor"):
        assert key in snapshot
    # #183: each page_info slice carries next/before cursors for continuation.
    for kind in ("reports", "reviews", "acknowledgements"):
        info = snapshot["page_info"][kind]
        assert "next" in info
        assert "before" in info
        assert "has_more" in info
        assert "total" in info


def test_snapshot_cost_bounded_with_many_events_and_reports(
    service, project, joined, monkeypatch
):
    # ≥5000 events and ≥200 work_reports: projection must stay bounded.
    report_task = service.create_task(
        project["id"],
        title="Snapshot perf",
        description="bulk reports for bounded projection",
        acceptance_criteria=["bounded"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    task_id = report_task["task"]["id"]
    connection = sqlite3.connect(service.database.path)
    try:
        connection.execute("BEGIN")
        payload = '{"schema_version":7,"body":"x"}'
        for i in range(5000):
            connection.execute(
                """
                INSERT INTO events(project_id, event_type, payload_json, created_at)
                VALUES (?, 'message.message', ?, ?)
                """,
                (project["id"], payload, iso_now()),
            )
        for i in range(200):
            connection.execute(
                """
                INSERT INTO work_reports(
                    id, project_id, task_id, session_id, summary, files_json,
                    tests_json, system_evidence_json, commit_hash, created_at
                )
                VALUES (?, ?, ?, ?, ?, '[]', '[]', '{}', '', ?)
                """,
                (
                    f"wr-bulk-{i:04d}",
                    project["id"],
                    task_id,
                    joined["agent"]["id"],
                    f"report {i}",
                    iso_now(),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    settings = replace(service.settings, snapshot_recent_limit=50)
    service.settings = settings
    started = time.perf_counter()
    snapshot = service.snapshot(project["id"])
    elapsed = time.perf_counter() - started
    encoded = __import__("json").dumps(snapshot, ensure_ascii=False)
    assert len(snapshot["reports"]) <= 50
    assert snapshot["page_info"]["limit"] == 50
    assert snapshot["totals"]["reports"] >= 200
    assert snapshot["page_info"]["reports"]["total"] >= 200
    assert snapshot["page_info"]["reports"]["has_more"] is True
    assert snapshot["page_info"]["reports"]["has_more_newer"] is False
    assert snapshot["page_info"]["reports"]["next"]
    assert snapshot["page_info"]["reports"]["before"]
    # Cursor must carry created_at|id tiebreaker (#183).
    next_cursor = snapshot["page_info"]["reports"]["next"]
    before_cursor = snapshot["page_info"]["reports"]["before"]
    assert "|" in next_cursor and "|" in before_cursor

    # Consumable older page: pass next → reports_before.
    page2 = service.snapshot(project["id"], reports_before=next_cursor)
    assert page2["reports"], "older page must be non-empty when has_more"
    page2_ids = {row["id"] for row in page2["reports"]}
    page1_ids = {row["id"] for row in snapshot["reports"]}
    assert not (page1_ids & page2_ids), "pages must not overlap"
    # Older page items are strictly older than the page1 window boundary.
    boundary_created = next_cursor.split("|", 1)[0]
    assert all(row["created_at"] <= boundary_created for row in page2["reports"])

    # Walk to the last older page: has_more/next must go false — no fake next (#183).
    current = page2
    seen_ids = set(page1_ids) | set(page2_ids)
    for _ in range(10):
        info = current["page_info"]["reports"]
        if not info["has_more"]:
            assert info["next"] is None, "last page must not advertise next"
            break
        nxt = info["next"]
        assert nxt
        current = service.snapshot(project["id"], reports_before=nxt)
        new_ids = {row["id"] for row in current["reports"]}
        assert not (new_ids & seen_ids)
        seen_ids |= new_ids
    else:
        raise AssertionError("report pages did not terminate")
    assert current["page_info"]["reports"]["has_more"] is False
    assert current["page_info"]["reports"]["next"] is None

    # Consumable newer page: pass before → reports_after returns items newer than head.
    newer = service.snapshot(project["id"], reports_after=before_cursor)
    newer_ids = {row["id"] for row in newer["reports"]}
    assert not (newer_ids & page2_ids)

    # reviews + acknowledgements must page with the same contract (#183).
    for kind, before_key, after_key in (
        ("reviews", "reviews_before", "reviews_after"),
        ("acknowledgements", "acknowledgements_before", "acknowledgements_after"),
    ):
        first = service.snapshot(project["id"])
        info = first["page_info"][kind]
        assert "next" in info and "before" in info and "has_more" in info
        if info["has_more"]:
            assert info["next"]
            second = service.snapshot(
                project["id"], **{before_key: info["next"]}
            )
            first_keys = {
                (r.get("created_at"), r.get("id") or r.get("event_id"))
                for r in first[kind]
            }
            second_keys = {
                (r.get("created_at"), r.get("id") or r.get("event_id"))
                for r in second[kind]
            }
            assert not (first_keys & second_keys)
            # walk to terminal page
            cur = second
            for _ in range(10):
                cinfo = cur["page_info"][kind]
                if not cinfo["has_more"]:
                    assert cinfo["next"] is None
                    break
                cur = service.snapshot(
                    project["id"], **{before_key: cinfo["next"]}
                )
            else:
                raise AssertionError(f"{kind} pages did not terminate")

    # Bounded payload: recent window only, not the full 200-report history.
    assert len(encoded) < 2_000_000
    # Bounded projection: must finish quickly even with 5k events + 200 reports.
    assert elapsed < 5.0


def test_snapshot_ack_cursor_compares_project_seq_not_physical_id(
    service, project, joined
):
    """#183: ack cursor encodes project_seq; WHERE must compare project_seq."""
    other = service.post_message(
        project["id"],
        body="ack me",
        session_id=joined["agent"]["id"],
        token=joined["token"],
        model_display_name="test",
        requires_ack=True,
    )
    service.acknowledge_event(
        project["id"],
        event_id=other["event_id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    # Build two more acks via direct insert so recent_limit=1 has pages.
    connection = sqlite3.connect(service.database.path)
    try:
        connection.execute("BEGIN")
        for i, seq in enumerate((other["event_id"] + 50, other["event_id"] + 60)):
            connection.execute(
                """
                INSERT INTO events(project_id, event_type, payload_json, created_at, project_seq)
                VALUES (?, 'message.message', ?, ?, ?)
                """,
                (
                    project["id"],
                    '{"schema_version":7,"body":"x"}',
                    iso_now(),
                    seq,
                ),
            )
            physical = connection.execute("SELECT last_insert_rowid() AS id").fetchone()[0]
            connection.execute(
                """
                INSERT INTO event_acknowledgements(event_id, session_id, created_at)
                VALUES (?, ?, ?)
                """,
                (physical, joined["agent"]["id"], iso_now()),
            )
        connection.commit()
    finally:
        connection.close()

    service.settings = replace(service.settings, snapshot_recent_limit=1)
    first = service.snapshot(project["id"])
    ack_info = first["page_info"]["acknowledgements"]
    assert ack_info["returned"] <= 1
    if ack_info["before"]:
        parts = ack_info["before"].split("|")
        assert len(parts) == 3
        # event_id in cursor is project_seq (not physical e.id).
        assert int(parts[1]) == other["event_id"] or int(parts[1]) >= other["event_id"]
    assert ack_info["has_more"] is True
    assert ack_info["next"]
    page2 = service.snapshot(
        project["id"], acknowledgements_before=ack_info["next"]
    )
    first_keys = {
        (r["created_at"], r["event_id"], r["session_id"]) for r in first["acknowledgements"]
    }
    second_keys = {
        (r["created_at"], r["event_id"], r["session_id"]) for r in page2["acknowledgements"]
    }
    assert not (first_keys & second_keys), "ack pages must not overlap"
    assert page2["acknowledgements"], "older ack page must be non-empty"


def test_snapshot_page_cursor_rejects_malformed_and_both_directions(
    service, project, joined
):
    with pytest.raises(DomainError) as bad:
        service.snapshot(project["id"], reports_before="not-a-cursor")
    assert bad.value.code == "invalid_page_cursor"
    assert bad.value.status_code == 422

    with pytest.raises(DomainError) as both:
        service.snapshot(
            project["id"],
            reports_before="2026-01-01T00:00:00Z|x",
            reports_after="2026-01-01T00:00:00Z|x",
        )
    assert both.value.code == "invalid_page_cursor"


def test_snapshot_single_row_latest_page_has_no_fake_newer_continuation(
    service, project, joined
):
    """#183: 1 report + limit=1 on the newest page must not claim has_more_newer."""
    report_task = service.create_task(
        project["id"],
        title="Solo",
        description="one report only",
        acceptance_criteria=["ok"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    connection = sqlite3.connect(service.database.path)
    try:
        connection.execute(
            """
            INSERT INTO work_reports(
                id, project_id, task_id, session_id, summary, files_json,
                tests_json, system_evidence_json, commit_hash, created_at
            )
            VALUES ('wr-solo', ?, ?, ?, 'solo', '[]', '[]', '{}', '', ?)
            """,
            (
                project["id"],
                report_task["task"]["id"],
                joined["agent"]["id"],
                iso_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    service.settings = replace(service.settings, snapshot_recent_limit=1)
    snap = service.snapshot(project["id"])
    info = snap["page_info"]["reports"]
    assert info["returned"] == 1
    assert info["has_more"] is False
    assert info["has_more_newer"] is False
    assert info["next"] is None
    # before is the window high-water mark (poll cursor), not a continuation page.
    assert info["before"]
    assert "|" in info["before"]


def test_snapshot_tied_created_at_pages_by_id_without_gap_or_overlap(
    service, project, joined
):
    """#183: identical created_at must still page cleanly via id tiebreaker."""
    report_task = service.create_task(
        project["id"],
        title="Tied",
        description="same timestamps",
        acceptance_criteria=["ok"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )
    tied_at = "2026-01-01T00:00:00Z"
    connection = sqlite3.connect(service.database.path)
    try:
        for i in range(5):
            connection.execute(
                """
                INSERT INTO work_reports(
                    id, project_id, task_id, session_id, summary, files_json,
                    tests_json, system_evidence_json, commit_hash, created_at
                )
                VALUES (?, ?, ?, ?, ?, '[]', '[]', '{}', '', ?)
                """,
                (
                    f"wr-tied-{i}",
                    project["id"],
                    report_task["task"]["id"],
                    joined["agent"]["id"],
                    f"t{i}",
                    tied_at,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    service.settings = replace(service.settings, snapshot_recent_limit=2)
    page1 = service.snapshot(project["id"])
    ids1 = [r["id"] for r in page1["reports"]]
    assert len(ids1) == 2
    assert page1["page_info"]["reports"]["has_more"] is True
    page2 = service.snapshot(
        project["id"], reports_before=page1["page_info"]["reports"]["next"]
    )
    ids2 = [r["id"] for r in page2["reports"]]
    page3 = service.snapshot(
        project["id"], reports_before=page2["page_info"]["reports"]["next"]
    )
    ids3 = [r["id"] for r in page3["reports"]]
    all_ids = ids1 + ids2 + ids3
    assert sorted(all_ids) == sorted({*all_ids}), "no overlap"
    assert len(all_ids) == 5, "no gap across tied-timestamp pages"
    assert page3["page_info"]["reports"]["has_more"] is False
    assert page3["page_info"]["reports"]["next"] is None


def test_events_project_type_task_index_exists():
    assert SCHEMA_VERSION >= 23
    assert "idx_events_project_type_task" in SCHEMA


def _session_row(service, session_id):
    connection = sqlite3.connect(service.database.path)
    try:
        row = connection.execute(
            "SELECT last_heartbeat, last_read_cursor FROM agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        return {"last_heartbeat": row[0], "last_read_cursor": int(row[1] or 0)}
    finally:
        connection.close()


def test_room_sync_merges_heartbeat_and_cursor_single_write(service, project, joined):
    session_id = joined["agent"]["id"]
    token = joined["token"]
    service.post_message(
        project["id"],
        body="hello",
        session_id=session_id,
        token=token,
        model_display_name="test",
    )
    before = _session_row(service, session_id)
    synced = service.room_sync(
        project["id"], session_id=session_id, token=token, after=0
    )
    after = _session_row(service, session_id)
    assert after["last_heartbeat"] >= before["last_heartbeat"]
    assert after["last_read_cursor"] >= before["last_read_cursor"]
    assert synced["unread_count"] == 0

    empty = service.room_sync(
        project["id"],
        session_id=session_id,
        token=token,
        after=max(int(synced.get("cursor") or 0), after["last_read_cursor"]),
    )
    assert empty["unread_count"] == 0
    final = _session_row(service, session_id)
    assert final["last_heartbeat"] >= after["last_heartbeat"]
    assert final["last_read_cursor"] >= after["last_read_cursor"]

    service.room_sync(project["id"], session_id=session_id, token=token, after=0)
    assert _session_row(service, session_id)["last_read_cursor"] >= final[
        "last_read_cursor"
    ]


# ---------------------------------------------------------------------------
# #189 UNC rejection
# ---------------------------------------------------------------------------


def test_register_workspace_rejects_unc_paths(service, project):
    for unc in (r"\\attacker\share\repo", "//attacker/share/repo"):
        with pytest.raises(DomainError) as error:
            service.register_workspace(
                project["id"],
                host_key="h-unc",
                host_name="H",
                local_path=unc,
            )
        assert error.value.code == "invalid_workspace"
        assert "UNC" in error.value.message or "local path" in error.value.message

    local_ok = service.register_workspace(
        project["id"],
        host_key="h-local",
        host_name="H",
        local_path=r"C:\workspace\project",
    )
    assert local_ok["workspace"]["local_path"] == r"C:\workspace\project"

    posix = service.register_workspace(
        project["id"],
        host_key="h-posix",
        host_name="H",
        local_path="/var/lib/project",
    )
    assert posix["workspace"]["local_path"] == "/var/lib/project"


# ---------------------------------------------------------------------------
# #193 idempotency retention
# ---------------------------------------------------------------------------


def test_idempotency_retention_deletes_old_rows_keeps_window(service, project, joined):
    settings = replace(service.settings, idempotency_retention_days=30)
    service.settings = settings
    result = service.post_message(
        project["id"],
        body="idempotent",
        session_id=joined["agent"]["id"],
        token=joined["token"],
        model_display_name="test",
        request_id="idem-retention-1",
    )
    assert result["event_id"]

    connection = sqlite3.connect(service.database.path)
    try:
        rows = connection.execute(
            "SELECT scope, request_id, created_at FROM idempotency_records"
        ).fetchall()
        assert rows
        # Age one row past retention.
        old_cutoff = (
            utc_now() - timedelta(days=31)
        ).isoformat().replace("+00:00", "Z")
        scope, request_id, _ = rows[0]
        connection.execute(
            "UPDATE idempotency_records SET created_at = ? WHERE scope = ? AND request_id = ?",
            (old_cutoff, scope, request_id),
        )
        # Insert a recent row that must survive.
        connection.execute(
            """
            INSERT INTO idempotency_records(
                scope, request_id, operation, request_hash, response_json,
                created_at, completed_at
            ) VALUES (?, ?, 'keep', 'h', '{}', ?, ?)
            """,
            ("keep-scope", "keep-id", iso_now(), iso_now()),
        )
        connection.commit()
    finally:
        connection.close()

    swept = service.enforce_idempotency_retention(force=True)
    assert swept["retention_days"] == 30
    assert swept["deleted"] >= 1

    connection = sqlite3.connect(service.database.path)
    try:
        remaining = connection.execute(
            "SELECT request_id FROM idempotency_records"
        ).fetchall()
        ids = {row[0] for row in remaining}
        assert request_id not in ids
        assert "keep-id" in ids
    finally:
        connection.close()


def test_idempotency_retention_zero_keeps_forever(service, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_IDEMPOTENCY_RETENTION_DAYS", "0")
    settings = load_settings()
    instance = AgentChatRoomService(Database(settings.database_path), settings)
    instance.initialize()
    result = instance.enforce_idempotency_retention(force=True)
    assert result == {"deleted": 0, "retention_days": 0}


# ---------------------------------------------------------------------------
# #194 dependency AND semantics (covered in test_services; extra forms here)
# ---------------------------------------------------------------------------


def test_dependency_gate_requires_integration_done(service, project):
    registered = service.register_workspace(
        project["id"],
        host_key="test-host",
        host_name="Test Host",
        local_path=project["root_path"],
    )
    joined = service.join_room(
        project["id"],
        agent_key="dep-exec-main",
        name="Dep Exec",
        client="codex",
        model="unknown",
        worktree=project["root_path"],
        host_id=registered["host"]["id"],
        workspace_id=registered["workspace"]["id"],
    )
    dep = service.create_task(
        project["id"],
        title="dep",
        acceptance_criteria=["dep ok"],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]
    child = service.create_task(
        project["id"],
        title="child",
        acceptance_criteria=["child ok"],
        depends_on=[dep["id"]],
        actor_session_id=joined["agent"]["id"],
        token=joined["token"],
    )["task"]

    # unverified blocks
    with pytest.raises(DomainError) as unverified:
        service.claim_task(
            project["id"], child["id"], joined["agent"]["id"], joined["token"]
        )
    assert unverified.value.code == "task_dependencies_incomplete"

    # approved but not integrated still blocks
    service.claim_task(
        project["id"], dep["id"], joined["agent"]["id"], joined["token"]
    )
    service.submit_work_report(
        project["id"],
        dep["id"],
        session_id=joined["agent"]["id"],
        token=joined["token"],
        summary="done",
        files=["d.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    # Need independent reviewer: use a different software identity.
    other = service.join_room(
        project["id"],
        software_key="other-inst",
        name="Other",
        client="other",
        model="unknown",
    )
    service.submit_review(
        project["id"],
        dep["id"],
        reviewer_session_id=other["agent"]["id"],
        token=other["token"],
        verdict="approved",
        criteria=[{"criterion": "dep ok", "status": "passed"}],
    )
    with pytest.raises(DomainError) as approved_pending:
        service.claim_task(
            project["id"], child["id"], joined["agent"]["id"], joined["token"]
        )
    assert approved_pending.value.code == "task_dependencies_incomplete"

    service.submit_integration(
        project["id"],
        dep["id"],
        integrator_session_id=other["agent"]["id"],
        token=other["token"],
        result="done",
        summary="integrated",
        files=["d.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    claimed = service.claim_task(
        project["id"], child["id"], joined["agent"]["id"], joined["token"]
    )
    assert claimed["task"]["status"] == "claimed"


# ---------------------------------------------------------------------------
# #198 logical_path case handling
# ---------------------------------------------------------------------------


def test_logical_path_case_preserved_and_compared_casefold():
    assert validate_logical_path("Packages/MyLib") == "Packages/MyLib"
    assert logical_paths_equal("Packages/MyLib", "packages/mylib")
    # Windows-style registration vs Linux-style candidate must match.
    stored = validate_logical_path("packages/mylib")
    candidate = validate_logical_path("Packages/MyLib")
    assert logical_paths_equal(stored, candidate)


# ---------------------------------------------------------------------------
# #199 join orphan adoption
# ---------------------------------------------------------------------------


def test_second_install_with_same_client_does_not_absorb_orphans(service, project):
    first = service.join_room(
        project["id"],
        software_key="zcode-aaa",
        name="Z-A",
        client="zcode",
        model="unknown",
    )
    # Create an orphan session (member_id NULL) with same client.
    connection = sqlite3.connect(service.database.path)
    try:
        connection.execute(
            """
            INSERT INTO agent_sessions(
                id, project_id, member_id, agent_key, name, client, model, role,
                status, branch, worktree, capabilities_json, metadata_json,
                token_hash, token_expires_at, created_at, last_heartbeat
            ) VALUES (?, ?, NULL, '', 'orphan', 'zcode', 'unknown', 'executor',
                      'online', '', '', '{}', '{}', 'hash', ?, ?, ?)
            """,
            (
                "agent_orphan_1",
                project["id"],
                iso_now(),
                iso_now(),
                iso_now(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    second = service.join_room(
        project["id"],
        software_key="zcode-bbb",
        name="Z-B",
        client="zcode",
        model="unknown",
    )
    connection = sqlite3.connect(service.database.path)
    try:
        row = connection.execute(
            "SELECT member_id FROM agent_sessions WHERE id = ?",
            ("agent_orphan_1",),
        ).fetchone()
        # Second install must not adopt the orphan into its member.
        assert row[0] is None or row[0] == first["agent"]["member_id"]
        # Prefer: remains NULL when two members share the client.
        member_count = connection.execute(
            """
            SELECT COUNT(DISTINCT s.member_id)
            FROM agent_sessions s
            WHERE s.project_id = ? AND s.member_id IS NOT NULL
              AND lower(trim(s.client)) = 'zcode'
            """,
            (project["id"],),
        ).fetchone()[0]
        if member_count >= 2:
            assert row[0] is None
    finally:
        connection.close()
    assert second["agent"]["id"]


# ---------------------------------------------------------------------------
# #200 event naming
# ---------------------------------------------------------------------------


def test_work_commit_unverified_uses_work_prefix_and_history_kind():
    assert history_detail_kind("work.commit_unverified") == "work_report"
    assert history_detail_kind("work_report.commit_unverified") == "work_report"
    services = (
        Path(__file__).parents[1] / "src" / "agentchatroom" / "services.py"
    ).read_text(encoding="utf-8")
    assert re.search(r'"work\.commit_unverified"', services)
    assert not re.search(
        r'"work_report\.commit_unverified"\s*,\s*\n\s*actor_session_id',
        services,
    )
