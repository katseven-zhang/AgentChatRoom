from __future__ import annotations

import os
import socket
import sys
import threading
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from queue import Queue

import pytest

from agentchatroom.cli import build_parser, process_is_running, service_url
from agentchatroom.gui import (
    CLOSE,
    KEEP_RUNNING,
    LogTail,
    HostError,
    PortError,
    STAY,
    STOP_AND_CLOSE,
    ServiceController,
    action_after_worker_event,
    build_tray_menu_spec,
    button_states,
    close_action,
    config_file_path,
    format_worker_error,
    host_requires_management_auth,
    port_is_free,
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


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


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
        def __init__(self, name, image, title, menu):
            self.menu = menu
            self.visible = False

        def run_detached(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "pystray",
        types.SimpleNamespace(Menu=lambda *items: items, MenuItem=menu_item, Icon=Icon),
    )
    monkeypatch.setattr(gui_module, "tray_icon_image", lambda: object())

    icon = gui_module.create_tray_icon(actions.append)
    assert icon is not None
    assert icon.visible is True
    for _label, callback, _default in menu_items:
        callback(icon, None)
    assert actions == [action for _label, action, _default in build_tray_menu_spec()]


def test_gui_status_uses_full_width_row(monkeypatch, settings) -> None:
    try:
        import tkinter as tk
    except ImportError as error:
        pytest.skip(f"Tk unavailable: {error}")
    try:
        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")

    import agentchatroom.gui as gui_module

    windows = []
    monkeypatch.setattr(gui_module, "load_settings", lambda _config: settings)
    monkeypatch.setattr(gui_module, "create_tray_icon", lambda _action: None)
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: windows.append(self))

    gui_module.run_gui()
    window = windows[0]
    try:
        assert window.status_label.master is window.start_button.master.master
        assert window.status_label.pack_info()["side"] == "top"
        assert window.status_label.pack_info()["fill"] == "x"
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
        assert collect(1) == [("log", "startup line")]

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("second line\ntoken=hushhush\n")

        events = collect(2)
    finally:
        stop_event.set()
        tail.join(timeout=5)

    assert events == [("log", "second line"), ("log", "token=[REDACTED]")]
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
    assert port_is_free(*address) is True


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
