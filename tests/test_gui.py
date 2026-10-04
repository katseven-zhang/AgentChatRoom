# audit-allow-file:windows_absolute_path,windows_user_home,private_lan_address,live_domain_id
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from queue import Queue

import pytest

from agentchatroom.cli import build_parser, process_is_running, service_url
from agentchatroom.config import Settings
from agentchatroom.gui import (
    CLOSE,
    KEEP_RUNNING,
    LogTail,
    HostError,
    PORT_FREE,
    PORT_OCCUPIED,
    PORT_RESERVED,
    PortError,
    STAY,
    STOP_AND_CLOSE,
    ServiceController,
    action_after_worker_event,
    build_tray_menu_spec,
    button_states,
    close_action,
    config_file_path,
    duplicate_instance_notice,
    format_worker_error,
    host_requires_management_auth,
    port_is_free,
    port_probe_state,
    port_unavailable_message,
    redact_line,
    restart_steps,
    resolve_frontend_url,
    run_restart_worker,
    run_start_worker,
    run_stop_worker,
    running_url_from_log,
    tray_icon_image,
    update_config_port,
    update_config_values,
    validate_host,
    validate_port,
)
from agentchatroom.gui_autostart import (
    GuiInstanceGuard, LoginStartup, instance_guard_name, is_temporary_executable,
    startup_command,
)
from agentchatroom.gui_event_log import (
    RoomEventTail, format_room_event, format_service_line, sanitize_gui_text,
)
from tests.conftest import boot_gui_or_skip, require_tk_runtime


@pytest.fixture
def isolated_gui_instance_guard(monkeypatch) -> None:
    """Keep GUI layout tests independent of the user's running controller."""
    import agentchatroom.gui as gui_module

    class Guard:
        def __init__(self, *, name: str, window_title: str):
            assert name
            assert window_title

        def acquire(self) -> bool:
            return True

        def activation_requested(self) -> bool:
            return False

        def release(self) -> None:
            pass

    monkeypatch.setattr(gui_module, "GuiInstanceGuard", Guard)


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex and event")
def test_second_gui_launch_signals_existing_instance() -> None:
    from uuid import uuid4

    name = f"AgentChatRoom_Test_GUI_{uuid4().hex}"
    owner = GuiInstanceGuard(name)
    contender = GuiInstanceGuard(name)
    successor = GuiInstanceGuard(name)
    try:
        assert owner.acquire() is True
        assert owner.activation_requested() is False
        assert contender.acquire() is False
        assert owner.activation_requested() is True
        assert owner.activation_requested() is False
    finally:
        contender.release()
        owner.release()
    try:
        assert successor.acquire() is True
    finally:
        successor.release()


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI window activation")
def test_second_gui_launch_restores_minimized_window() -> None:
    import ctypes
    from ctypes import wintypes
    from uuid import uuid4

    tk = require_tk_runtime()
    try:
        window = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")
    title = f"AgentChatRoom GUI test {uuid4().hex}"
    name = f"AgentChatRoom_Test_GUI_{uuid4().hex}"
    owner = GuiInstanceGuard(name, title)
    contender = GuiInstanceGuard(name, title)
    try:
        window.title(title)
        window.update()
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        handle = user32.FindWindowW(None, title)
        assert handle
        assert owner.acquire() is True
        user32.ShowWindow(handle, 6)  # SW_MINIMIZE
        assert user32.IsIconic(handle)
        assert contender.acquire() is False
        window.update()
        assert user32.IsWindowVisible(handle)
        assert not user32.IsIconic(handle)
    finally:
        contender.release()
        owner.release()
        window.destroy()


def test_instance_guard_name_scopes_the_lock_to_one_data_directory(tmp_path) -> None:
    """A dev checkout and a packaged release must not exclude each other.

    They manage different databases, pid files and log files, so the second one
    refusing to start because the first holds the lock is a defect, not the
    single-instance contract the README describes.
    """
    development = instance_guard_name(tmp_path / "dev-runtime")
    release = instance_guard_name(tmp_path / "release-runtime")

    assert development != release
    assert development == instance_guard_name(tmp_path / "dev-runtime")
    # Windows object names carry no path separators.
    assert "\\" not in development and "/" not in development
    assert development == instance_guard_name(Path(str(tmp_path / "dev-runtime")))


def test_instance_guard_name_survives_an_unresolvable_data_directory() -> None:
    assert instance_guard_name(None) == instance_guard_name(None)
    assert instance_guard_name(None) != instance_guard_name("some-directory")


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex and event")
def test_two_data_directories_can_run_a_console_at_the_same_time(tmp_path) -> None:
    development = GuiInstanceGuard(instance_guard_name(tmp_path / "dev"))
    release = GuiInstanceGuard(instance_guard_name(tmp_path / "release"))
    same_directory = GuiInstanceGuard(instance_guard_name(tmp_path / "dev"))
    try:
        assert development.acquire() is True
        assert release.acquire() is True
        assert same_directory.acquire() is False
    finally:
        same_directory.release()
        release.release()
        development.release()


def test_duplicate_instance_notice_says_where_the_window_went() -> None:
    notice = duplicate_instance_notice("AgentChatRoom")
    assert "AgentChatRoom 控制台" in notice
    assert "系统托盘" in notice
    assert "不同的数据目录" in notice


def test_run_gui_reports_a_duplicate_instance_instead_of_returning_silently(
    monkeypatch, settings, capsys
) -> None:
    import agentchatroom.gui as gui_module

    try:
        import tkinter as tk
    except ImportError as error:
        pytest.skip(f"Tk unavailable: {error}")

    monkeypatch.setattr(gui_module, "load_settings", lambda _config: settings)

    class DuplicateGuard:
        def __init__(self, **_kwargs) -> None:
            pass

        def acquire(self) -> bool:
            return False

    monkeypatch.setattr(gui_module, "GuiInstanceGuard", DuplicateGuard)
    monkeypatch.setattr(
        gui_module,
        "create_tray_icon",
        lambda *_a, **_k: pytest.fail("no window may be built for a duplicate"),
    )

    boot_gui_or_skip(gui_module, tk)

    # A silent return is indistinguishable from a program that refused to
    # start; the user is told a console is already running and where to find it.
    assert "控制台正在运行" in capsys.readouterr().err


