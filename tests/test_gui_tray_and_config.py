"""GUI tray registration order (#314), bounded server-log polling (#315), in-place
config patching (#322).

Each test here pins a real ordering or a real byte budget rather than a
handy fake: the previous fakes made all three defects invisible.
"""

from __future__ import annotations

import os
import sys
import threading
import tomllib
import types
from pathlib import Path
from queue import Queue

import pytest

from agentchatroom import gui as gui_module
from agentchatroom.errors import DomainError
from agentchatroom.gui import (
    RUNNING_URL_TAIL_BYTES,
    RUNNING_URL_TAIL_LINES,
    create_tray_icon,
    run_start_worker,
    running_url_from_log,
    stop_tray_icon,
    tray_degraded_hint,
    update_config_values,
)
from tests.conftest import boot_gui_or_skip, require_tk_runtime

CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
STARTED_URL = "http://127.0.0.1:8765"


# --------------------------------------------------------------------------------------
# shared fakes
# --------------------------------------------------------------------------------------
class _Guard:
    def __init__(self, *, window_title: str) -> None:
        assert window_title

    def acquire(self) -> bool:
        return True

    def activation_requested(self) -> bool:
        return False

    def release(self) -> None:
        pass


class _FakeThread:
    """Stand-in for LogTail / RoomEventTail so no real reader thread starts."""

    def __init__(self, _source, _sink, stop_event) -> None:
        self.stop_event = stop_event
        self.cursors: dict = {}
        self.presence: dict = {}

    def start(self) -> None:
        pass


class Win32OrderedIcon:
    """pystray._win32.Icon ordering, faithfully.

    ``run_detached`` starts the loop thread, that thread creates ``_hwnd`` and
    only then runs the ready setup, and showing the icon (NIM_ADD) requires
    ``_hwnd``. Setting ``visible`` from the caller's thread before the handle
    exists therefore fails exactly like the real Shell_NotifyIcon call against
    a NULL window handle, and pystray's ``if self._visible == value: return``
    early exit makes the later ready-time show a no-op. Both shapes are
    reproduced here so the race is observable and the fix is provable.
    """

    def __init__(self, name, image, title, menu, *, behaviour="ready") -> None:
        self.menu = menu
        self.behaviour = behaviour
        self.events: list[str] = []
        self.stop_calls = 0
        self.setup_callback = None
        self._hwnd = None
        self._visible = False

    @property
    def visible(self) -> bool:
        return self._visible

    @visible.setter
    def visible(self, value: bool) -> None:
        if self._visible == value:
            self.events.append("visible_noop")
            return
        if not value:
            self._visible = False
            return
        if self._hwnd is None:
            # Real backend: NIM_ADD is issued against a missing window handle.
            self.events.append("nim_add_without_hwnd")
            raise RuntimeError("Shell_NotifyIcon: no notify window handle yet")
        if self.behaviour == "refused":
            self.events.append("nim_add_refused")
            raise OSError("Shell_NotifyIconW returned FALSE")
        self.events.append("nim_add")
        self._visible = True

    def run_detached(self, setup=None) -> None:
        self.setup_callback = setup
        self.events.append("run_detached")
        if self.behaviour == "never_ready":
            # The loop thread exists but never reaches ``_mark_ready``.
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self._hwnd = object()
        self.events.append("hwnd_created")
        setup = self.setup_callback or (lambda icon: setattr(icon, "visible", True))
        setup(self)

    def stop(self) -> None:
        self.stop_calls += 1
        self.events.append("stop")


def fake_pystray(behaviour: str = "ready") -> tuple[types.SimpleNamespace, list[Win32OrderedIcon]]:
    """Return a pystray-shaped module whose ``Icon`` follows the win32 order."""
    created: list[Win32OrderedIcon] = []

    def icon_class(name, image, title, menu):
        icon = Win32OrderedIcon(name, image, title, menu, behaviour=behaviour)
        created.append(icon)
        return icon

    module = types.SimpleNamespace(
        Menu=lambda *items: items,
        MenuItem=lambda label, callback, *, default=False: (label, callback, default),
        Icon=icon_class,
    )
    return module, created


def install_fake_pystray(monkeypatch, behaviour: str = "ready"):
    module, created = fake_pystray(behaviour)
    monkeypatch.setitem(sys.modules, "pystray", module)
    monkeypatch.setattr(gui_module, "tray_icon_image", lambda: object())
    return created


