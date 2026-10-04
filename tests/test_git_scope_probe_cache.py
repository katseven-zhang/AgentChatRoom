"""Task #253: the git scope probe cache has to cache what it actually probes.

The read gate and the write both required the source to be exactly ``"path"``, so
a local work tree without an origin (source ``git_local``) re-ran up to two git
subprocesses on every ``list_projects`` / ``get_project`` read - and the GUI polls
that twice per second. The TTL was also a hardcoded magic constant, which the
project rules forbid.
"""

from __future__ import annotations

import time

import pytest

from agentchatroom import services as services_module
from agentchatroom.config import load_settings


class _Probe:
    def __init__(self, remote: str, is_work_tree: bool) -> None:
        self.remote = remote
        self.is_work_tree = is_work_tree
        self.calls = 0

    def __call__(self, root):
        self.calls += 1
        return self.remote, root, self.is_work_tree


@pytest.fixture()
def counted_probe(monkeypatch):
    def _install(remote="", is_work_tree=True):
        probe = _Probe(remote, is_work_tree)
        monkeypatch.setattr(services_module, "_project_git_info", probe)
        return probe

    return _install


def test_local_work_tree_is_cached_so_repeated_reads_probe_once(
    service, project, counted_probe
):
    probe = counted_probe()

    first = service.get_project(project["id"])
    second = service.get_project(project["id"])
    listed = service.list_projects()

    assert first["project_source"] == "git_local"
    assert second["project_source"] == "git_local"
    assert [item["id"] for item in listed] == [project["id"]]
    assert probe.calls == 1, probe.calls


def test_cached_source_survives_a_list_read_and_a_detail_read(
    service, project, counted_probe
):
    probe = counted_probe()
    service.list_projects()
    service.get_project(project["id"])
    service.list_projects()

    assert probe.calls == 1


def test_ttl_expiry_forces_a_fresh_probe(service, project, counted_probe, monkeypatch):
    probe = counted_probe()
    monkeypatch.setattr(
        service, "git_scope_recheck_ttl_seconds", lambda: 0.05, raising=False
    )

    service.get_project(project["id"])
    time.sleep(0.08)
    service.get_project(project["id"])

    assert probe.calls == 2


def test_invalidate_forces_an_immediate_reprobe(
    service, project, counted_probe
):
    probe = counted_probe()
    service.get_project(project["id"])
    service.get_project(project["id"])
    assert probe.calls == 1

    service.invalidate_git_scope_probe(project["id"])
    service.get_project(project["id"])

    assert probe.calls == 2


def test_an_origin_on_disk_is_not_hidden_by_the_display_cache(
    service, project, counted_probe
):
    probe = counted_probe(remote="", is_work_tree=False)
    service.get_project(project["id"])
    assert probe.calls == 1

    service.invalidate_git_scope_probe(project["id"])
    probe.remote = "https://example.invalid/team/repo.git"
    probe.is_work_tree = True
    resolved = service.get_project(project["id"])

    assert resolved["git_remote"] == "https://example.invalid/team/repo.git"


def test_ttl_is_a_validated_setting_with_documented_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_GIT_SCOPE_RECHECK_TTL_SECONDS", "12.5")
    assert load_settings(data_dir=tmp_path).git_scope_recheck_ttl_seconds == 12.5

    monkeypatch.setenv("AGENTCHATROOM_GIT_SCOPE_RECHECK_TTL_SECONDS", "0")
    with pytest.raises(ValueError, match="git scope recheck ttl"):
        load_settings(data_dir=tmp_path)

    monkeypatch.setenv("AGENTCHATROOM_GIT_SCOPE_RECHECK_TTL_SECONDS", "99999")
    with pytest.raises(ValueError, match="git scope recheck ttl"):
        load_settings(data_dir=tmp_path)