def test_run_gui_opens_the_window_when_the_database_cannot_be_opened(
    monkeypatch, settings
) -> None:
    """A missing PostgreSQL URL is a fixable config error, not a missing GUI.

    ``create_database`` raises while the Room event tail is being constructed;
    before #331 that exception escaped ``run_gui`` before any window existed, so
    the user saw the program silently fail to start with no diagnostic at all.
    """
    import agentchatroom.gui as gui_module

    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
    except (ImportError, tk.TclError) as error:
        pytest.skip(f"Tk display unavailable: {error}")

    missing_url_env = "AGENTCHATROOM_TEST_MISSING_DATABASE_URL"
    postgres = replace(
        settings, database_backend="postgresql", database_url_env=missing_url_env
    )
    monkeypatch.delenv(missing_url_env, raising=False)
    monkeypatch.setattr(gui_module, "load_settings", lambda _config: postgres)

    class RecordingLogTail:
        def __init__(self, path, _sink, stop_event):
            self.path = path
            self.stop_event = stop_event
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(gui_module, "LogTail", RecordingLogTail)
    monkeypatch.setattr(
        gui_module, "create_tray_icon", lambda _action, **_kwargs: None
    )

    windows: list = []
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: windows.append(self))
    boot_gui_or_skip(gui_module, tk)

    assert windows, "the window must appear even when the database cannot open"
    window = windows[0]
    try:
        log_text = window.log_view.get("1.0", "end")
        assert missing_url_env in log_text
        assert "Room 事件读取器未启动" in log_text
        # The failed reader must not leave the controls stuck mid-action.
        assert window.action_active is False
        assert window.start_button.instate(["!disabled"])
    finally:
        window.destroy()


def port_is_listening(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port_to_close(port: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not port_is_listening(port):
            return True
        time.sleep(0.05)
    return not port_is_listening(port)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8765", 8765),
        ("  8765  ", 8765),
        ("1", 1),
        ("65535", 65535),
    ],
)
def test_validate_port_accepts_usable_ports(raw: str, expected: int) -> None:
    assert validate_port(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "0", "65536", "99999999", "abc", "12.5", "-1", "8 765", "8765号"],
)
def test_validate_port_rejects_unusable_input(raw: str) -> None:
    with pytest.raises(PortError):
        validate_port(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("  localhost  ", "localhost"),
        ("::1", "::1"),
        ("0.0.0.0", "0.0.0.0"),
        ("192.168.1.10", "192.168.1.10"),
        ("::", "::"),
    ],
)
def test_validate_host_accepts_usable_addresses(raw: str, expected: str) -> None:
    assert validate_host(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "example.com", "999.1.1.1", "127.0.0.1:8765", "0x1", "1.2.3.4.5"],
)
def test_validate_host_rejects_unusable_input(raw: str) -> None:
    with pytest.raises(HostError):
        validate_host(raw)


@pytest.mark.parametrize(
    ("host", "requires_auth"),
    [
        ("127.0.0.1", False),
        ("localhost", False),
        ("::1", False),
        ("0.0.0.0", True),
        ("192.168.1.10", True),
        ("::", True),
    ],
)
def test_host_auth_rule_mirrors_server_config(host: str, requires_auth: bool) -> None:
    assert host_requires_management_auth(host) is requires_auth


def test_restart_steps_stop_before_start_only_when_running() -> None:
    assert restart_steps(service_running=True) == ("stop", "start")
    assert restart_steps(service_running=False) == ("start",)


def test_resolve_frontend_url_prefers_logged_address() -> None:
    assert (
        resolve_frontend_url("http://127.0.0.1:9000", "0.0.0.0", 8765)
        == "http://127.0.0.1:9000"
    )
    assert resolve_frontend_url(None, "127.0.0.1", 8765) == service_url(
        "127.0.0.1", 8765
    )


def test_tray_menu_covers_console_lifecycle_actions() -> None:
    spec = build_tray_menu_spec()
    actions = [action for _label, action, _default in spec]
    assert actions == ["show", "frontend", "start", "restart", "stop", "quit"]
    defaults = [default for _label, _action, default in spec]
    assert defaults.count(True) == 1
    assert spec[0][0] == "打开控制台"
    assert defaults[0] is True


def test_tray_icon_image_renders_with_pillow() -> None:
    pytest.importorskip("PIL")
    image = tray_icon_image()
    assert image is not None
    assert image.size == (64, 64)


def test_create_tray_icon_degrades_without_pystray(monkeypatch) -> None:
    import agentchatroom.gui as gui_module

    monkeypatch.setitem(sys.modules, "pystray", None)
    assert gui_module.create_tray_icon(lambda action: None) is None


def test_create_tray_icon_actions_have_pystray_compatible_signatures(monkeypatch) -> None:
    import inspect
    import types

    import agentchatroom.gui as gui_module

    actions: list[str] = []
    menu_items = []

    def menu_item(label, callback, *, default=False):
        assert len(inspect.signature(callback).parameters) == 2
        menu_items.append((label, callback, default))
        return label, callback, default

    class Icon:
        """pystray._win32 ordering: the handle exists only inside the loop thread.

        ``visible`` therefore raises when it is set before the backend is
        ready, which is exactly the NIM_ADD-before-_hwnd race #314 removed.
        """

        def __init__(self, name, image, title, menu):
            self.menu = menu
            self._hwnd = None
            self._visible = False
            self.stop_calls = 0

        @property
        def visible(self) -> bool:
            return self._visible

        @visible.setter
        def visible(self, value: bool) -> None:
            if self._visible == value:
                return
            if value:
                if self._hwnd is None:
                    raise RuntimeError("NIM_ADD without a notify window handle")
                self._visible = True
            else:
                self._visible = False

        def run_detached(self, setup=None) -> None:
            ready = setup or (lambda icon: setattr(icon, "visible", True))
            threading.Thread(target=self._run, args=(ready,), daemon=True).start()

        def _run(self, setup) -> None:
            self._hwnd = object()
            setup(self)

        def stop(self) -> None:
            self.stop_calls += 1

    monkeypatch.setitem(
        sys.modules,
        "pystray",
        types.SimpleNamespace(Menu=lambda *items: items, MenuItem=menu_item, Icon=Icon),
    )
    monkeypatch.setattr(gui_module, "tray_icon_image", lambda: object())

    icon = gui_module.create_tray_icon(actions.append)
    assert icon is not None
    assert icon.visible is True
    assert icon.stop_calls == 0
    for _label, callback, _default in menu_items:
        callback(icon, None)
    assert actions == [action for _label, action, _default in build_tray_menu_spec()]


def test_gui_status_uses_full_width_row(
    monkeypatch, settings, isolated_gui_instance_guard
) -> None:
    tk = require_tk_runtime()

    import agentchatroom.gui as gui_module

    windows = []
    monkeypatch.setattr(gui_module, "load_settings", lambda _config: settings)
    monkeypatch.setattr(
        gui_module, "create_tray_icon", lambda _action, **_kwargs: None
    )
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: windows.append(self))

    boot_gui_or_skip(gui_module, tk)
    window = windows[0]
    try:
        assert window.status_label.master is window.start_button.master.master
        assert window.status_label.pack_info()["side"] == "top"
        assert window.status_label.pack_info()["fill"] == "x"
    finally:
        window.destroy()


