"""#290 guard: the server must not resolve() a client-declared workspace root.
# audit-allow-file:windows_absolute_path

Mode B/C let a client advertise workspace roots that the server cannot see. The
server used to run ``Path(raw).expanduser().resolve()`` on that declaration,
which on a POSIX server turns ``file:///D:/claw/proj`` into a path under the
server's own working directory, and a relative form into ``<server cwd>/...``.
The fabricated value was then stored as a workspace ``local_path``, so the first
binding after a compression/restart permanently failed to match
(``project_not_registered``) or registered a server-side artefact path.

These tests pin the fixed behaviour. The implementation is expected to keep the
client's raw string for matching and take the workspace root from the database
row instead.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agentchatroom.bootstrap import bootstrap_local_room, match_registered_projects
from agentchatroom.project_registration import (
    project_registration_path,
    register_checkout_project,
)

# A root that exists on a Windows client and nowhere near this test host.
DECLARED = r"z:/missingroom/proj"


def _boot(service, **extra):
    return bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        **extra,
    )


def test_matched_checkout_comes_from_the_database_row(service, tmp_path):
    """AC1: an invisible declared root must not become the workspace root."""
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Remote")
    service.register_workspace(
        project["id"],
        host_key="host:remote",
        host_name="Remote",
        local_path=r"Z:\MissingRoom\Proj",
    )

    matches = match_registered_projects(service, [DECLARED])

    assert len(matches) == 1
    matched_project, checkout = matches[0]
    assert matched_project["id"] == project["id"]
    # The declaration is invisible here, so the database row is the only value
    # the server can vouch for.
    assert checkout == Path(str(project["root_path"])).expanduser().resolve()


def test_bootstrap_stores_no_server_side_artefact_path(service, tmp_path):
    """AC2: a cloud binding must not write a spliced local_path."""
    project_dir = tmp_path / "room"
    other_dir = tmp_path / "other"
    project_dir.mkdir()
    other_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Remote")
    other = service.create_project(root_path=str(other_dir), name="Other")
    for target, local in (
        (project, r"Z:\MissingRoom\Proj"),
        (other, r"Z:\OtherRoom\Proj"),
    ):
        service.register_workspace(
            target["id"],
            host_key=f"host:{target['id']}",
            host_name="Remote",
            local_path=local,
        )

    outcome = _boot(
        service, workspace_roots=[DECLARED], database_first=True
    )

    assert outcome.public["status"] == "ready"
    assert outcome.public["project"]["id"] == project["id"]
    recorded = [item["local_path"] for item in service.list_workspaces(project["id"])]
    # The Project's own root gets registered, because that is the only path the
    # server can vouch for.
    assert any(Path(item) == project_dir.resolve() for item in recorded)
    # The client's declaration stays the row it already was, and no second row
    # appears holding a path fabricated by resolving it against the server cwd.
    assert len(recorded) == 2
    assert not any(
        "missingroom" in item.casefold() and str(Path.cwd()).casefold() in item.casefold()
        for item in recorded
    )


def test_invisible_checkout_skips_filesystem_healing(service, tmp_path):
    """AC3: healing must not fabricate a checkout the server cannot see."""
    project_dir = tmp_path / "room"
    project_dir.mkdir()
    project = service.create_project(root_path=str(project_dir), name="Remote")
    register_checkout_project(project_dir, project)
    service.register_workspace(
        project["id"],
        host_key="host:remote",
        host_name="Remote",
        local_path=r"Z:\MissingRoom\Proj",
    )
    registration = project_registration_path(project_dir)

    # The checkout disappears from this host (unplugged drive, deleted dir).
    shutil.rmtree(project_dir)

    outcome = _boot(
        service, workspace_roots=[DECLARED], database_first=True
    )

    assert outcome.public["status"] == "ready", (
        "a checkout that is invisible to the server must not block the binding"
    )
    assert not project_dir.exists(), "healing recreated an invisible checkout"
    assert not registration.exists()
