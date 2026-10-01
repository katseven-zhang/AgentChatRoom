"""Task #260: a failed join must not rewrite an archived Room's identity.

``resolve_project_for_join`` had two self-heal selectors with different rules:
the checkout-registration branch matched on ``project_key`` and root path only, so
an archived Room was healed (``UPDATE projects`` plus a ``project.updated``
event) and only *then* rejected with ``project_archived``. The same branch caught
only ``OSError`` while the other caught nothing, so a stored root path that could
not be resolved surfaced as a 500.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentchatroom.errors import DomainError


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
    )


def _events_for(service, project_id, event_type):
    with service.database.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) AS total FROM events WHERE project_id = ? AND event_type = ?",
            (project_id, event_type),
        ).fetchone()["total"]


def _row(service, project_id):
    with service.database.connect() as connection:
        return connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()


def test_join_heal_never_touches_an_archived_room(service, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    project = service.create_project(
        root_path=str(repository), name="Archived Room"
    )
    service.archive_project(project["id"])
    _git(repository, "remote", "add", "origin", "https://example.invalid/team/repo.git")

    with pytest.raises(DomainError) as error:
        service.resolve_project_for_join(
            root_path=str(repository),
            registered_project_key=project["project_key"],
        )

    assert error.value.code == "project_archived"
    stored = _row(service, project["id"])
    assert str(stored["git_remote"] or "") == ""
    assert str(stored["logical_path"] or "") == ""
    assert _events_for(service, project["id"], "project.updated") == 0


def test_join_heal_still_upgrades_an_active_local_path_room(service, tmp_path):
    repository = tmp_path / "active-repository"
    repository.mkdir()
    _git(repository, "init")
    project = service.create_project(
        root_path=str(repository), name="Active Room"
    )
    _git(repository, "remote", "add", "origin", "https://example.invalid/team/active.git")

    resolved = service.resolve_project_for_join(
        root_path=str(repository),
        registered_project_key=project["project_key"],
    )

    assert resolved["git_remote"] == "https://example.invalid/team/active.git"
    assert _events_for(service, project["id"], "project.updated") == 1


def test_an_unreadable_stored_root_cannot_turn_a_join_into_a_500(
    service, tmp_path
):
    repository = tmp_path / "clean-repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "remote", "add", "origin", "https://example.invalid/team/clean.git")
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id, project_key, name, root_path, git_remote, logical_path,
                settings_json, created_at, updated_at, archived_at
            ) VALUES ('project_unreadable', 'prj_unreadable', 'Unreadable',
                      ?, '', '', '{}', ?, ?, NULL)
            """,
            ("bad\x00path", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )

    # The point is the failure mode: a stored root_path that cannot be resolved
    # must never escape as ValueError (a 500) and must never be healed into the
    # joining repository's scope. Either outcome below is a structured refusal or
    # a clean no-match resolution; ValueError would fail the test by escaping.
    try:
        service.resolve_project_for_join(root_path=str(repository))
    except DomainError:
        pass

    stored = _row(service, "project_unreadable")
    assert str(stored["git_remote"] or "") == ""
    assert _events_for(service, "project_unreadable", "project.updated") == 0


def test_heal_requires_the_derived_logical_path_to_match_too(
    service, tmp_path
):
    repository = tmp_path / "mono"
    (repository / "packages" / "api").mkdir(parents=True)
    _git(repository, "init")
    with service.database.connect(write=True) as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id, project_key, name, root_path, git_remote, logical_path,
                settings_json, created_at, updated_at, archived_at
            ) VALUES ('project_stale_scope', 'prj_stale_scope', 'Stale Scope',
                      ?, '', 'packages/api', '{}', ?, ?, NULL)
            """,
            (
                str(repository),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
    _git(repository, "remote", "add", "origin", "https://example.invalid/team/mono.git")

    with pytest.raises(DomainError):
        service.resolve_project_for_join(
            root_path=str(repository),
            registered_project_key="prj_stale_scope",
        )

    stored = _row(service, "project_stale_scope")
    assert str(stored["git_remote"] or "") == ""
    assert _events_for(service, "project_stale_scope", "project.updated") == 0