def open_window(monkeypatch, settings):
    """Boot the real controller window with the reader threads faked out."""
    tk = require_tk_runtime()

    windows: list = []
    monkeypatch.setattr(gui_module, "load_settings", lambda _config: settings)
    monkeypatch.setattr(gui_module, "LogTail", _FakeThread)
    monkeypatch.setattr(gui_module, "RoomEventTail", _FakeThread)
    monkeypatch.setattr(gui_module, "GuiInstanceGuard", _Guard)
    monkeypatch.setattr(
        gui_module, "create_tray_icon", lambda _handler, **_kwargs: None
    )
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: windows.append(self))
    boot_gui_or_skip(gui_module, tk)
    return windows[0]


def claim_running_service(monkeypatch, settings, *, url_line: str | None = None) -> int:
    """Make the controller see a live service without starting one."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    (settings.data_dir / "server.pid").write_text(str(pid), encoding="ascii")
    if url_line is not None:
        (settings.data_dir / "server.log").write_text(url_line, encoding="utf-8")
    return pid


def forbid_whole_log_reads(monkeypatch, log_path: Path) -> None:
    real_read_text = Path.read_text

    def guarded(self, *args, **kwargs):
        if self == log_path:
            raise AssertionError("the GUI must not read the whole server log")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)


# --------------------------------------------------------------------------------------
# #314 tray icon registration order
# --------------------------------------------------------------------------------------
def test_tray_icon_is_registered_only_after_the_backend_window_handle(monkeypatch) -> None:
    created = install_fake_pystray(monkeypatch)

    icon = create_tray_icon(lambda action: None, ready_timeout=5)

    assert icon is not None
    assert icon is created[0]
    # NIM_ADD never precedes _hwnd, and the caller no longer pre-sets visible.
    assert icon.events == ["run_detached", "hwnd_created", "nim_add"]
    assert icon.visible is True
    assert icon.stop_calls == 0
    # A custom ready callback was passed, so pystray's own show-up cannot be
    # short-circuited by its ``_visible == value`` early exit.
    assert callable(icon.setup_callback)


def test_tray_icon_degrades_when_the_shell_refuses_the_icon(monkeypatch) -> None:
    created = install_fake_pystray(monkeypatch, behaviour="refused")
    notes: list[str] = []

    assert create_tray_icon(
        lambda action: None, ready_timeout=1, note=notes.append
    ) is None
    icon = created[0]
    # The handle existed, so this is a real registration failure, not the race.
    assert icon.events == ["run_detached", "hwnd_created", "nim_add_refused", "stop"]
    assert icon.visible is False
    # The loop thread is not left running behind an invisible icon.
    assert icon.stop_calls == 1
    assert notes and "不会收起到系统托盘" in notes[0]
    # Diagnostics stay readable on this surface: no redaction was added.
    assert "Shell_NotifyIconW returned FALSE" in notes[0]


def test_tray_icon_degrades_and_stops_when_ready_never_arrives(monkeypatch) -> None:
    created = install_fake_pystray(monkeypatch, behaviour="never_ready")
    notes: list[str] = []

    icon = create_tray_icon(
        lambda action: None, ready_timeout=0.2, note=notes.append
    )

    assert icon is None
    assert created[0].stop_calls == 1
    assert created[0].visible is False
    assert notes and "系统托盘" in notes[0]
    assert "agentchatroom[gui]" not in notes[0]


def test_stop_tray_icon_tolerates_a_loop_that_never_ran() -> None:
    class Broken:
        def stop(self):
            raise AttributeError("'NoneType' has no attribute 'PostMessage'")

    stop_tray_icon(Broken())
    stop_tray_icon(None)


def test_real_pystray_defers_visible_to_the_ready_setup() -> None:
    """The library contract the fix relies on, checked against real pystray."""
    import inspect

    pytest.importorskip("pystray")
    try:
        import pystray._base as pystray_base
    except ImportError:  # pragma: no cover - backend selection is platform bound
        pytest.skip("pystray base icon unavailable")

    assert "setup" in inspect.signature(pystray_base.Icon.run_detached).parameters
    source = inspect.getsource(pystray_base.Icon)
    # pystray itself sets visible inside the setup thread, and the setter
    # short-circuits on an equal value: pre-setting it from the caller's thread
    # is what skipped NIM_ADD.
    assert "self.visible = True" in source
    assert "if self._visible == value" in source


def test_tray_degraded_hint_names_the_failure_and_the_fallback() -> None:
    hint = tray_degraded_hint("OSError: Shell_NotifyIconW returned FALSE", 3.0)
    assert "OSError: Shell_NotifyIconW returned FALSE" in hint
    assert "不会收起到系统托盘" in hint
    assert "双击" not in hint
    assert "3" in tray_degraded_hint("", 3.0)


def test_run_gui_stops_the_tray_loop_when_the_mainloop_exits(monkeypatch, settings) -> None:
    created = install_fake_pystray(monkeypatch)
    window = None
    windows: list = []
    tk = require_tk_runtime()

    real_create = gui_module.create_tray_icon
    monkeypatch.setattr(
        gui_module,
        "create_tray_icon",
        lambda handler, *, note=None: real_create(
            handler, ready_timeout=5, note=note
        ),
    )
    monkeypatch.setattr(gui_module, "load_settings", lambda _config: settings)
    monkeypatch.setattr(gui_module, "LogTail", _FakeThread)
    monkeypatch.setattr(gui_module, "RoomEventTail", _FakeThread)
    monkeypatch.setattr(gui_module, "GuiInstanceGuard", _Guard)
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: windows.append(self))

    boot_gui_or_skip(gui_module, tk)
    window = windows[0]
    icon = window.tray
    assert icon is not None
    assert icon.visible is True
    # run_gui already returned: its finally block must have stopped the loop.
    assert icon.stop_calls == 1
    assert icon is created[0]

    if window is not None:
        window.destroy()


def test_run_gui_reports_tray_degradation_without_the_generic_hint(
    monkeypatch, settings
) -> None:
    tk = require_tk_runtime()

    created = install_fake_pystray(monkeypatch, behaviour="never_ready")
    real_create = gui_module.create_tray_icon
    monkeypatch.setattr(
        gui_module,
        "create_tray_icon",
        lambda handler, *, note=None: real_create(
            handler, ready_timeout=0.2, note=note
        ),
    )
    windows: list = []
    monkeypatch.setattr(gui_module, "load_settings", lambda _config: settings)
    monkeypatch.setattr(gui_module, "LogTail", _FakeThread)
    monkeypatch.setattr(gui_module, "RoomEventTail", _FakeThread)
    monkeypatch.setattr(gui_module, "GuiInstanceGuard", _Guard)
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: windows.append(self))

    boot_gui_or_skip(gui_module, tk)
    window = windows[0]
    try:
        shown = window.log_view.get("1.0", "end")
        assert window.tray is None
        assert created[0].stop_calls == 1
        assert "不会收起到系统托盘" in shown
        # pystray IS installed here, so the "please pip install" hint would lie.
        assert "agentchatroom[gui]" not in shown
    finally:
        window.destroy()


# --------------------------------------------------------------------------------------
# #315 bounded server-log reads
# --------------------------------------------------------------------------------------
def test_running_url_from_log_scans_only_a_bounded_tail(tmp_path, monkeypatch) -> None:
    log_path = tmp_path / "server.log"
    noise = "[service] chatty diagnostic line that keeps the log growing\n" * 20000
    log_path.write_text(
        noise + f"INFO: Uvicorn running on {STARTED_URL} (Press CTRL+C to quit)\n",
        encoding="utf-8",
    )
    assert log_path.stat().st_size > RUNNING_URL_TAIL_BYTES * 4

    forbid_whole_log_reads(monkeypatch, log_path)
    calls: list[tuple[int, dict]] = []
    real_tail = gui_module.tail_text_lines

    def spy(path, limit, **kwargs):
        calls.append((limit, kwargs))
        return real_tail(path, limit, **kwargs)

    monkeypatch.setattr(gui_module, "tail_text_lines", spy)

    assert running_url_from_log(log_path) == STARTED_URL
    assert [limit for limit, _ in calls] == [RUNNING_URL_TAIL_LINES]
    assert calls[0][1]["max_scan_bytes"] == RUNNING_URL_TAIL_BYTES


def test_running_url_from_log_keeps_the_last_address_and_missing_file_shape(
    tmp_path,
) -> None:
    log_path = tmp_path / "server.log"
    assert running_url_from_log(log_path) is None
    log_path.write_text(
        "INFO: Uvicorn running on http://127.0.0.1:9000\n"
        "INFO: Uvicorn running on http://127.0.0.1:9001\n",
        encoding="utf-8",
    )
    assert running_url_from_log(log_path) == "http://127.0.0.1:9001"


def test_poll_state_never_touches_the_log_for_a_gui_started_service(
    monkeypatch, settings
) -> None:
    window = open_window(monkeypatch, settings)
    try:
        pid = claim_running_service(monkeypatch, settings)
        window.handle_event(
            ("started", {"started": True, "pid": pid, "url": STARTED_URL})
        )

        def explode(*_args, **_kwargs):
            raise AssertionError("the remembered URL should have been used")

        monkeypatch.setattr(gui_module, "running_url_from_log", explode)
        window.poll_state()
        assert STARTED_URL in window.status_label.cget("text")
        window.poll_state()
        assert STARTED_URL in window.status_label.cget("text")
    finally:
        window.destroy()


def test_poll_state_resolves_an_external_service_once_per_process(
    monkeypatch, settings
) -> None:
    window = open_window(monkeypatch, settings)
    try:
        pid = claim_running_service(
            monkeypatch,
            settings,
            url_line=f"INFO: Uvicorn running on {STARTED_URL}\n",
        )
        forbid_whole_log_reads(monkeypatch, settings.data_dir / "server.log")
        real_lookup = gui_module.running_url_from_log
        lookups: list[Path] = []

        def counting_lookup(path):
            lookups.append(Path(path))
            return real_lookup(path)

        monkeypatch.setattr(gui_module, "running_url_from_log", counting_lookup)

        window.poll_state()
        window.poll_state()
        window.poll_state()

        # One bounded tail read for the whole life of that process, not one per tick.
        assert len(lookups) == 1
        assert STARTED_URL in window.status_label.cget("text")
    finally:
        window.destroy()


def test_poll_state_does_not_report_a_false_service_exit(monkeypatch, settings) -> None:
    window = open_window(monkeypatch, settings)
    try:
        pid = claim_running_service(
            monkeypatch, settings, url_line=f"INFO: Uvicorn running on {STARTED_URL}\n"
        )

        def explode(*_args, **_kwargs):
            raise AssertionError("a running service must not be re-probed mid-poll")

        monkeypatch.setattr(gui_module, "running_url_from_log", explode)
        window.handle_event(
            ("started", {"started": True, "pid": pid, "url": STARTED_URL})
        )
        window.poll_state()
        window.poll_state()
        window.poll_state()
        assert "服务进程已退出" not in window.log_view.get("1.0", "end")

        (settings.data_dir / "server.pid").unlink()
        window.poll_state()
        window.poll_state()
        exits = window.log_view.get("1.0", "end").count("服务进程已退出")
        assert exits == 1
    finally:
        window.destroy()


def test_on_frontend_uses_the_remembered_url_without_reading_the_log(
    monkeypatch, settings
) -> None:
    window = open_window(monkeypatch, settings)
    try:
        pid = claim_running_service(monkeypatch, settings)
        window.handle_event(
            ("started", {"started": True, "pid": pid, "url": STARTED_URL})
        )

        def explode(*_args, **_kwargs):
            raise AssertionError("open frontend must reuse the remembered URL")

        monkeypatch.setattr(gui_module, "running_url_from_log", explode)
        opened: list[str] = []
        monkeypatch.setattr(
            gui_module, "webbrowser", types.SimpleNamespace(open=opened.append)
        )

        window.on_frontend()
        deadline = 20
        while not opened and deadline:
            threading.Event().wait(0.05)
            deadline -= 1
        assert opened == [STARTED_URL]
    finally:
        window.destroy()


def test_on_frontend_resolves_an_external_service_through_one_bounded_read(
    monkeypatch, settings
) -> None:
    window = open_window(monkeypatch, settings)
    try:
        claim_running_service(
            monkeypatch,
            settings,
            url_line="INFO: Uvicorn running on http://127.0.0.1:9000\n",
        )
        forbid_whole_log_reads(monkeypatch, settings.data_dir / "server.log")
        real_lookup = gui_module.running_url_from_log
        lookups: list[int] = []

        def counting_lookup(path):
            lookups.append(1)
            return real_lookup(path)

        monkeypatch.setattr(gui_module, "running_url_from_log", counting_lookup)
        opened: list[str] = []
        monkeypatch.setattr(
            gui_module, "webbrowser", types.SimpleNamespace(open=opened.append)
        )

        window.on_frontend()
        window.on_frontend()
        window.on_frontend()
        deadline = 40
        while len(opened) < 3 and deadline:
            threading.Event().wait(0.05)
            deadline -= 1

        assert opened == ["http://127.0.0.1:9000"] * 3
        # Three clicks on an externally started service cost one bounded read.
        assert len(lookups) == 1
    finally:
        window.destroy()


# --------------------------------------------------------------------------------------
# #322 in-place config patching
# --------------------------------------------------------------------------------------
COMPLEX_CONFIG = """\
schema_note = 1