def test_gui_login_checkbox_and_autostart_action(
    monkeypatch, settings, isolated_gui_instance_guard
) -> None:
    tk = require_tk_runtime()

    import agentchatroom.gui as gui_module

    class FakeStartup:
        active = False

        def enabled(self):
            return self.active

        def is_current(self):
            return True

        def enable(self):
            self.active = True

        def disable(self):
            self.active = False

    startup = FakeStartup()
    windows = []
    monkeypatch.setattr(gui_module, "load_settings", lambda _config: settings)
    monkeypatch.setattr(gui_module, "LoginStartup", lambda: startup)
    monkeypatch.setattr(
        gui_module, "create_tray_icon", lambda _action, **_kwargs: None
    )
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: windows.append(self))

    boot_gui_or_skip(gui_module, tk, autostart=True)
    window = windows[0]
    try:
        assert window.login_startup_checkbox.cget("text") == "开机启动"
        assert window.login_startup_var.get() is False
        window.login_startup_var.set(True)
        window.on_login_startup_toggle()
        assert startup.active is True
        window.login_startup_var.set(False)
        window.on_login_startup_toggle()
        assert startup.active is False
        calls = []
        window.on_start = lambda: calls.append("start")
        window.start_after_login()
        assert calls == ["start"]
    finally:
        window.destroy()


@pytest.mark.parametrize("same_data_dir", [False, True])
def test_login_autostart_config_error_keeps_gui_open_for_retry(
    monkeypatch, settings, isolated_gui_instance_guard, same_data_dir
) -> None:
    tk = require_tk_runtime()

    import agentchatroom.gui as gui_module

    attempts = []
    def load(_path):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("invalid port")
        return settings

    log_tails = []
    room_tails = []

    class FakeLogTail:
        def __init__(self, path, _sink, stop_event):
            self.path, self.stop_event = path, stop_event
            log_tails.append(self)

        def start(self):
            pass

    class FakeRoomTail:
        def __init__(self, active_settings, _sink, stop_event, *, service=None):
            self.settings, self.stop_event = active_settings, stop_event
            self.service = service
            self.cursors, self.presence = {}, {}
            room_tails.append(self)

        def start(self):
            pass

    windows = []
    def mainloop(window):
        windows.append(window)
        assert window._retry_config_load() is True

    monkeypatch.setattr(gui_module, "load_settings", load)
    monkeypatch.setattr(
        gui_module, "default_data_dir",
        lambda: settings.data_dir if same_data_dir else settings.data_dir / "fallback",
    )
    monkeypatch.setattr(gui_module, "LogTail", FakeLogTail)
    monkeypatch.setattr(gui_module, "RoomEventTail", FakeRoomTail)
    monkeypatch.setattr(
        gui_module, "create_tray_icon", lambda _action, **_kwargs: None
    )
    monkeypatch.setattr(tk.Tk, "mainloop", mainloop)
    boot_gui_or_skip(gui_module, tk, autostart=True)
    window = windows[0]
    try:
        assert "配置读取失败" in window.log_view.get("1.0", "end")
        assert len(attempts) == 2
        assert len(room_tails) == 2
        assert len(log_tails) == (1 if same_data_dir else 2)
        assert (room_tails[0].settings.database_path == settings.database_path) is same_data_dir
        assert room_tails[1].settings.database_path == settings.database_path
        assert log_tails[-1].path == settings.data_dir / "server.log"
        assert room_tails[0].stop_event.is_set()
    finally:
        window.destroy()


def test_config_retry_keeps_the_old_room_reader_when_the_rebuild_fails(
    monkeypatch, settings, isolated_gui_instance_guard
) -> None:
    """Stopping the old reader before building its replacement is what made
    the panel permanently blank: a rebuild that raised left nothing running.
    """
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
    except (ImportError, tk.TclError) as error:
        pytest.skip(f"Tk display unavailable: {error}")

    import agentchatroom.gui as gui_module

    fallback = replace(settings, data_dir=settings.data_dir / "fallback")
    attempts = []

    def load(_path):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("invalid port")
        return settings

    log_tails = []
    room_tails = []

    class FakeLogTail:
        def __init__(self, path, _sink, stop_event):
            self.path, self.stop_event = path, stop_event
            log_tails.append(self)

        def start(self):
            pass

    class FakeRoomTail:
        def __init__(self, active_settings, _sink, stop_event, *, service=None):
            self.settings, self.stop_event = active_settings, stop_event
            self.service = service
            self.cursors, self.presence = {}, {}
            room_tails.append(self)

        def start(self):
            pass

    def exploding_room_tail(active_settings, sink, stop_event, *, service=None):
        # Same failure create_database raises for a postgresql backend whose
        # connection URL is missing.
        if room_tails:
            raise ValueError("AGENTCHATROOM_TEST_URL must contain a connection URL")
        return FakeRoomTail(active_settings, sink, stop_event, service=service)

    windows = []
    observed: dict = {}

    def mainloop(window):
        windows.append(window)
        assert window._retry_config_load() is True
        # Sampled here: once run_gui returns, its shutdown stops every reader.
        observed["room_stopped"] = room_tails[0].stop_event.is_set()
        observed["old_log_stopped"] = log_tails[0].stop_event.is_set()
        observed["log_count"] = len(log_tails)

    monkeypatch.setattr(gui_module, "load_settings", load)
    monkeypatch.setattr(gui_module, "default_data_dir", lambda: fallback.data_dir)
    monkeypatch.setattr(gui_module, "LogTail", FakeLogTail)
    monkeypatch.setattr(gui_module, "RoomEventTail", exploding_room_tail)
    monkeypatch.setattr(
        gui_module, "create_tray_icon", lambda _action, **_kwargs: None
    )
    monkeypatch.setattr(tk.Tk, "mainloop", mainloop)
    boot_gui_or_skip(gui_module, tk, autostart=True)
    window = windows[0]
    try:
        assert len(room_tails) == 1
        # The reader that was already running is still running: only the log
        # reader, whose data directory genuinely changed, was rebuilt.
        assert observed["room_stopped"] is False
        assert observed["old_log_stopped"] is True
        assert observed["log_count"] == 2
        text = window.log_view.get("1.0", "end")
        assert "Room 事件读取器未启动" in text
        assert "配置已重新加载" in text
        assert window.action_active is False
    finally:
        window.destroy()


def test_button_states_follow_service_and_action_state() -> None:
    assert button_states(service_running=False, action_active=False) == (True, False)
    assert button_states(service_running=True, action_active=False) == (False, True)
    assert button_states(service_running=False, action_active=True) == (False, False)
    assert button_states(service_running=True, action_active=True) == (False, False)


def test_action_after_worker_event_clears_only_terminal_kinds() -> None:
    assert action_after_worker_event(True, "start_failed") is False
    assert action_after_worker_event(True, "restart_aborted") is False
    assert action_after_worker_event(True, "stop_failed") is False
    assert action_after_worker_event(True, "stopped") is False
    assert action_after_worker_event(True, "started") is False
    assert action_after_worker_event(True, "log") is True
    assert action_after_worker_event(False, "log") is False


