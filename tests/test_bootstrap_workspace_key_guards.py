"""#308 guard: workspace path comparison folds separators and case.
# audit-allow-file:windows_absolute_path

``workspace_path_keys`` unconditionally produced three forms of every path
(``raw``, ``raw`` with forward slashes, ``raw`` with backslashes), and
``os.path.normcase`` is a documented no-op off Windows. So a POSIX server
compared a Windows client's root case-sensitively while offering three
spellings of each side: separator variants could match across Projects, while a
mere case difference failed to match at all. The same file already used
``casefold`` for the pin comparison, and ``stored_project_scope`` already
provided a canonical ``(kind, identity, logical_path)`` scope.

These tests pin the fixed behaviour: matching is separator- and
case-insensitive on any host, never crosses Project boundaries, and still fails
closed when a root is genuinely ambiguous.

The assertions go through ``match_registered_projects`` rather than any
normalisation helper, so they hold regardless of how the comparison is
implemented internally.
"""

from __future__ import annotations

import os

import pytest

from agentchatroom.bootstrap import bootstrap_local_room, match_registered_projects

# One Windows client root, spelled five ways.
EQUIVALENT_DECLARATIONS = [
    r"Z:\SharedRoom\Proj",
    "z:/sharedroom/proj",
    "Z:/SharedRoom/Proj",
    r"z:\SHAREDROOM\PROJ",
    "file:///Z:/SharedRoom/Proj",
]


def _boot(service, **extra):
    return bootstrap_local_room(
        service,
        software_key="boot-agent",
        software_name="Boot Agent",
        client="codex",
        model="unknown",
        **extra,
    )


def _project_with_root(service, tmp_path, name, local_path):
    root = tmp_path / name.lower()
    root.mkdir()
    project = service.create_project(root_path=str(root), name=name)
    service.register_workspace(
        project["id"],
        host_key=f"host:{name.lower()}",
        host_name=name,
        local_path=local_path,
    )
    return project, root


@pytest.mark.parametrize("declared", EQUIVALENT_DECLARATIONS)
def test_equivalent_declarations_match_the_same_project(service, tmp_path, declared):
    project, _root = _project_with_root(
        service, tmp_path, "Shared", r"Z:\SharedRoom\Proj"
    )

    matches = match_registered_projects(service, [declared])

    assert [item[0]["id"] for item in matches] == [project["id"]]


def test_case_difference_matches_on_a_posix_server(monkeypatch, service, tmp_path):
    """On POSIX ``os.path.normcase`` is a no-op, so folding must not rely on it."""
    monkeypatch.setattr(os.path, "normcase", lambda value: value, raising=False)

    project, _root = _project_with_root(service, tmp_path, "Shared", r"d:\claw\proj")

    matches = match_registered_projects(service, ["D:/Claw/Proj"])

    assert [item[0]["id"] for item in matches] == [project["id"]]


def test_normalisation_does_not_bind_another_project(service, tmp_path):
    """Folding must not widen matching across distinct Projects."""
    mine, _mine_root = _project_with_root(
        service, tmp_path, "Mine", r"Z:\SharedRoom\Proj"
    )
    _project_with_root(service, tmp_path, "Other", r"Z:\OtherRoom\Proj")

    matches = match_registered_projects(service, ["z:/sharedroom/proj"])

    assert [item[0]["id"] for item in matches] == [mine["id"]]


def test_equivalent_roots_on_two_projects_stay_ambiguous(service, tmp_path):
    """Ambiguity must still fail closed after normalisation."""
    first, first_root = _project_with_root(
        service, tmp_path, "First", r"Z:\SharedRoom\Proj"
    )
    second, _second_root = _project_with_root(
        service, tmp_path, "Second", r"Z:\SharedRoom\Proj"
    )
    assert first["id"] != second["id"]

    matches = match_registered_projects(service, ["z:/sharedroom/proj"])
    assert len(matches) == 2

    outcome = _boot(
        service,
        workspace_roots=["z:/sharedroom/proj"],
        database_first=True,
    )
    assert outcome.public["status"] in {
        "ambiguous_workspace",
        "project_workspace_mismatch",
    }
    assert outcome.binding is None
    assert first_root.exists()