[server]
# Listen address and port of the local Room service.
host = "127.0.0.1"
port = 8765
mcp_http_enabled = true

[server.tls]
enabled = false

[[hooks]]
command = "python"
args = ["-m", "agentchatroom"]

[knowledge]
kinds = [
    "decision",
    "procedure",
]
"""


def comment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("#")]


def test_update_config_values_keeps_every_comment_and_nested_table(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(COMPLEX_CONFIG, encoding="utf-8")

    update_config_values(config_path, host="0.0.0.0", port=9100)

    saved = config_path.read_text(encoding="utf-8")
    document = tomllib.loads(saved)
    assert document["server"]["host"] == "0.0.0.0"
    assert document["server"]["port"] == 9100
    assert document["server"]["mcp_http_enabled"] is True
    assert comment_lines(saved) == comment_lines(COMPLEX_CONFIG)
    assert "# Listen address and port of the local Room service." in saved
    assert document["server"]["tls"]["enabled"] is False
    assert document["hooks"] == [{"command": "python", "args": ["-m", "agentchatroom"]}]
    assert document["knowledge"]["kinds"] == ["decision", "procedure"]
    assert 'args = ["-m", "agentchatroom"]' in saved


def test_update_config_values_survives_a_root_scalar_that_used_to_raise_type_error(
    tmp_path,
) -> None:
    """A root-level int made the old rebuild do ``dict(1)`` -> TypeError."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(COMPLEX_CONFIG, encoding="utf-8")

    update_config_values(config_path, port=9123)

    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["schema_note"] == 1
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["server"]["port"] == 9123


