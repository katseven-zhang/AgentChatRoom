from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentchatroom import desktop


@pytest.mark.parametrize("frozen", [False, True])
def test_pick_directory_returns_a_resolved_existing_folder(monkeypatch, tmp_path, frozen):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
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
    expected = [sys.executable, "pick-directory"] if frozen else [
        sys.executable, "-m", "agentchatroom.desktop"
    ]
    assert calls[0][0][:-1] == expected
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


@pytest.mark.parametrize("output", ["", "{}", "[]", "null", '{"path": null}', "not json"])
def test_pick_directory_rejects_missing_or_invalid_protocol(monkeypatch, output):
    monkeypatch.setattr(desktop.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 0, stdout=output, stderr=""))
    with pytest.raises(desktop.DirectoryPickerUnavailable):
        desktop.pick_directory()


@pytest.mark.parametrize("outcome", ["selected", "cancelled", "failed"])
def test_picker_helper_emits_protocol_and_destroys_root(monkeypatch, tmp_path, capsys, outcome):
    calls = []
    root = SimpleNamespace(
        withdraw=lambda: None,
        attributes=lambda *args: None,
        update_idletasks=lambda: None,
        destroy=lambda: calls.append("destroyed"),
    )

    def askdirectory(**kwargs):
        assert kwargs["parent"] is root
        assert kwargs["initialdir"] == str(tmp_path.resolve())
        assert kwargs["mustexist"] is True
        if outcome == "failed":
            raise RuntimeError("private desktop error")
        return str(tmp_path) if outcome == "selected" else ""

    monkeypatch.setitem(sys.modules, "tkinter", SimpleNamespace(
        Tk=lambda: root, TclError=RuntimeError,
        filedialog=SimpleNamespace(askdirectory=askdirectory),
    ))
    assert desktop.run_directory_picker(str(tmp_path)) == (2 if outcome == "failed" else 0)
    payload = json.loads(capsys.readouterr().out)
    expected = {"error": "folder_picker_unavailable"} if outcome == "failed" else {
        "path": str(tmp_path) if outcome == "selected" else ""
    }
    assert payload == expected
    assert calls == ["destroyed"]


@pytest.mark.parametrize("exit_code", [0, 2])
def test_packaged_entry_routes_picker_after_restoring_streams(monkeypatch, tmp_path, exit_code):
    from agentchatroom import stdio_runtime

    entry_path = Path(__file__).resolve().parents[1] / "packaging" / "entry_app.py"
    spec = importlib.util.spec_from_file_location("picker_entry_under_test", entry_path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    calls = []
    monkeypatch.setattr(stdio_runtime, "prepare_standard_streams", lambda: calls.append("streams"))

    def picker(initial_path):
        assert calls == ["streams"]
        calls.append(initial_path)
        return exit_code

    monkeypatch.setattr(desktop, "run_directory_picker", picker)
    monkeypatch.setattr(sys, "argv", ["agentchatroom.exe", "pick-directory", str(tmp_path)])
    with pytest.raises(SystemExit) as raised:
        entry.main()
    assert raised.value.code == exit_code
    assert calls == ["streams", str(tmp_path)]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows packaged dialog smoke test")
@pytest.mark.parametrize("confirm", [False, True])
def test_packaged_picker_opens_native_dialog_and_returns_result(tmp_path, confirm):
    executable = os.environ.get("AGENTCHATROOM_TEST_PICKER_EXE")
    if not executable:
        pytest.skip("Set AGENTCHATROOM_TEST_PICKER_EXE to test a built EXE")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    process = subprocess.Popen(
        [executable, "pick-directory", str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    dialogs = []

    @callback_type
    def find_dialog(window, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == process.pid and user32.IsWindowVisible(window):
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(window, title, len(title))
            if title.value == "选择项目文件夹":
                dialogs.append(window)
        return True

    try:
        deadline = time.monotonic() + 15
        while not dialogs and process.poll() is None and time.monotonic() < deadline:
            user32.EnumWindows(find_dialog, 0)
            time.sleep(0.05)
        assert dialogs, "Packaged helper did not open the native directory dialog"
        # WM_COMMAND/IDOK selects the initial folder; WM_CLOSE cancels this helper only.
        assert user32.PostMessageW(dialogs[0], 0x0111 if confirm else 0x0010, 1 if confirm else 0, 0)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        payload = json.loads(stdout)
        if confirm:
            assert Path(payload["path"]).resolve() == tmp_path.resolve()
        else:
            assert payload == {"path": ""}
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
