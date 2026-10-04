"""#294 guard: a restore must report the checkout writes it performed.

Healing (rewriting ``AGENTS.md`` and ``.agentchatroom/project.json``) ran
unconditionally *before* the restore decision, but the restore branch returned a
hard-coded payload holding a single ``runtime_session_restored`` notice. So a
re-bootstrap after a compression or restart wrote to the user's checkout and
reported nothing at all.

These tests pin the fixed behaviour: every write performed on the checkout is
accompanied by an observable notice, including on the restore path.
"""

from __future__ import annotations

from agentchatroom.bootstrap import bootstrap_local_room
from agentchatroom.project_registration import (
    project_registration_path,
    register_checkout_project,
)


def _boot(service, **extra):
    return bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        **extra,
    )


def _notice_codes(outcome):
    return [notice["code"] for notice in outcome.public.get("notices") or []]


def test_restored_bootstrap_reports_the_instructions_write(service, tmp_path):
    """AC1/AC3: an AGENTS.md rewrite during a restore is not silent."""
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Restore")
    register_checkout_project(project_dir, project)

    first = _boot(service, workspace_roots=[project_dir], cwd=project_dir)
    assert first.binding is not None

    # Make the coordination block stale so the next bootstrap must rewrite it.
    instructions = project_dir / "AGENTS.md"
    original = instructions.read_text(encoding="utf-8")
    instructions.write_text(
        original.replace("room_sync", "legacy-sync-marker"), encoding="utf-8"
    )

    restored = _boot(
        service,
        workspace_roots=[project_dir],
        cwd=project_dir,
        restore_binding=first.binding,
    )

    codes = _notice_codes(restored)
    assert restored.public["connection"]["room_session"] == "restored"
    assert "runtime_session_restored" in codes
    # The write really happened, so it has to be visible.
    assert "project_instructions_updated" in codes
    assert codes != ["runtime_session_restored"]
    assert "legacy-sync-marker" not in instructions.read_text(encoding="utf-8")


def test_registration_rewrite_on_restore_is_reported(service, tmp_path):
    """AC2: a project.json rewrite during a restore carries a notice."""
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Rewrite")
    register_checkout_project(project_dir, project)
    service.register_workspace(
        project["id"],
        host_key="host:rewrite",
        host_name="Rewrite",
        local_path=str(project_dir),
    )
    registration = project_registration_path(project_dir)

    first = _boot(
        service, workspace_roots=[project_dir], cwd=project_dir, database_first=True
    )
    assert first.binding is not None

    # The registration document goes missing (compression, a partial checkout,
    # an older client). Healing rewrites it, so the write must be reported.
    registration.unlink()

    restored = _boot(
        service,
        workspace_roots=[project_dir],
        cwd=project_dir,
        database_first=True,
        restore_binding=first.binding,
    )

    assert restored.public["connection"]["room_session"] == "restored"
    assert registration.exists(), "healing did not rewrite the registration"
    assert "project_registration_refreshed" in _notice_codes(restored)


def test_fresh_bootstrap_reports_the_instructions_write(service, tmp_path):
    """AC2: the same obligation applies when no restore happens."""
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Fresh")
    register_checkout_project(project_dir, project)
    instructions = project_dir / "AGENTS.md"
    instructions.write_text("stale\n", encoding="utf-8")

    outcome = _boot(service, workspace_roots=[project_dir], cwd=project_dir)

    assert outcome.public["status"] == "ready"
    # No coordination block existed, so the action is "appended" rather than
    # "updated"; either way the write is announced.
    codes = _notice_codes(outcome)
    assert any(code.startswith("project_instructions_") for code in codes), codes
    assert "room_sync" in instructions.read_text(encoding="utf-8")