def test_update_config_values_keeps_the_checked_in_example_comments(tmp_path) -> None:
    example = CHECKOUT_ROOT / "config.example.toml"
    if not example.is_file():
        pytest.skip("config.example.toml is not part of this checkout")
    config_path = tmp_path / "config.toml"
    original = example.read_text(encoding="utf-8")
    config_path.write_text(original, encoding="utf-8")

    update_config_values(config_path, host="127.0.0.1", port=9131)

    saved = config_path.read_text(encoding="utf-8")
    document = tomllib.loads(saved)
    assert document["server"]["host"] == "127.0.0.1"
    assert document["server"]["port"] == 9131
    assert len(comment_lines(saved)) == len(comment_lines(original))
    for comment in comment_lines(original):
        assert comment in saved


def test_update_config_values_writes_atomically_and_leaves_no_temp_file(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(COMPLEX_CONFIG, encoding="utf-8")

    def explode(*_args):
        raise OSError("interrupted")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        update_config_values(config_path, port=9111)
    monkeypatch.undo()

    assert config_path.read_text(encoding="utf-8") == COMPLEX_CONFIG
    assert [item.name for item in tmp_path.iterdir() if ".tmp" in item.name] == []

    update_config_values(config_path, port=9111)
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["server"]["port"] == 9111


def test_update_config_values_refuses_a_corrupt_file_without_touching_it(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    corrupt = "[server\nport = "
    config_path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(DomainError) as captured:
        update_config_values(config_path, port=9000)

    assert captured.value.code == "config_file_unparsable"
    assert captured.value.details["required_action"] == (
        "restore_config_from_backup_or_delete_it"
    )
    assert config_path.read_text(encoding="utf-8") == corrupt


def test_update_config_values_without_values_is_a_no_op(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(COMPLEX_CONFIG, encoding="utf-8")

    update_config_values(config_path)

    assert config_path.read_text(encoding="utf-8") == COMPLEX_CONFIG


@pytest.mark.parametrize(
    "error",
    [TypeError("dict(1)"), ValueError("bad"), DomainError("config_file_unparsable", "no")],
)
def test_start_worker_reports_a_config_write_failure_as_a_log_line(
    monkeypatch, settings, error
) -> None:
    """The service DID start: a config write problem must never read 「启动失败」."""
    sink: Queue = Queue()

    def failing_write(_path, **_kwargs):
        raise error

    monkeypatch.setattr(gui_module, "update_config_values", failing_write)

    run_start_worker(
        sink,
        host=settings.host,
        port=9123,
        settings=settings,
        start_fn=lambda _host, _port: {"started": True, "pid": 1, "url": STARTED_URL},
        port_probe=lambda _host, _port: True,
    )

    events = []
    while not sink.empty():
        events.append(sink.get_nowait())
    kinds = [kind for kind, _payload in events]
    assert "start_failed" not in kinds
    assert "started" in kinds
    failures = [payload for kind, payload in events if kind == "log" and "[配置写入失败]" in str(payload)]
    assert len(failures) == 1
    assert "服务已启动" in str(failures[0])
    assert type(error).__name__ in str(failures[0])