def test_format_worker_error_covers_system_exit_and_exception() -> None:
    assert format_worker_error(SystemExit("gone")) == "gone"
    assert format_worker_error(ValueError("boom")) == "ValueError: boom"


def test_start_worker_unexpected_error_resets_action_and_reports(
    settings,
) -> None:
    """#191: non-OSError in start_worker must surface and re-enable buttons."""
    sink: Queue = Queue()

    def boom(host: str, port: int) -> dict:
        raise ValueError("ctypes boundary")

    run_start_worker(
        sink,
        host="127.0.0.1",
        port=8765,
        settings=settings,
        start_fn=boom,
        port_probe=lambda _h, _p: True,
    )

    events = []
    while not sink.empty():
        events.append(sink.get_nowait())
    assert events, "error must be visible in the log sink"
    kind, message = events[-1]
    assert kind == "start_failed"
    assert "ValueError" in message
    assert "ctypes boundary" in message

    action_active = action_after_worker_event(True, kind)
    assert action_active is False
    start_enabled, stop_enabled = button_states(False, action_active)
    assert start_enabled is True
    assert stop_enabled is False


def test_restart_worker_unexpected_error_reports_restart_aborted(
    settings,
) -> None:
    """#191: restart failures use restart_aborted, not stop_failed."""
    sink: Queue = Queue()

    def boom() -> dict:
        raise ValueError("pid file decode")

    run_restart_worker(
        sink,
        is_running=lambda: True,
        stop_fn=boom,
        start_body=lambda: None,
    )

    kind, message = sink.get_nowait()
    assert kind == "restart_aborted"
    assert "ValueError" in message
    assert action_after_worker_event(True, kind) is False
    assert button_states(True, False) == (False, True)


def test_stop_worker_unexpected_error_reports_stop_failed() -> None:
    """#191: stop failures emit stop_failed so pending exit can continue."""
    sink: Queue = Queue()

    def boom() -> dict:
        raise RuntimeError("tree kill failed")

    run_stop_worker(sink, stop_fn=boom)

    kind, message = sink.get_nowait()
    assert kind == "stop_failed"
    assert "RuntimeError" in message
    assert action_after_worker_event(True, kind) is False


def test_start_worker_port_busy_still_reports_without_raising(settings) -> None:
    sink: Queue = Queue()
    run_start_worker(
        sink,
        host="127.0.0.1",
        port=9,
        settings=settings,
        start_fn=lambda h, p: {"started": True},
        port_probe=lambda _h, _p: False,
    )
    kind, message = sink.get_nowait()
    assert kind == "start_failed"
    assert "已被占用" in message


def test_close_action_maps_all_three_running_choices() -> None:
    assert close_action(service_running=False, user_choice="stop") == CLOSE
    assert close_action(service_running=False, user_choice="keep") == CLOSE
    assert close_action(service_running=False, user_choice=None) == CLOSE
    assert close_action(service_running=True, user_choice="stop") == STOP_AND_CLOSE
    assert close_action(service_running=True, user_choice="keep") == KEEP_RUNNING
    assert close_action(service_running=True, user_choice="cancel") == STAY
    assert close_action(service_running=True, user_choice=None) == STAY


def test_redact_line_masks_credentials() -> None:
    from agentchatroom.api import redact_log_line

    plain = 'INFO:     127.0.0.1:1 - "GET /health HTTP/1.1" 200'
    assert redact_line(plain) == plain
    assert redact_line("token=abc123.456") == "token=[REDACTED]"
    assert redact_line("token: abc123.456") == "token: [REDACTED]"
    assert redact_line("password=hunter2value") == "password=[REDACTED]"
    assert redact_line("api_key = value1234") == "api_key = [REDACTED]"
    assert redact_line("Authorization: " + "Bearer " + "abc.def.ghi") == (
        "Authorization: [REDACTED]"
    )
    assert redact_line("client sent " + "sk-" + "proj-" + "abcdefgh") == "client sent [REDACTED]"
    assert "abc123.456" not in redact_line("token=abc123.456")
    assert "sk-" not in redact_line("client sent " + "sk-" + "proj-" + "abcdefgh")
    # Shared policy: GUI and API produce identical masks for the same line.
    samples = [
        "token=abc123.456",
        "Authorization: " + "Bearer " + "abc.def.ghi",
        "client sent " + "sk-" + "proj-" + "abcdefgh",
        "ghp_" + ("A" * 36),
        "xoxb-" + ("a" * 20),
        "failed acr.credential_x.secretvaluehere",
    ]
    for sample in samples:
        assert redact_line(sample) == redact_log_line(sample)


