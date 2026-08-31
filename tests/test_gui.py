from __future__ import annotations

import socket
import sys
import threading
import time
import tomllib
from dataclasses import replace
from queue import Queue

import pytest

from agentchatroom.cli import build_parser, process_is_running
from agentchatroom.gui import (
    CLOSE,
    KEEP_RUNNING,
    LogTail,
    PortError,
    STAY,
    STOP_AND_CLOSE,
    ServiceController,
    button_states,
    close_action,
    config_file_path,
    port_is_free,
    redact_line,
    running_url_from_log,
    update_config_port,
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


def test_button_states_follow_service_and_action_state() -> None:
    assert button_states(service_running=False, action_active=False) == (True, False)
    assert button_states(service_running=True, action_active=False) == (False, True)
    assert button_states(service_running=False, action_active=True) == (False, False)
    assert button_states(service_running=True, action_active=True) == (False, False)


def test_close_action_maps_all_three_running_choices() -> None:
    assert close_action(service_running=False, user_choice="stop") == CLOSE
    assert close_action(service_running=False, user_choice="keep") == CLOSE
    assert close_action(service_running=False, user_choice=None) == CLOSE
    assert close_action(service_running=True, user_choice="stop") == STOP_AND_CLOSE
    assert close_action(service_running=True, user_choice="keep") == KEEP_RUNNING
    assert close_action(service_running=True, user_choice="cancel") == STAY
    assert close_action(service_running=True, user_choice=None) == STAY


def test_redact_line_masks_credentials() -> None:
    plain = 'INFO:     127.0.0.1:1 - "GET /health HTTP/1.1" 200'
    assert redact_line(plain) == plain
    assert redact_line("token=abc123.456") == "token=***"
    assert redact_line("token: abc123.456") == "token=***"
    assert redact_line("password=hunter2value") == "password=***"
    assert redact_line("api_key = value1234") == "api_key=***"
    assert redact_line("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ***"
    assert redact_line("client sent sk-proj-abcdefgh") == "client sent sk-***"
    assert "abc123.456" not in redact_line("token=abc123.456")
    assert "sk-proj-" not in redact_line("client sent sk-proj-abcdefgh")


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

    assert events == [("log", "second line"), ("log", "token=***")]
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


def test_gui_service_controller_reuses_cli_lifecycle(settings) -> None:
    controller = ServiceController(settings)
    port = available_port()

    assert controller.is_running() is False

    started = controller.start(port)
    pid = started["pid"]
    try:
        assert started["started"] is True
        assert controller.is_running() is True
        assert controller.running_pid() == pid
        assert process_is_running(pid)

        repeated = controller.start(port)
        assert repeated["already_running"] is True
        assert repeated["pid"] == pid
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
