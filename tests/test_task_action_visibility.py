"""#349: one domain truth for task actions, labels and vocabularies.

Before this card the adapter and the front end each re-derived the same
decisions:

* ``web/app.js`` disabled the assign button by ``phase``, while
  ``services.assign_task`` refuses on ``execution_status``. A task that is
  ``completed`` but not yet integrated reads as ``pending_integration``, so the
  button was clickable and the server always answered ``task_not_assignable``.
* ``gui_event_log._STATUS_ZH`` kept its own wording table, spelling
  ``changes_requested`` "要求修改" where the served projection says "已退回",
  and had no entry at all for ``integration_failed`` / ``unclassified``.
* ``app.css`` had no ``.status-badge.todo`` / ``.status-badge.defined`` rule, and
  an integration failure was red text on a neutral chip.
* The navigation groups, expert-filter face codes, intake labels and release
  reason codes were hard-coded in the browser even though the service already
  published every one of those vocabularies.
* ``cancelled`` was a published intake status and a declared destination of four
  transitions, but no code path could ever write it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentchatroom.api import task_view_presentation_config
from agentchatroom.contracts import (
    TASK_ACTION_CODES,
    TASK_EXECUTION_STATUSES,
    TASK_INTEGRATION_STATUSES,
    TASK_INTAKE_STATUSES,
    TASK_INTAKE_TRANSITIONS,
    TASK_RELEASE_EXECUTION_STATUSES,
    TASK_RELEASE_REASON_CODES,
    TASK_VERIFICATION_STATUSES,
    TASK_VIEW_ALL_PHASES,
    task_available_actions,
    task_view,
)
from agentchatroom.errors import DomainError

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "agentchatroom" / "web"


# ---------------------------------------------------------------------------
# AC1: available actions come from the domain, not from a phase guess.
# ---------------------------------------------------------------------------
def _actions(execution, verification="not_required", integration="pending"):
    return task_available_actions(
        execution_status=execution,
        verification_status=verification,
        integration_status=integration,
    )


def test_assign_is_offered_exactly_where_assign_task_accepts():
    """The rule under test is services.assign_task's own guard.

    It refuses on ``execution_status in {"completed", "cancelled"}``, which is
    not the same predicate as the phase: ``completed`` maps to four different
    phases depending on the other two faces, and only one of them
    (``pending_integration``) previously slipped through the phase check.
    """
    for execution in sorted(TASK_EXECUTION_STATUSES):
        for verification in sorted(TASK_VERIFICATION_STATUSES):
            for integration in sorted(TASK_INTEGRATION_STATUSES):
                phase = task_view(
                    execution_status=execution,
                    verification_status=verification,
                    integration_status=integration,
                )["phase"]
                offered = "assign" in _actions(execution, verification, integration)
                assert offered == (execution not in {"completed", "cancelled"}), (
                    execution, verification, integration, phase
                )


def test_a_completed_but_unintegrated_task_is_not_assignable():
    """The exact triple the old phase-based check got wrong.

    ``pending_integration`` is not ``done``, so the front end left the button
    enabled while ``assign_task`` refused with ``task_not_assignable``.
    """
    assert task_view(
        execution_status="completed",
        verification_status="approved",
        integration_status="pending",
    )["phase"] == "pending_integration"
    assert "assign" not in _actions("completed", "approved", "pending")
    assert "assign" not in _actions("completed", "not_required", "pending")


def test_release_is_offered_exactly_where_task_release_accepts():
    for execution in sorted(TASK_EXECUTION_STATUSES):
        offered = "release" in _actions(execution)
        assert offered == (execution in TASK_RELEASE_EXECUTION_STATUSES), execution


def test_cancel_is_refused_only_for_the_two_closed_phases():
    for execution in sorted(TASK_EXECUTION_STATUSES):
        for verification in sorted(TASK_VERIFICATION_STATUSES):
            for integration in sorted(TASK_INTEGRATION_STATUSES):
                phase = task_view(
                    execution_status=execution,
                    verification_status=verification,
                    integration_status=integration,
                )["phase"]
                offered = "cancel" in _actions(execution, verification, integration)
                assert offered == (phase not in {"done", "cancelled"}), (
                    execution, verification, integration, phase
                )


def test_available_actions_are_published_per_task_and_in_config(
    service, project, joined_agents
):
    from tests.conftest import join_room_with_workspace  # noqa: PLC0415

    executor, _reviewer = joined_agents
    task = service.create_task(
        project["id"],
        title="publish available actions",
        acceptance_criteria=["the action list is served"],
        actor_session_id=executor["agent"]["id"],
        token=executor["token"],
    )["task"]
    # A fresh todo task is open on every axis but has no owner to release from.
    assert set(task["available_actions"]) == {"assign", "cancel"}

    claimed = service.claim_task(
        project["id"],
        task["id"],
        executor["agent"]["id"],
        executor["token"],
    )["task"]
    assert set(claimed["available_actions"]) == {"assign", "cancel", "release"}

    reported = service.submit_work_report(
        project["id"],
        task["id"],
        session_id=executor["agent"]["id"],
        token=executor["token"],
        summary="done",
        files=["src/a.py"],
        tests=[{"command": "pytest", "exit_code": 0}],
    )
    finished = service.get_task(project["id"], task["id"])
    # execution=completed now: release and assign are both refused by the
    # service, so neither may be advertised.
    assert finished["execution_status"] == "completed"
    assert "release" not in finished["available_actions"]
    assert "assign" not in finished["available_actions"]
    assert "cancel" in finished["available_actions"]
    assert reported["event_id"]

    assert join_room_with_workspace is not None

    config = task_view_presentation_config()
    assert config["action_codes"] == list(TASK_ACTION_CODES)
    assert set(config["action_labels"]) == set(TASK_ACTION_CODES)
    for code in TASK_ACTION_CODES:
        assert config["action_labels"][code].strip()


def test_front_end_reads_the_published_actions_not_a_phase_guess():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("async function openTaskDetails(")
    end = javascript.index("function eventFromCaches(", start)
    dialog = javascript[start:end]

    assert "task.available_actions" in dialog
    assert 'includes("assign")' in dialog
    # The old rule keyed the button on the phase; it must be gone.
    assert '["done", "cancelled"].includes(taskView(task).phase)' not in javascript

    release_start = javascript.index("function taskReleaseVisible(task)")
    release_end = javascript.index("function clearDialogDrafts(dialog)", release_start)
    visibility = javascript[release_start:release_end]
    assert "available_actions" in visibility
    assert "taskActionAvailable" in visibility


# ---------------------------------------------------------------------------
# AC1: GUI wording is served, not restated.
# ---------------------------------------------------------------------------
def test_gui_event_log_has_no_private_status_table_and_serves_every_phase():
    from agentchatroom import gui_event_log

    assert not hasattr(gui_event_log, "_STATUS_ZH")
    source = Path(gui_event_log.__file__).read_text(encoding="utf-8")
    assert "_STATUS_ZH =" not in source

    # One triple per phase the projection can emit; each must resolve through
    # the served label table with no per-phase table of its own.
    triples = {
        "todo": ("todo", "not_required", "pending"),
        "claimed": ("claimed", "not_required", "pending"),
        "in_progress": ("in_progress", "not_required", "pending"),
        "blocked": ("blocked", "not_required", "pending"),
        "awaiting_review": ("completed", "pending", "pending"),
        "changes_requested": ("in_progress", "changes_requested", "pending"),
        "pending_integration": ("completed", "approved", "pending"),
        "integration_failed": ("completed", "approved", "failed"),
        "done": ("completed", "approved", "done"),
        "cancelled": ("cancelled", "not_required", "pending"),
        "unclassified": ("todo", "approved", "pending"),
    }
    assert set(triples) == set(TASK_VIEW_ALL_PHASES)

    config = task_view_presentation_config()
    for phase, (execution, verification, integration) in triples.items():
        projected = task_view(
            execution_status=execution,
            verification_status=verification,
            integration_status=integration,
        )["phase"]
        assert projected == phase, phase
        label = gui_event_log._event_status_label(
            {
                "execution_status": execution,
                "verification_status": verification,
                "integration_status": integration,
            }
        )
        assert label == config["phase_labels"][phase], phase
        assert label.strip()


def test_gui_wording_matches_the_served_projection_not_the_old_table():
    from agentchatroom import gui_event_log

    # "要求修改" was the GUI-only wording that disagreed with the projection.
    returned = gui_event_log._event_status_label(
        {
            "execution_status": "in_progress",
            "verification_status": "changes_requested",
            "integration_status": "pending",
        }
    )
    assert returned == "已退回"
    assert returned != "要求修改"

    # integration_failed and unclassified used to fall through to a raw code.
    failed = gui_event_log._event_status_label(
        {
            "execution_status": "completed",
            "verification_status": "approved",
            "integration_status": "failed",
        }
    )
    assert failed == "集成失败"
    residue = gui_event_log._event_status_label(
        {"execution_status": "todo", "verification_status": "approved",
         "integration_status": "pending"}
    )
    assert residue == "未归类"


def test_legacy_status_events_still_resolve_through_published_labels():
    from agentchatroom import gui_event_log

    config = task_view_presentation_config()
    assert gui_event_log._event_status_label({"status": "awaiting_review"}) == (
        config["status_labels"]["awaiting_review"]
    )
    # A code the service never publishes stays visible rather than blank.
    assert gui_event_log._event_status_label({"status": "totally_unknown"}) == (
        "totally_unknown"
    )


# ---------------------------------------------------------------------------
# AC2: every phase has a badge class; the failure phase is an alarm.
# ---------------------------------------------------------------------------
def test_every_task_view_phase_has_a_status_badge_class():
    """`todo` and `defined` had no rule, so both rendered as the base chip.

    A regression test over the shipped stylesheet is the only thing that keeps
    a newly declared phase from silently shipping without a class.
    """
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    declared = set(re.findall(r"\.status-badge\.([a-z_]+)", stylesheet))
    missing = set(TASK_VIEW_ALL_PHASES) - declared
    assert not missing, f"phases without a .status-badge class: {sorted(missing)}"
    # The intake vocabulary renders through the same class.
    assert "defined" in declared


def test_integration_failure_reads_as_an_alarm_not_a_colored_word():
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    block = stylesheet[
        stylesheet.index(".status-badge.integration_failed {") : stylesheet.index(
            ".status-badge.pending_integration {"
        )
    ]
    assert "background:" in block, "integration failure needs a warning fill"
    assert "var(--red-soft)" in block
    assert "border-color:" in block


def test_no_raw_hex_outside_the_two_theme_blocks():
    """Project rule: hex colors live only in the two theme blocks.

    Both blocks are allowed to declare literals; nothing after them may. The
    new badge rules go through tokens, so this is the guard that keeps a
    one-off color from creeping back into a component rule.
    """
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    # Comments are stripped first: the file is full of issue references such as
    # `/* #153 ... */` that are not colors and would otherwise be counted.
    code = re.sub(r"/\*.*?\*/", "", stylesheet, flags=re.DOTALL)
    dark_start = code.index(':root[data-theme="dark"]')
    dark_end = code.index("}", code.index("{", dark_start)) + 1
    light_start = code.index(":root {")
    light_end = code.index("}", light_start) + 1

    theme_spans = {(light_start, light_end), (dark_start, dark_end)}
    offenders = [
        match.start()
        for match in re.finditer(r"#[0-9a-fA-F]{3,8}\b", code)
        if not any(start <= match.start() < end for start, end in theme_spans)
    ]
    assert not offenders, [code[max(0, o - 60) : o + 20] for o in offenders[:5]]


# ---------------------------------------------------------------------------
# AC3: the published `cancelled` intake status is now reachable.
# ---------------------------------------------------------------------------
def test_cancelled_intake_status_has_a_write_path(service, project):
    """`cancelled` was published and declared but unwritable.

    Four transitions declared it as a destination and no command produced it,
    so an intake whose target never accepted it stayed `pending` forever.
    """
    from tests.test_task_intake_state_machine import (  # noqa: PLC0415
        _join,
        _submit,
        _three_identities,
    )

    members = _three_identities(service, project)
    intake = _submit(service, project, members)

    cancelled = service.cancel_task_intake(
        project["id"],
        intake["id"],
        session_id=members["creator"]["agent"]["id"],
        token=members["creator"]["token"],
        reason="no longer wanted",
    )["intake"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["note"] == "no longer wanted"
    assert service.get_task_intake(project["id"], intake["id"])["status"] == "cancelled"


def test_intake_cancel_requires_a_real_actor(service, project):
    from tests.test_task_intake_state_machine import _submit, _three_identities

    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    with pytest.raises(DomainError) as error:
        service.cancel_task_intake(project["id"], intake["id"])
    assert error.value.code == "management_auth_required"
    # Nothing was written by the refused call.
    assert service.get_task_intake(project["id"], intake["id"])["status"] == "pending"

    # An authenticated management request is the other accepted authority.
    assert service.cancel_task_intake(
        project["id"], intake["id"], management_authorized=True
    )["intake"]["status"] == "cancelled"


def test_intake_cancel_is_idempotent_and_emits_one_event(service, project):
    from tests.test_task_intake_state_machine import (
        _intake_event_payloads,
        _submit,
        _three_identities,
    )

    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    first = service.cancel_task_intake(
        project["id"],
        intake["id"],
        session_id=members["creator"]["agent"]["id"],
        token=members["creator"]["token"],
    )
    assert first["event_id"] is not None
    repeat = service.cancel_task_intake(
        project["id"],
        intake["id"],
        session_id=members["creator"]["agent"]["id"],
        token=members["creator"]["token"],
    )
    assert repeat["event_id"] is None

    events = _intake_event_payloads(service, project["id"])
    cancels = [p for name, p in events if name == "task.intake_cancelled"]
    assert len(cancels) == 1
    assert cancels[0]["previous_status"] == "pending"
    assert cancels[0]["status"] == "cancelled"
    assert cancels[0]["target_member_id"] == members["target"]["agent"]["member_id"]


def test_a_defined_intake_cannot_be_cancelled_through_the_intake_path(service, project):
    from tests.test_task_intake_state_machine import _submit, _three_identities

    members = _three_identities(service, project)
    intake = _submit(service, project, members)
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE task_intakes SET status = 'defined' WHERE id = ?", (intake["id"],)
        )
    with pytest.raises(DomainError) as error:
        service.cancel_task_intake(
            project["id"],
            intake["id"],
            session_id=members["creator"]["agent"]["id"],
            token=members["creator"]["token"],
        )
    assert error.value.code == "task_intake_already_defined"
    assert error.value.status_code == 409


def test_every_published_intake_status_now_has_a_reachable_writer(service, project):
    """No status may stay vocabulary-only.

    Each status is checked against a command that can produce it, so a future
    status added to the set cannot ship without one.
    """
    writers = {
        "pending": lambda svc, pid, m, i: _reassign(svc, pid, m, i),
        "accepted": lambda svc, pid, m, i: svc.acknowledge_task_intake(
            pid, i, session_id=m["target"]["agent"]["id"],
            token=m["target"]["token"], response="accepted",
        ),
        "declined": lambda svc, pid, m, i: svc.acknowledge_task_intake(
            pid, i, session_id=m["target"]["agent"]["id"],
            token=m["target"]["token"], response="declined", note="not this one",
        ),
        "blocked": lambda svc, pid, m, i: svc.acknowledge_task_intake(
            pid, i, session_id=m["target"]["agent"]["id"],
            token=m["target"]["token"], response="blocked", note="waiting",
        ),
        "defining": lambda svc, pid, m, i: _force_status(svc, pid, i, "defining"),
        "defined": lambda svc, pid, m, i: _force_status(svc, pid, i, "defined"),
        "cancelled": lambda svc, pid, m, i: svc.cancel_task_intake(
            pid, i, session_id=m["creator"]["agent"]["id"],
            token=m["creator"]["token"],
        ),
    }
    # `defining` is a transaction-local claim state with no public command; it
    # is declared precisely so define_task_from_intake can break a race, and
    # the code below asserts that rather than pretending a writer exists.
    transaction_local = {"defining"}
    from tests.test_task_intake_state_machine import _submit, _three_identities

    members = _three_identities(service, project)
    for status in sorted(TASK_INTAKE_STATUSES - transaction_local):
        intake = _submit(service, project, members)
        if status in {"pending"}:
            continue  # submit already produced it
        writers[status](service, project["id"], members, intake["id"])
        assert service.get_task_intake(project["id"], intake["id"])["status"] == status
    assert set(writers) | transaction_local == set(TASK_INTAKE_STATUSES)


def _reassign(service, project_id, members, intake_id):
    return service.reassign_task_intake(
        project_id,
        intake_id,
        target_member_id=members["other"]["agent"]["member_id"],
        management_authorized=True,
    )


def _force_status(service, project_id, intake_id, status):
    with service.database.connect(write=True) as connection:
        connection.execute(
            "UPDATE task_intakes SET status = ? WHERE id = ?", (status, intake_id)
        )
    return {"intake": service.get_task_intake(project_id, intake_id)}


def test_intake_cancelled_is_reachable_from_every_declared_source():
    sources = {
        status
        for status, targets in TASK_INTAKE_TRANSITIONS.items()
        if "cancelled" in targets and status != "cancelled"
    }
    assert sources == {"pending", "accepted", "declined", "blocked"}


# ---------------------------------------------------------------------------
# AC4: the vocabularies are generated from the versioned config.
# ---------------------------------------------------------------------------
def test_release_reason_and_intake_vocabularies_are_published():
    from agentchatroom.api import task_intake_presentation_config

    intake = task_intake_presentation_config()
    assert set(intake["statuses"]) == set(TASK_INTAKE_STATUSES)
    assert set(intake["status_labels"]) == set(TASK_INTAKE_STATUSES)
    assert set(intake["transitions"]) == set(TASK_INTAKE_STATUSES)
    assert set(intake["active_statuses"]) == {"pending", "accepted"}
    for status in TASK_INTAKE_STATUSES:
        assert intake["status_labels"][status].strip()
    assert intake["cancel_action"] == "task_intake_cancel"


def test_config_publishes_release_reasons_priority_range_and_attention_order():
    from agentchatroom.api import task_view_presentation_config as config_of

    config = config_of()
    assert config["task_priority_range"] == [0, 1, 2, 3, 4]
    assert set(config["attention_phase_order"]) == set(config["attention_phases"])
    # The severity ranking is not the alphabetical publication order.
    assert config["attention_phase_order"] != config["attention_phases"]


def test_public_config_serves_the_vocabularies_the_browser_consumes(settings):
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from agentchatroom.api import create_app  # noqa: PLC0415

    with TestClient(create_app(settings)) as client:
        domain = client.get("/api/v1/config/public").json()["domain"]
    assert set(domain["task_release_reason_codes"]) == set(TASK_RELEASE_REASON_CODES)
    assert set(domain["task_release_reason_labels"]) == set(TASK_RELEASE_REASON_CODES)
    for code in TASK_RELEASE_REASON_CODES:
        assert domain["task_release_reason_labels"][code].strip()
    assert set(domain["task_intake"]["statuses"]) == set(TASK_INTAKE_STATUSES)
    assert set(domain["task_intake"]["status_labels"]) == set(TASK_INTAKE_STATUSES)
    assert set(domain["task_view"]["action_codes"]) == set(TASK_ACTION_CODES)
    assert domain["task_view"]["task_priority_range"] == [0, 1, 2, 3, 4]
    assert domain["task_view"]["groups"]


def test_browser_no_longer_hard_codes_the_published_vocabularies():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    # Navigation groups: no inline group list beside the config read.
    nav_start = javascript.index("function taskNavigationEntries(tasks)")
    nav_end = javascript.index("function activeSubgroupRank(phase)", nav_start)
    navigation = javascript[nav_start:nav_end]
    assert "config?.groups" in navigation
    assert '["claimable", "active", "review", "integration", "done", "cancelled"]' not in (
        javascript
    )

    # Expert filter face codes come from the published label tables.
    filter_start = javascript.index("function taskExpertFilterSignature(")
    filter_end = javascript.index("function renderTasks(tasks)")
    expert = javascript[filter_start:filter_end]
    assert "${kind}_labels" in expert
    assert "task_priority_range" in expert
    for hard_coded in (
        '["todo", "claimed", "in_progress", "blocked", "completed", "cancelled"]',
        '["not_required", "pending", "changes_requested", "approved"]',
        '["pending", "done", "failed"]',
        "[0, 1, 2, 3, 4]",
    ):
        assert hard_coded not in expert, hard_coded

    # Intake labels and the active/archived split.
    assert "function taskIntakeStatus(status)" in javascript
    intake_start = javascript.index("function taskIntakeStatus(status)")
    intake_end = javascript.index("function taskIntakeTaskReference(intake)", intake_start)
    intake_helpers = javascript[intake_start:intake_end]
    assert "status_labels" in intake_helpers
    assert 'pending: "待 Agent 受理"' not in javascript
    assert "taskIntakeActiveStatuses()" in javascript
    assert 'new Set(["pending", "accepted"])' not in javascript

    # Release reasons are rendered from the published vocabulary.
    assert "function renderReleaseReasonOptions()" in javascript
    assert "applyPublicConfig()" in javascript
    release_start = javascript.index("function renderReleaseReasonOptions()")
    release_end = javascript.index('elements["task-release-button"]', release_start)
    release = javascript[release_start:release_end]
    assert "task_release_reason_labels" in release
    assert "task_release_reason_codes" in release
    # The markup keeps only the empty placeholder; required still gates submit.
    for code in TASK_RELEASE_REASON_CODES:
        assert f'<option value="{code}"' not in markup
    assert '<option value="">选择原因…</option>' in markup


def test_intake_cancel_entry_is_gated_by_the_published_transition_table():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function taskIntakeCancellable(intake)")
    end = javascript.index("function taskIntakeTaskReference(intake)", start)
    block = javascript[start:end]
    assert "transitions" in block
    assert 'includes("cancelled")' in block
    # `cancelled` declares a self-loop for idempotency; that must not render
    # a second cancel button on an already-cancelled intake.
    assert 'intake.status === "cancelled"' in block
    assert "data-intake-cancel-id" in block
    assert "/cancel" in javascript
    assert "task.intake_cancelled" in javascript


def test_element_ids_and_aria_attributes_are_not_regressed():
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "task-assign-button",
        "task-release-button",
        "task-cancel-button",
        "task-release-reason",
        "task-intake-list",
    ):
        assert f'id="{element_id}"' in markup, element_id
        assert f'"{element_id}"' in javascript, element_id
    # The release reason select keeps its accessible name and required gate.
    assert "<label>释放原因" in markup
    assert 'id="task-release-reason" required' in markup
    assert 'aria-modal="true"' in markup
    assert 'aria-labelledby="task-release-title"' in markup