def test_log_tail_forwards_redacted_events_from_file(tmp_path) -> None:
    log_path = tmp_path / "server.log"
    log_path.write_text("startup line\n", encoding="utf-8")
    sink: Queue = Queue()
    stop_event = threading.Event()
    tail = LogTail(log_path, sink, stop_event, tail_lines=2)
    tail.start()

    def collect(target: int, timeout: float = 5.0) -> list[tuple[str, str]]:
        collected: list[tuple[str, str]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(collected) < target:
            while not sink.empty():
                collected.append(sink.get_nowait())
            if len(collected) < target:
                time.sleep(0.05)
        return collected

    try:
        assert collect(1) == [("log", "历史服务日志：服务输出：startup line")]

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("second line\ntoken=hushhush\n")

        events = collect(2)
    finally:
        stop_event.set()
        tail.join(timeout=5)

    assert events == [("log", "服务输出：second line"), ("log", "服务输出：token=[REDACTED]")]
    assert "hushhush" not in "".join(payload for _kind, payload in events)


def test_running_url_from_log_returns_latest_address(tmp_path) -> None:
    log_path = tmp_path / "server.log"

    assert running_url_from_log(log_path) is None

    log_path.write_text("noise\n", encoding="utf-8")
    assert running_url_from_log(log_path) is None

    log_path.write_text(
        "INFO: Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)\n",
        encoding="utf-8",
    )
    assert running_url_from_log(log_path) == "http://127.0.0.1:8765"


def test_log_tail_recovers_after_service_log_truncated(tmp_path) -> None:
    log_path = tmp_path / "server.log"
    log_path.write_text("old line\n", encoding="utf-8")
    sink, stop_event = Queue(), threading.Event()
    tail = LogTail(log_path, sink, stop_event)
    tail.start()
    try:
        deadline = time.monotonic() + 3
        while sink.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "历史服务日志：服务输出：old line" == sink.get_nowait()[1]
        log_path.write_text("", encoding="utf-8")
        time.sleep(0.35)
        log_path.write_text("Application startup complete\n", encoding="utf-8")
        deadline = time.monotonic() + 3
        while sink.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sink.get_nowait() == ("log", "服务初始化完成。")
    finally:
        stop_event.set()
        tail.join(timeout=3)

    log_path.write_text(
        "INFO: Uvicorn running on http://127.0.0.1:8765\n"
        "INFO: Uvicorn running on http://127.0.0.1:9000\n",
        encoding="utf-8",
    )
    assert running_url_from_log(log_path) == "http://127.0.0.1:9000"


def test_config_file_path_prefers_explicit_config(settings) -> None:
    explicit = settings.data_dir / "custom.toml"
    configured = replace(settings, config_path=explicit)
    assert config_file_path(configured) == explicit
    assert config_file_path(settings) == settings.data_dir / "config.toml"


def test_update_config_values_persists_host_and_port(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                'host = "127.0.0.1"',
                "port = 9000",
                "",
                "[knowledge]",
                'kinds = ["decision", "api"]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    update_config_values(config_path, host="127.0.0.1", port=9100)

    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert document["server"]["host"] == "127.0.0.1"
    assert document["server"]["port"] == 9100
    assert document["knowledge"]["kinds"] == ["decision", "api"]

    update_config_values(config_path, host="0.0.0.0")

    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert document["server"]["host"] == "0.0.0.0"
    assert document["server"]["port"] == 9100


def test_update_config_port_preserves_other_settings(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                'host = "127.0.0.1"',
                "port = 9000",
                "",
                "[knowledge]",
                'kinds = ["decision", "api"]',
                "require_verified_task = false",
                "",
                "[interface]",
                'product_name = "AgentChatRoom"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    update_config_port(config_path, 9100)

    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert document["server"]["port"] == 9100
    assert document["server"]["host"] == "127.0.0.1"
    assert document["knowledge"]["kinds"] == ["decision", "api"]
    assert document["knowledge"]["require_verified_task"] is False
    assert document["interface"]["product_name"] == "AgentChatRoom"


def test_update_config_port_creates_missing_file(tmp_path) -> None:
    config_path = tmp_path / "nested" / "config.toml"

    update_config_port(config_path, 8123)

    document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert document["server"]["port"] == 8123


def test_port_is_free_reports_bound_port_as_taken() -> None:
    port = available_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        assert port_is_free("127.0.0.1", port) is False
    assert port_is_free("127.0.0.1", port) is True


def test_port_probe_rejects_live_reusable_listener() -> None:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert port_is_free("127.0.0.1", listener.getsockname()[1]) is False


def test_port_probe_matches_this_platform_socket_option_policy() -> None:
    """The SO_REUSEADDR choice is platform-dependent, so pin it per platform.

    ``port_is_free`` enables SO_REUSEADDR everywhere except Windows, because on
    Windows that option lets a probe *share* a live listener rather than fail -
    which would report a port in use as free. That branch was previously only
    reachable on POSIX, since the TIME_WAIT restart test skips on Windows, so
    the one platform where the option is deliberately absent had no coverage at
    all.

    A real listening socket is the only honest probe here: an address already
    bound must be reported as taken, whatever the platform does with the
    option. Asserting the reported answer on both platforms is what makes the
    ``os.name != "nt"`` condition meaningful rather than incidental.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port_is_free("127.0.0.1", port) is False
    # Released, the same address is free again - the probe is not simply
    # returning False for everything.
    assert port_is_free("127.0.0.1", port) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows socket option policy")
def test_windows_probe_does_not_enable_address_reuse() -> None:
    """On Windows the probe must not set SO_REUSEADDR, and must say so.

    Enabling it there would let the probe bind alongside a live listener and
    report a busy port as free, so the GUI would happily restart onto a port
    somebody else already owns. This asserts the observable consequence - a
    live listener is reported as taken - and pins the option itself, so the
    guard cannot be quietly deleted on the strength of the first assertion
    alone.
    """
    observed: list[int] = []
    original_setsockopt = socket.socket.setsockopt

    def spy_setsockopt(self, level, option, value):  # type: ignore[no-untyped-def]
        observed.append(option)
        return original_setsockopt(self, level, option, value)

    port = available_port()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        socket.socket.setsockopt = spy_setsockopt  # type: ignore[method-assign]
        try:
            assert port_is_free("127.0.0.1", port) is False
        finally:
            socket.socket.setsockopt = original_setsockopt  # type: ignore[method-assign]

    # SO_REUSEADDR is never requested on this platform, whether or not the
    # probe happened to need the socket option for anything else.
    assert socket.SO_REUSEADDR not in observed


@pytest.mark.skipif(os.name == "nt", reason="POSIX TCP TIME_WAIT restart semantics")
def test_port_probe_allows_restart_after_server_active_close() -> None:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(2)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        address = listener.getsockname()
        with socket.create_connection(address, timeout=2) as client:
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(2)
                peer.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert peer.recv(1) == b""
    # Both platforms must allow the immediate rebind a restart needs. POSIX
    # gets it from SO_REUSEADDR; Windows allows binding over a TIME_WAIT
    # reservation by default, and the probe no longer treats the leftover
    # reservation as a listener because nothing answers a connect on it.
    assert port_probe_state(*address) == PORT_FREE


def test_port_probe_names_a_live_listener_as_occupied() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        assert port_probe_state("127.0.0.1", listener.getsockname()[1]) == PORT_OCCUPIED


def test_port_probe_separates_a_reservation_from_a_listener(monkeypatch) -> None:
    """A refused bind that nothing answers must not be reported as occupied.

    Windows holds a just-closed connection in TIME_WAIT and refuses the bind
    without any process listening, so telling the user to stop a service or
    change the port sends them after a fix that cannot work.
    """
    import agentchatroom.gui as gui_module

    attempts: list[int] = []

    def refuse(_host, _port):
        attempts.append(1)
        return False

    monkeypatch.setattr(gui_module, "_bind_probe", refuse)
    monkeypatch.setattr(gui_module, "_listener_reachable", lambda *_a, **_k: False)
    monkeypatch.setattr(gui_module.time, "sleep", lambda _seconds: None)

    assert port_probe_state("127.0.0.1", 9100) == PORT_RESERVED
    # One bind to classify, then the bounded retries that give a reservation
    # the chance to clear before the user is told anything.
    assert len(attempts) == gui_module.PORT_PROBE_ATTEMPTS
    assert port_is_free("127.0.0.1", 9100) is False


def test_port_probe_retry_recovers_once_the_reservation_clears(monkeypatch) -> None:
    """The bounded retry is what makes a restart racing the OS release work."""
    import agentchatroom.gui as gui_module

    binds: list[int] = []

    def bind_after_two(_host, _port):
        binds.append(1)
        return len(binds) > 2

    monkeypatch.setattr(gui_module, "_bind_probe", bind_after_two)
    monkeypatch.setattr(gui_module, "_listener_reachable", lambda *_a, **_k: False)
    monkeypatch.setattr(gui_module.time, "sleep", lambda _seconds: None)

    assert port_probe_state("127.0.0.1", 9100) == PORT_FREE
    assert len(binds) == 3
    assert port_is_free("127.0.0.1", 9100) is True


def test_reserved_port_advice_says_wait_not_change_port() -> None:
    reserved = port_unavailable_message(9100, PORT_RESERVED)
    assert "已被占用" not in reserved
    assert "更换端口不能解决" in reserved
    assert "已被占用" in port_unavailable_message(9100, PORT_OCCUPIED)


def test_start_worker_explains_a_reserved_port_without_guessing(monkeypatch) -> None:
    """The default probe classifies, so a TIME_WAIT refusal is not called occupied."""
    import agentchatroom.gui as gui_module

    sink: Queue = Queue()
    monkeypatch.setattr(gui_module, "port_probe_state", lambda _h, _p: PORT_RESERVED)

    run_start_worker(
        sink,
        host="127.0.0.1",
        port=9100,
        settings=Settings(data_dir=Path("unused")),
        start_fn=lambda _h, _p: pytest.fail("must not start on a reserved port"),
    )

    kind, message = sink.get_nowait()
    assert kind == "start_failed"
    assert "更换端口不能解决" in message


def test_gui_service_controller_reuses_cli_lifecycle(settings) -> None:
    controller = ServiceController(settings)
    port = available_port()

    assert controller.is_running() is False

    started = controller.start(settings.host, port)
    pid = started["pid"]
    try:
        assert started["started"] is True
        assert controller.is_running() is True
        assert controller.running_pid() == pid
        assert process_is_running(pid)

        repeated = controller.start(settings.host, port)
        assert repeated["already_running"] is True
        assert repeated["pid"] == pid
        assert controller.display_url(settings.host, port) == service_url(
            settings.host, port
        )
    finally:
        stopped = controller.stop()

    assert stopped == {"stopped": True, "pid": pid}
    assert controller.is_running() is False
    assert not (settings.data_dir / "server.pid").exists()
    assert not process_is_running(pid)
    assert wait_for_port_to_close(port)
    assert port_is_free(settings.host, port) is True


def test_gui_service_controller_reports_stale_pid_as_stopped(settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "server.pid").write_text("99999999", encoding="ascii")
    controller = ServiceController(settings)

    assert controller.is_running() is False
    result = controller.stop()

    assert result["stopped"] is False
    assert result["reason"] == "not_running"
    assert not (settings.data_dir / "server.pid").exists()


def test_cli_registers_gui_command_without_tkinter() -> None:
    parser = build_parser()
    args = parser.parse_args(["gui", "--config", "custom.toml"])

    assert args.command == "gui"
    assert args.config == "custom.toml"


def test_run_gui_reports_missing_tkinter_with_actionable_error(monkeypatch) -> None:
    import agentchatroom.gui as gui_module

    monkeypatch.setitem(sys.modules, "tkinter", None)

    with pytest.raises(SystemExit) as exit_info:
        gui_module.run_gui(config_path="unused")

    assert "tkinter" in str(exit_info.value)
    assert "python.org" in str(exit_info.value)


def test_run_gui_reports_broken_tcl_runtime_with_visible_diagnostic(monkeypatch) -> None:
    """#161 退回回归：tkinter 可导入但 Tcl/Tk 运行库损坏（如打包缺 init.tcl）
    属于 TclError，必须给出明确修复提示并退出，而不是窗口未出现就静默崩溃。"""
    import types

    import agentchatroom.gui as gui_module

    broken = types.ModuleType("tkinter")
    broken.TclError = type("TclError", (Exception,), {})

    def _broken_tk():
        raise broken.TclError("Can't find a usable init.tcl in the following directories")

    broken.Tk = _broken_tk
    broken.TclVersion = 8.6
    broken.TkVersion = 8.6
    broken.messagebox = types.SimpleNamespace()
    broken.scrolledtext = types.SimpleNamespace()
    broken.ttk = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "tkinter", broken)

    dialogs: list[str] = []
    monkeypatch.setattr(gui_module, "_show_fatal_gui_dialog", dialogs.append)

    with pytest.raises(SystemExit) as exit_info:
        gui_module.run_gui(config_path="unused")

    message = str(exit_info.value)
    assert "init.tcl" in message or "Tcl/Tk" in message
    assert "重新打包" in message
    assert dialogs == [message]


def _load_entry_app():
    import importlib.util

    entry_path = (
        Path(__file__).resolve().parents[1] / "packaging" / "entry_app.py"
    )
    spec = importlib.util.spec_from_file_location("entry_app_under_test", entry_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_app_routes_default_to_gui_console(monkeypatch) -> None:
    import agentchatroom.gui as gui_module
    import agentchatroom.stdio_runtime as stdio_runtime

    entry_app = _load_entry_app()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(stdio_runtime, "prepare_standard_streams", lambda: None)
    monkeypatch.setattr(
        gui_module, "run_gui", lambda config_path: calls.append(("gui", config_path))
    )
    monkeypatch.setattr(sys, "argv", ["agentchatroom.exe"])

    entry_app.main()

    assert calls == [("gui", None)]


def test_entry_app_routes_gui_config_flag(monkeypatch) -> None:
    import agentchatroom.gui as gui_module
    import agentchatroom.stdio_runtime as stdio_runtime

    entry_app = _load_entry_app()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(stdio_runtime, "prepare_standard_streams", lambda: None)
    monkeypatch.setattr(
        gui_module, "run_gui", lambda config_path: calls.append(("gui", config_path))
    )
    monkeypatch.setattr(
        sys, "argv", ["agentchatroom.exe", "gui", "--config", "custom.toml"]
    )

    entry_app.main()

    assert calls == [("gui", "custom.toml")]


def test_entry_app_routes_login_autostart_flag(monkeypatch) -> None:
    import agentchatroom.gui as gui_module
    import agentchatroom.stdio_runtime as stdio_runtime

    entry_app = _load_entry_app()
    calls = []
    monkeypatch.setattr(stdio_runtime, "prepare_standard_streams", lambda: None)
    monkeypatch.setattr(gui_module, "run_gui", lambda path, *, autostart: calls.append((path, autostart)))
    monkeypatch.setattr(sys, "argv", ["agentchatroom.exe", "gui", "--autostart"])
    entry_app.main()
    assert calls == [(None, True)]


def test_login_startup_command_quotes_unicode_and_spaces() -> None:
    executable = Path(tempfile.gettempdir()) / "中文 目录" / "agentchatroom.exe"
    command = startup_command(executable=executable, frozen=True)
    assert command == f'"{executable.resolve()}" gui --autostart'
    assert is_temporary_executable(executable)


def test_login_startup_registry_round_trip_uses_only_own_value(monkeypatch, tmp_path) -> None:
    import types

    values = {"Another App": "keep"}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def query(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_READ=1, KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=lambda *_args: Key(), CreateKeyEx=lambda *_args: Key(),
        QueryValueEx=query,
        SetValueEx=lambda _key, name, _reserved, _kind, value: values.__setitem__(name, value),
        DeleteValue=lambda _key, name: values.pop(name),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr("agentchatroom.gui_autostart.is_temporary_executable", lambda _p: False)
    startup = LoginStartup(executable=tmp_path / "中文 目录" / "agentchatroom.exe", frozen=True)
    assert not startup.enabled()
    startup.enable()
    assert startup.enabled() and startup.is_current()
    startup.enable()
    assert len(values) == 2
    startup.disable()
    assert not startup.enabled()
    assert values == {"Another App": "keep"}


@pytest.mark.parametrize("previous", [None, "old startup command"])
@pytest.mark.parametrize("read_raises", [False, True])
def test_login_startup_failed_verification_restores_own_previous_value(
    monkeypatch, tmp_path, previous, read_raises
) -> None:
    import types

    values = {"Another App": "keep"}
    if previous is not None:
        values["AgentChatRoom"] = previous

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def query(_key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_READ=1, KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=lambda *_args: Key(), CreateKeyEx=lambda *_args: Key(),
        QueryValueEx=query,
        SetValueEx=lambda _key, name, _reserved, _kind, value: values.__setitem__(name, value),
        DeleteValue=lambda _key, name: values.pop(name),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr("agentchatroom.gui_autostart.is_temporary_executable", lambda _p: False)
    startup = LoginStartup(executable=tmp_path / "agentchatroom.exe", frozen=True)
    if read_raises:
        def unreadable():
            raise PermissionError("read denied")
        monkeypatch.setattr(startup, "read_command", unreadable)
    else:
        monkeypatch.setattr(startup, "read_command", lambda: "unreadable")
    with pytest.raises(OSError, match="校验失败"):
        startup.enable()
    assert values["Another App"] == "keep"
    assert values.get("AgentChatRoom") == previous


def test_login_startup_write_failure_does_not_report_enabled(monkeypatch, tmp_path) -> None:
    import types

    def deny(*_args):
        raise PermissionError("denied")

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_READ=1, KEY_SET_VALUE=2, CreateKeyEx=deny
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr("agentchatroom.gui_autostart.is_temporary_executable", lambda _p: False)
    with pytest.raises(PermissionError):
        LoginStartup(executable=tmp_path / "agentchatroom.exe", frozen=True).enable()


def test_login_startup_detects_stale_executable_path(monkeypatch, tmp_path) -> None:
    startup = LoginStartup(executable=tmp_path / "new place" / "agentchatroom.exe", frozen=True)
    monkeypatch.setattr(startup, "read_command", lambda: r'"C:\old place\agentchatroom.exe" gui --autostart')
    assert startup.enabled()
    assert not startup.is_current()


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user startup registry only")
def test_live_windows_login_startup_round_trip_opt_in() -> None:
    executable = os.getenv("AGENTCHATROOM_TEST_STARTUP_EXE", "")
    if not executable:
        pytest.skip("set AGENTCHATROOM_TEST_STARTUP_EXE for the opt-in registry test")
    path = Path(executable).resolve()
    assert path.is_file()
    startup = LoginStartup(executable=path, frozen=True)
    assert not startup.enabled(), "pre-existing startup entry; do not overwrite it"
    try:
        startup.enable()
        assert startup.enabled() and startup.is_current()
    finally:
        startup.disable()
    assert not startup.enabled()


def test_chinese_room_event_formatting_and_state_separation() -> None:
    base = {"project_seq": 1, "created_at": "2026-09-23T01:00:00Z",
            "task_number": 203, "payload": {"actor": {"name": "Codex"}, "title": "中文日志"}}
    created = format_room_event({**base, "event_type": "task.created"}, "agentchatroom")
    assert "发布了任务 #203「中文日志」" in created
    assert "[agentchatroom]" in created
    assert "等待独立验收（尚未集成）" in format_room_event(
        {**base, "event_type": "task.completed"}, "agentchatroom")
    assert "已完成集成" in format_room_event(
        {**base, "event_type": "task.integration_completed"}, "agentchatroom")
    assert "主动离开" in format_room_event(
        {**base, "event_type": "agent.left"}, "agentchatroom")
    assert "重新接入" in format_room_event(
        {**base, "event_type": "agent.joined"}, "agentchatroom", reconnected=True)
    assert "显式接管" in format_room_event(
        {**base, "event_type": "task.reclaimed", "payload": {
            **base["payload"], "explicit_live_takeover": True,
        }}, "agentchatroom")


def test_room_event_fallback_and_redaction() -> None:
    event = {"event_type": "future.event", "created_at": "bad",
             "payload": {"title": "token=supersecret\nfoo"}}
    assert "future.event" in format_room_event(event, "项目")
    assert "事件 #7" in format_room_event({**event, "project_seq": 7}, "项目")
    assert "supersecret" not in format_room_event(
        {**event, "event_type": "task.created", "task_number": 1}, "项目")
    assert "supersecret" not in sanitize_gui_text("token=supersecret")
    assert "[路径已隐藏]" in sanitize_gui_text(r"failed C:\Users\Person\secret.txt")
    assert format_service_line("Application startup complete") == "服务初始化完成。"


def test_gui_suppresses_routine_mcp_requests_and_hides_transport_identifiers() -> None:
    assert format_service_line(
        'INFO: 127.0.0.1:60000 - "POST /mcp HTTP/1.1" 200 OK'
    ) is None
    assert format_service_line(
        'INFO: HTTP Request: POST http://127.0.0.1:8765/mcp "HTTP/1.1 200 OK"'
    ) is None
    assert format_service_line("Received request of type CallToolRequest") is None
    assert format_service_line("Created MCP session ID: a1234567-b123-c123-d123-e123456789ab") is None
    assert format_service_line("Terminating session: 27ecf87bea34b5b8ffcba4a3d6df6c9") is None
    assert format_service_line("Session 27ecf87bea34b5b8ffcba4a3d6df6c9 idle timeout") is None
    assert format_service_line("Transport for session 27ecf87bea34b5b8ffcba4a3d6df6c9 is gone: keepalive") is None
    assert format_service_line("StreamableHTTP session manager started") is None
    assert format_service_line("Adopted reaped MCP session fd1841d7889946c2 as 57fa3d95") is None
    assert format_service_line("INFO: routine library detail") is None
    assert "27ecf87bea34b5b8ffcba4a3d6df6c9" not in sanitize_gui_text(
        "Session 27ecf87bea34b5b8ffcba4a3d6df6c9 disconnected"
    )
    assert "服务错误" in format_service_line(
        "Terminating session: 27ecf87bea34b5b8ffcba4a3d6df6c9 failed"
    )
    assert "服务错误" in format_service_line("MCP session ID rotation failed")
    assert format_service_line(
        'INFO: 127.0.0.1:60000 - "POST /mcp HTTP/1.1" 500 Internal Server Error'
    ) == "服务接口请求失败（HTTP 500）；详情请查看服务日志。"
    sanitized = sanitize_gui_text("session_id=agent_1234567890abcdef")
    assert "agent_1234567890abcdef" not in sanitized
    assert "[已隐藏]" in sanitized
    assert "secretopaque" not in sanitize_gui_text("mcpSessionId=secretopaque")


def test_room_event_tail_baselines_and_delivers_each_new_event_once() -> None:
    class Service:
        def __init__(self):
            self.events = [{"project_seq": 1, "event_type": "agent.joined", "payload": {"name": "Old"}}]

        def list_projects(self):
            return [{"id": "p", "name": "测试项目"}]

        def list_events(self, _project, *, after, limit):
            rows = [e for e in self.events if e["project_seq"] > after][:limit]
            return {"events": rows, "latest_cursor": self.events[-1]["project_seq"]}

        def get_task(self, _project, _task):
            return {"title": "新任务"}

    service = Service()
    sink = Queue()
    tail = RoomEventTail.__new__(RoomEventTail)
    tail.service, tail.sink, tail.cursors, tail.replaced_sessions = service, sink, {}, set()
    tail.poll_once()
    assert sink.empty()
    service.events.append({"project_seq": 2, "event_type": "task.created", "task_number": 204,
                           "task_id": "t", "created_at": "2026-09-23T01:00:00Z", "payload": {}})
    tail.poll_once()
    tail.poll_once()
    assert sink.qsize() == 1
    assert "发布了任务 #204「新任务」" in sink.get_nowait()[1]


def test_room_event_tail_reads_real_persisted_stream(service, project, settings) -> None:
    sink = Queue()
    tail = RoomEventTail(settings, sink, threading.Event(), service=service)
    tail.poll_once()  # existing project history is only a baseline
    assert sink.empty()
    with service.database.connect(write=True) as connection:
        service._emit(connection, project["id"], "agent.joined", payload={"name": "测试 Agent"})
        service._emit(connection, project["id"], "task.created", payload={
            "task_number": 204, "title": "开机启动"})
        service._emit(connection, project["id"], "agent.left", payload={"name": "测试 Agent"})
    tail.poll_once()
    lines = [sink.get_nowait()[1] for _ in range(3)]
    assert "测试 Agent 接入 Room" in lines[0]
    assert "发布了任务 #204「开机启动」" in lines[1]
    assert "测试 Agent 主动离开 Room" in lines[2]


def test_room_presence_timeout_is_not_reported_as_active_leave() -> None:
    class Service:
        state = "online"

        def list_projects(self):
            return [{"id": "p", "name": "测试项目"}]

        def list_session_presence(self, _project):
            return [{"id": "s", "name": "MiniMax", "status": self.state,
                     "left_at": None}]

    service = Service()
    sink = Queue()
    tail = RoomEventTail.__new__(RoomEventTail)
    tail.service, tail.sink, tail.presence = service, sink, {}
    tail.poll_presence_once()
    service.state = "offline"
    tail.poll_presence_once()
    tail.poll_presence_once()
    assert sink.qsize() == 1
    message = sink.get_nowait()[1]
    assert "心跳/活动超时" in message
    assert "不是主动离开" in message


def test_entry_app_routes_service_subcommands_to_cli(monkeypatch) -> None:
    import agentchatroom.cli as cli_module
    import agentchatroom.stdio_runtime as stdio_runtime

    entry_app = _load_entry_app()
    calls: list[str] = []
    monkeypatch.setattr(stdio_runtime, "prepare_standard_streams", lambda: None)
    monkeypatch.setattr(cli_module, "main", lambda: calls.append("cli"))
    monkeypatch.setattr(sys, "argv", ["agentchatroom.exe", "serve"])

    entry_app.main()

    assert calls == ["cli"]


def test_log_tail_recovers_after_a_transient_read_failure(tmp_path) -> None:
    """#324: one unreadable read must not end the log panel for the session.

    A share or an antivirus can take the file away between the stat and the
    readline. The thread used to die there, silently, and the panel simply
    stopped updating.
    """
    log = tmp_path / "server.log"
    log.write_text("INFO: Uvicorn running on http://127.0.0.1:8765\n", encoding="utf-8")

    sink: Queue = Queue()
    stop_event = threading.Event()
    tail = LogTail(log, sink, stop_event)

    original_open = Path.open
    fail_once = {"armed": True}

    def patched(self, *args, **kwargs):
        if self == log and fail_once["armed"] and args and args[0] == "r":
            fail_once["armed"] = False
            raise PermissionError(13, "used by another process", str(self))
        return original_open(self, *args, **kwargs)

    Path.open = patched  # type: ignore[method-assign]
    try:
        worker = threading.Thread(target=tail.run, daemon=True)
        worker.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            if any("稍后自动重试" in str(item) for item in list(sink.queue)):
                break
            time.sleep(0.05)
        stop_event.set()
        worker.join(timeout=5)
    finally:
        Path.open = original_open  # type: ignore[method-assign]

    messages = [str(item) for item in list(sink.queue)]
    assert any("稍后自动重试" in message for message in messages), messages
    assert worker.is_alive() is False, "the tail thread died instead of retrying"


def test_log_tail_reports_an_unreadable_file_once_per_outage(tmp_path) -> None:
    """A share that stays unreachable must not fill the panel with repeats."""
    log = tmp_path / "server.log"
    log.write_text("INFO: line\n", encoding="utf-8")
    sink: Queue = Queue()

    tail = LogTail(log, sink, threading.Event())

    original_open = Path.open

    def always_denied(self, *args, **kwargs):
        raise PermissionError(13, "denied", str(self))

    Path.open = always_denied  # type: ignore[method-assign]
    try:
        for _ in range(5):
            assert tail._open() is None
    finally:
        Path.open = original_open  # type: ignore[method-assign]

    notices = [str(item) for item in list(sink.queue)]
    assert len(notices) == 1, notices


def test_a_second_exit_request_while_stopping_is_ignored() -> None:
    """#324: the double tray quit must not reach the stop-confirmation dialog.

    Reaching it would ask the user to confirm a stop that already finished,
    because the second stop_detached_server call has nothing left to stop and
    answers stopped=False / not_running.
    """
    calls: list[str] = []

    class FakeWindow:
        action_active = False
        pending_exit_after_stop = False

        def __init__(self) -> None:
            self.tray = object()

        def _guard(self) -> bool:
            if self.action_active or self.pending_exit_after_stop:
                return True
            calls.append("proceeded")
            return False

    window = FakeWindow()
    # The guard both exit entries start with, verbatim.
    assert window._guard() is False
    window.action_active = True
    assert window._guard() is True
    window.action_active = False
    window.pending_exit_after_stop = True
    assert window._guard() is True
    assert calls == ["proceeded"], "a guarded request still reached the dialog"
