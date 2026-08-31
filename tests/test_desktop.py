from __future__ import annotations

import json
import subprocess

import pytest

from agentchatroom import desktop


def test_pick_directory_returns_a_resolved_existing_folder(monkeypatch, tmp_path):
    initial = tmp_path / "initial"
    selected = tmp_path / "selected"
    initial.mkdir()
    selected.mkdir()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"path": str(selected)}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    assert desktop.pick_directory(str(initial)) == str(selected.resolve())
    assert calls[0][0][-1] == str(initial.resolve())
    assert "shell" not in calls[0][1]
    assert calls[0][1]["check"] is False


def test_pick_directory_returns_none_when_the_user_cancels(monkeypatch):
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"path": ""}) + "\n",
            stderr="",
        ),
    )

    assert desktop.pick_directory() is None


def test_pick_directory_reports_an_unavailable_desktop(monkeypatch):
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            2,
            stdout=json.dumps({"error": "folder_picker_unavailable"}) + "\n",
            stderr="",
        ),
    )

    with pytest.raises(desktop.DirectoryPickerUnavailable):
        desktop.pick_directory()
