"""Saving config.toml must not be able to destroy config.toml.

The admin endpoint and the GUI both rewrote the file in place, so an interrupt
during the write left a truncated file the service could no longer start from,
and two writers could drop one another's section.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from agentchatroom import gui
from agentchatroom.api import create_app, upsert_toml_section
from agentchatroom.config import atomic_write_text, rewrite_config_text
from agentchatroom.errors import DomainError

STARTER = '[server]\nport = 8765\n\n[backup]\nauto_backup_enabled = true\n'


def _litter(path: Path) -> list[str]:
    return sorted(item.name for item in path.parent.iterdir() if ".tmp" in item.name)


def test_a_failed_replace_leaves_the_previous_file_intact(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(STARTER, encoding="utf-8")

    def explode(*_args):
        raise OSError("interrupted")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        atomic_write_text(target, "[server]\nport = 1\n")
    monkey.undo()

    assert target.read_text(encoding="utf-8") == STARTER
    assert _litter(tmp_path) == []


def test_a_failed_flush_leaves_no_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text(STARTER, encoding="utf-8")
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError):
        atomic_write_text(target, "[server]\nport = 1\n")

    assert target.read_text(encoding="utf-8") == STARTER
    assert _litter(tmp_path) == []


def test_rewrite_refuses_to_persist_text_it_cannot_read_back(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(STARTER, encoding="utf-8")

    with pytest.raises(DomainError) as captured:
        rewrite_config_text(target, lambda current: current + "\n[[broken\n")

    assert captured.value.code == "config_file_unparsable"
    assert captured.value.details["required_action"]
    assert target.read_text(encoding="utf-8") == STARTER


def test_rewrite_hands_the_builder_the_current_text(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(STARTER, encoding="utf-8")

    rewrite_config_text(
        target, lambda current: upsert_toml_section(current, "gui", {"theme": "dark"})
    )

    saved = target.read_text(encoding="utf-8")
    assert '[gui]\ntheme = "dark"' in saved
    assert "port = 8765" in saved


def test_concurrent_section_writers_do_not_lose_each_other(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(STARTER, encoding="utf-8")
    failures: list[BaseException] = []

    def write(section: str, key: str) -> None:
        try:
            for _ in range(8):
                rewrite_config_text(
                    target,
                    lambda current, s=section, k=key: upsert_toml_section(
                        current, s, {k: s}
                    ),
                )
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    threads = [
        threading.Thread(target=write, args=(f"sec{i}", f"key{i}")) for i in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    saved = target.read_text(encoding="utf-8")
    for i in range(4):
        assert f"[sec{i}]" in saved, saved
    assert _litter(tmp_path) == []


def test_gui_port_save_is_atomic_and_keeps_other_sections(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text(STARTER, encoding="utf-8")

    gui.update_config_values(target, port=9000)

    saved = target.read_text(encoding="utf-8")
    assert "port = 9000" in saved
    assert "[backup]" in saved
    assert _litter(tmp_path) == []

    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        gui.update_config_values(target, port=9100)
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == saved


def test_gui_reports_a_structured_error_for_a_corrupt_config(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("[server\nport = ", encoding="utf-8")

    with pytest.raises(DomainError) as captured:
        gui.update_config_values(target, port=9000)

    assert captured.value.code == "config_file_unparsable"
    assert captured.value.details["required_action"]


def test_admin_backup_settings_write_through_the_shared_path(tmp_path):
    from fastapi.testclient import TestClient

    from agentchatroom.config import Settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = data_dir / "config.toml"
    config.write_text(STARTER, encoding="utf-8")

    with TestClient(create_app(Settings(data_dir=data_dir))) as client:
        response = client.put(
            "/api/v1/admin/backup-settings",
            json={"auto_backup_interval_seconds": 120, "auto_backup_max_kept": 3},
        )

    assert response.status_code == 200, response.text
    saved = config.read_text(encoding="utf-8")
    assert "auto_backup_interval_seconds = 120" in saved
    assert "auto_backup_max_kept = 3" in saved
    assert "port = 8765" in saved
    assert _litter(data_dir) == []
