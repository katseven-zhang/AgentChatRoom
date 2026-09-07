"""Unit and anti-regression tests for GUI shell, ServerTarget, and client configuration.

Covers acceptance criteria for Task #73:
1. ServerTarget local/remote URL normalization and error rejection.
2. Remote target probe failure path without running a real server or starting local child process.
3. Version handshake (/api/v1/version) with zero authentication assumption.
4. User-level client configuration loading, persistence, and isolation from checkout config.
5. Dynamic resolution of ServerTarget from Settings without hardcoded localhost literals.
6. Shell JS API folder picker bridge and close action mapping.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agentchatroom.client_config import (
    ClientConfig,
    ProbeResult,
    ServerTarget,
    TargetError,
    load_client_config,
    resolve_server_target,
    save_client_config,
)
from agentchatroom.config import Settings
from agentchatroom.gui import (
    CLOSE,
    KEEP_RUNNING,
    STAY,
    STOP_AND_CLOSE,
    close_action,
)
from agentchatroom.shell import (
    ShellJsApi,
    prompt_close_action_win32,
)


def test_server_target_local_normalization():
    target = ServerTarget.local("http://127.0.0.1:8765/")
    assert target.mode == "local"
    assert target.base_url == "http://127.0.0.1:8765"

    target2 = ServerTarget.local("127.0.0.1:9000")
    assert target2.mode == "local"
    assert target2.base_url == "http://127.0.0.1:9000"


def test_server_target_remote_normalization():
    target = ServerTarget.remote("https://remote.example.com:8443/room")
    assert target.mode == "remote"
    assert target.base_url == "https://remote.example.com:8443"

    target_http = ServerTarget.remote("http://my-server.local:8000")
    assert target_http.mode == "remote"
    assert target_http.base_url == "http://my-server.local:8000"


def test_server_target_invalid_url_rejection():
    with pytest.raises(TargetError, match="cannot be empty"):
        ServerTarget.remote("")

    with pytest.raises(TargetError, match="Unsupported URL scheme"):
        ServerTarget.remote("ftp://server.example.com")

    with pytest.raises(TargetError, match="Invalid server target address"):
        ServerTarget.remote("http://")


def test_remote_target_probe_failure_path_without_server():
    """Anti-regression test for Task #73: Probing an unreachable remote address
    must fail cleanly with structured ProbeResult, without launching local processes
    or attempting full remote connection flows.
    """
    # Pick an unbound local port that is guaranteed closed
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]

    target = ServerTarget.remote(f"http://127.0.0.1:{closed_port}")
    probe = target.probe(timeout=1.0)

    assert probe.ok is False
    assert probe.status_code == 0
    assert probe.error is not None
    assert "Connection failed" in probe.error or "Probe failed" in probe.error


def test_server_target_probe_success_with_mock_version_endpoint():
    """Verify probe handshake succeeds against /api/v1/version."""
    class MockVersionHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/v1/version":
                payload = json.dumps({
                    "version": "0.2.2",
                    "schema_version": 7,
                    "product_name": "AgentChatRoom",
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), MockVersionHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        target = ServerTarget.local(f"http://127.0.0.1:{port}")
        probe = target.probe(timeout=2.0)
        assert probe.ok is True
        assert probe.version == "0.2.2"
        assert probe.schema_version == 7
        assert probe.product_name == "AgentChatRoom"
        assert probe.status_code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_server_target_probe_handles_auth_challenge_with_zero_assumption():
    """Verify probe records HTTP 401 without crashing, proving shell zero auth assumption."""
    class MockAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Login required"}}')

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), MockAuthHandler)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        target = ServerTarget.remote(f"http://127.0.0.1:{port}")
        probe = target.probe(timeout=2.0)
        assert probe.ok is False
        assert probe.status_code == 401
        assert "Login required" in (probe.error or "")
    finally:
        server.shutdown()
        server.server_close()


def test_user_client_config_persistence_and_isolation(tmp_path: Path):
    config_file = tmp_path / "client.toml"
    assert not config_file.exists()

    # Default should be local
    default_cfg = load_client_config(config_file)
    assert default_cfg.mode == "local"
    assert default_cfg.remote_url == ""

    # Save custom config
    custom_cfg = ClientConfig(mode="remote", remote_url="https://remote.agentchatroom.net:8443")
    saved_path = save_client_config(custom_cfg, config_file)
    assert saved_path.exists()

    # Reload from disk
    loaded_cfg = load_client_config(config_file)
    assert loaded_cfg.mode == "remote"
    assert loaded_cfg.remote_url == "https://remote.agentchatroom.net:8443"


def test_resolve_server_target_without_hardcoded_literals(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, host="127.0.0.1", port=9123)
    target = resolve_server_target(settings)
    assert target.mode == "local"
    assert target.base_url == "http://127.0.0.1:9123"

    # Override port
    target_custom_port = resolve_server_target(settings, effective_port=9999)
    assert target_custom_port.base_url == "http://127.0.0.1:9999"

    # Remote mode resolution
    remote_cfg = ClientConfig(mode="remote", remote_url="http://remote.host:8080")
    target_remote = resolve_server_target(settings, remote_cfg)
    assert target_remote.mode == "remote"
    assert target_remote.base_url == "http://remote.host:8080"


def test_shell_close_action_semantics():
    assert close_action(service_running=False, user_choice=None) == CLOSE
    assert close_action(service_running=True, user_choice="stop") == STOP_AND_CLOSE
    assert close_action(service_running=True, user_choice="keep") == KEEP_RUNNING
    assert close_action(service_running=True, user_choice="cancel") == STAY
    assert close_action(service_running=True, user_choice=None) == STAY


def test_shell_js_api_pick_directory_safe_without_window():
    api = ShellJsApi()
    assert api.pick_directory() is None


def test_packaged_onedir_executables_smoke():
    import os
    import subprocess
    dist_dir = Path(__file__).resolve().parent.parent / "dist" / "agentchatroom"
    exe = dist_dir / ("agentchatroom.exe" if os.name == "nt" else "agentchatroom")

    if not exe.exists():
        pytest.skip("Packaged onedir bundle not built in this environment")

    # The single-instance mutex makes any child exe invocation hand off to the
    # running instance instead of executing, so the smoke test is only
    # meaningful when no other instance is already running on this machine.
    running = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq agentchatroom.exe"],
        capture_output=True, text=True, encoding="gbk", errors="replace", check=False,
    )
    if "agentchatroom.exe" in (running.stdout or ""):
        pytest.skip("An agentchatroom.exe instance is already running")

    # User mandate for Task #73: exactly one exe carrying start, stop, and
    # frontend management; the former gui/mcp entries must no longer exist.
    suffix = ".exe" if os.name == "nt" else ""
    for legacy in ("agentchatroom-gui", "agentchatroom-mcp"):
        assert not (dist_dir / f"{legacy}{suffix}").exists(), legacy

    # GUI dispatch smoke: bare option-style invocation reaches the shell
    # parser (double-click default), so `--help` shows the GUI shell flags.
    completed = subprocess.run(
        [str(exe), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    assert "AgentChatRoom GUI Shell" in completed.stdout

    # CLI dispatch smoke: explicit subcommands reach the console CLI parser.
    completed = subprocess.run(
        [str(exe), "serve", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    assert "usage: agentchatroom serve" in completed.stdout

    # MCP stdio dispatch smoke: `agentchatroom.exe mcp` answers initialize.
    p = subprocess.Popen(
        [str(exe), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    init_msg = (
        json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest-smoke", "version": "1.0"},
            },
        })
        + "\n"
    )
    stdout, _ = p.communicate(init_msg, timeout=30)
    p.kill()
    assert "jsonrpc" in stdout
    assert "protocolVersion" in stdout


def test_packaging_spec_single_executable_dispatch():
    """Task #73 (user revised): the spec builds exactly one windowed exe whose
    entry dispatches gui / mcp / console-CLI modes instead of three exes."""
    import os
    root = Path(__file__).resolve().parent.parent
    spec_text = (root / "agentchatroom.spec").read_text(encoding="utf-8")
    entry_text = (root / "packaging" / "entry_app.py").read_text(encoding="utf-8")

    assert spec_text.count("Analysis(") == 1
    assert spec_text.count("EXE(") == 1
    assert spec_text.count("COLLECT(") == 1
    assert "name='agentchatroom'" in spec_text
    # Windowed panel: double-click opens the GUI without a console window.
    assert "console=False" in spec_text
    assert "packaging/entry_app.py" in spec_text

    # One entry dispatches every delivery mode from the single exe.
    assert "agentchatroom.shell" in entry_text
    assert "agentchatroom.mcp_server" in entry_text
    assert "agentchatroom.cli" in entry_text

    suffix = ".exe" if os.name == "nt" else ""
    for legacy_entry in ("entry_gui.py", "entry_cli.py", "entry_mcp.py"):
        assert not (root / "packaging" / legacy_entry).exists(), legacy_entry


# ---------------------------------------------------------------------------
# Task #73 review fixes: config propagation, probe gate, remote P1 boundary,
# zero-port rejection, redacted shell output, actual listening address.
# ---------------------------------------------------------------------------

def _install_run_shell_stubs(monkeypatch, tmp_path, target, client_config=None):
    from agentchatroom import shell as shell_module

    monkeypatch.setattr(shell_module, "enforce_single_instance", lambda *a, **k: True)
    settings = Settings(data_dir=tmp_path, host="127.0.0.1", port=8765)
    monkeypatch.setattr(shell_module, "load_settings", lambda config_path=None: settings)
    monkeypatch.setattr(
        shell_module,
        "load_client_config",
        lambda config_path=None: client_config or ClientConfig(),
    )
    monkeypatch.setattr(shell_module, "resolve_server_target", lambda *a, **k: target)
    return shell_module


class _RecordingShell:
    launched = False

    def __init__(self, settings, target, client_config):
        self.settings = settings
        self.target = target
        self.client_config = client_config
        self.controller = type(
            "Controller",
            (),
            {"log_path": Path(settings.data_dir) / "server.log"},
        )()

    def ensure_server_running(self):
        return None

    def launch(self, debug=False):
        _RecordingShell.launched = True
        return 0


def test_service_controller_start_passes_explicit_config_path(monkeypatch, tmp_path):
    from agentchatroom import gui as gui_module

    config_path = tmp_path / "config.toml"
    config_path.write_text("[server]\nport = 8765\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path, config_path=config_path, host="127.0.0.1", port=8765
    )
    captured = {}

    def fake_start_detached_server(effective_settings, host, port, config_path_arg):
        captured["config_path"] = config_path_arg
        return {"started": True}

    monkeypatch.setattr(gui_module, "start_detached_server", fake_start_detached_server)
    gui_module.ServiceController(settings).start(9001)
    assert captured["config_path"] == str(config_path)


def test_run_shell_local_probe_failure_blocks_window(monkeypatch, tmp_path, capsys):
    from agentchatroom.client_config import ProbeResult

    class Target:
        mode = "local"
        base_url = "http://127.0.0.1:8765"

        def probe(self, timeout=3.0):
            return ProbeResult(ok=False, status_code=0, error="Connection failed")

    shell_module = _install_run_shell_stubs(
        monkeypatch, tmp_path, Target(), ClientConfig(auto_start=True)
    )
    _RecordingShell.launched = False
    monkeypatch.setattr(shell_module, "GuiShell", _RecordingShell)
    assert shell_module.run_shell() == 1
    assert not _RecordingShell.launched
    assert "Error connecting to target" in capsys.readouterr().err


def test_run_shell_local_auth_challenge_still_opens_panel(monkeypatch, tmp_path):
    from agentchatroom.client_config import ProbeResult

    class Target:
        mode = "local"
        base_url = "http://127.0.0.1:8765"

        def probe(self, timeout=3.0):
            return ProbeResult(ok=False, status_code=401, error="HTTP 401")

    shell_module = _install_run_shell_stubs(
        monkeypatch, tmp_path, Target(), ClientConfig(auto_start=True)
    )
    _RecordingShell.launched = False
    monkeypatch.setattr(shell_module, "GuiShell", _RecordingShell)
    assert shell_module.run_shell() == 0
    assert _RecordingShell.launched


def test_run_shell_remote_target_never_opens_window_p1(monkeypatch, tmp_path, capsys):
    from agentchatroom.client_config import ProbeResult

    class Target:
        mode = "remote"
        base_url = "https://review.invalid"

        def __init__(self, probe_result=None):
            self._probe_result = probe_result

        def probe(self, timeout=3.0):
            return self._probe_result or ProbeResult(ok=True, version="1.0")

    shell_module = _install_run_shell_stubs(monkeypatch, tmp_path, Target())
    _RecordingShell.launched = False
    monkeypatch.setattr(shell_module, "GuiShell", _RecordingShell)

    # Reachable remote: probe succeeds but P1 never opens a management window.
    assert shell_module.run_shell() == 0
    assert not _RecordingShell.launched
    assert "S1-S3" in capsys.readouterr().out

    # Unreachable remote: probe failure path with a non-zero exit.
    shell_module2 = _install_run_shell_stubs(
        monkeypatch, tmp_path, Target(ProbeResult(ok=False, status_code=0, error="no route"))
    )
    _RecordingShell.launched = False
    monkeypatch.setattr(shell_module2, "GuiShell", _RecordingShell)
    assert shell_module2.run_shell() == 1
    assert not _RecordingShell.launched


def test_run_shell_rejects_zero_port(monkeypatch, tmp_path):
    import pytest

    from agentchatroom.gui import PortError

    shell_module = _install_run_shell_stubs(monkeypatch, tmp_path, object())
    with pytest.raises(PortError):
        shell_module.run_shell(port=0)


def test_run_shell_redacts_sensitive_probe_errors(monkeypatch, tmp_path, capsys):
    from agentchatroom.client_config import ProbeResult

    class Target:
        mode = "local"
        base_url = "http://127.0.0.1:8765"

        def probe(self, timeout=3.0):
            return ProbeResult(
                ok=False,
                status_code=0,
                error="handshake failed token=supersecret123",
            )

    shell_module = _install_run_shell_stubs(
        monkeypatch, tmp_path, Target(), ClientConfig(auto_start=True)
    )
    _RecordingShell.launched = False
    monkeypatch.setattr(shell_module, "GuiShell", _RecordingShell)
    assert shell_module.run_shell() == 1
    err = capsys.readouterr().err
    assert "supersecret123" not in err
    assert "token=***" in err


def test_run_shell_local_target_follows_actual_listening_url(monkeypatch, tmp_path):
    from agentchatroom.client_config import ProbeResult, ServerTarget

    log_path = tmp_path / "server.log"
    log_path.write_text(
        "INFO:     Uvicorn running on http://127.0.0.1:9000 (Press CTRL+C to quit)\n",
        encoding="utf-8",
    )

    class Target:
        mode = "local"
        base_url = "http://127.0.0.1:8765"
        probed_urls = []

        def probe(self, timeout=3.0):
            Target.probed_urls.append(self.base_url)
            return ProbeResult(ok=True, version="1.0")

    shell_module = _install_run_shell_stubs(
        monkeypatch, tmp_path, Target(), ClientConfig(auto_start=True)
    )
    _RecordingShell.launched = False
    monkeypatch.setattr(shell_module, "GuiShell", _RecordingShell)

    original_probe = ServerTarget.probe

    def fake_probe(self, timeout=3.0):
        Target.probed_urls.append(self.base_url)
        return ProbeResult(ok=True, version="1.0")

    monkeypatch.setattr(ServerTarget, "probe", fake_probe)
    try:
        assert shell_module.run_shell() == 0
    finally:
        monkeypatch.setattr(ServerTarget, "probe", original_probe, raising=False)
    assert "http://127.0.0.1:9000" in Target.probed_urls[-1]
    assert _RecordingShell.launched




# ---------------------------------------------------------------------------
# Task #84: manual service control — shell top bar, placeholder page,
# js_api start/stop/status adapter, and the auto_start client option.
# ---------------------------------------------------------------------------

def test_client_config_auto_start_default_and_roundtrip(tmp_path):
    from agentchatroom.client_config import save_client_config

    default_config = load_client_config()
    assert default_config.auto_start is False

    toml_text = ClientConfig(mode="local").to_toml()
    assert "auto_start = false" in toml_text

    config_path = tmp_path / "client.toml"
    save_client_config(
        ClientConfig(mode="local", auto_start=True), config_path=config_path
    )
    reloaded = load_client_config(config_path=config_path)
    assert reloaded.auto_start is True


def _build_manual_shell(tmp_path, *, log_url=None):
    """Build a real GuiShell with a stubbed controller for js_api tests."""
    from agentchatroom import shell as shell_module

    settings = Settings(data_dir=tmp_path, host="127.0.0.1", port=8765)
    shell = shell_module.GuiShell(
        settings, ServerTarget.local("http://127.0.0.1:8765"), ClientConfig()
    )

    class StubController:
        def __init__(self):
            self.log_path = tmp_path / "server.log"
            self.start_results = [{"started": True, "pid": 4242}]
            self.stop_results = [{"stopped": True}]
            self.started_ports = []
            self.stopped = False

        def is_running(self):
            return self.stopped is False

        def running_pid(self):
            return 4242 if self.stopped is False else None

        def start(self, port):
            self.started_ports.append(port)
            return self.start_results.pop(0)

        def stop(self):
            self.stopped = True
            return self.stop_results.pop(0)

    shell.controller = StubController()
    if log_url:
        shell.controller.log_path.write_text(
            f"INFO:     Uvicorn running on {log_url} (Press CTRL+C to quit)\n",
            encoding="utf-8",
        )
    return shell


def test_shell_js_api_start_service_rejects_invalid_port():
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(Path("."))
    api = shell_module.ShellJsApi()
    api.bind_shell(shell)
    result = api.start_service("0")
    assert result["ok"] is False
    assert "端口必须" in result["error"]
    assert shell.controller.started_ports == []


def test_shell_js_api_start_service_rejects_occupied_port(monkeypatch):
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(Path("."))
    api = shell_module.ShellJsApi()
    api.bind_shell(shell)
    monkeypatch.setattr(shell_module, "port_is_free", lambda host, port: False)
    result = api.start_service("8765")
    assert result["ok"] is False
    assert "已被占用" in result["error"]
    assert shell.controller.started_ports == []


def test_shell_js_api_start_service_success_persists_and_navigates(tmp_path, monkeypatch):
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(tmp_path, log_url="http://127.0.0.1:9010")
    api = shell_module.ShellJsApi()
    api.bind_shell(shell)

    navigated = []
    persisted = []

    class FakeWindow:
        def load_url(self, url):
            navigated.append(url)

        def load_html(self, content):
            navigated.append("html:" + content[:20])

    api.set_window(FakeWindow())
    monkeypatch.setattr(
        shell_module,
        "update_config_port",
        lambda path, port: persisted.append((path, port)),
    )

    result = api.start_service("9010")
    assert result["ok"] is True
    assert result["pid"] == 4242
    assert shell.controller.started_ports == [9010]
    assert persisted and persisted[0][1] == 9010
    assert navigated == ["http://127.0.0.1:9010"]
    assert shell.target.base_url == "http://127.0.0.1:9010"


def test_shell_js_api_stop_service_returns_to_placeholder(tmp_path):
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(tmp_path)
    api = shell_module.ShellJsApi()
    api.bind_shell(shell)

    loaded_html = []

    class FakeWindow:
        def load_html(self, content):
            loaded_html.append(content)

    api.set_window(FakeWindow())
    result = api.stop_service()
    assert result["ok"] is True
    assert result["running"] is False
    assert len(loaded_html) == 1
    assert "服务未启动" in loaded_html[0]
    assert "启动服务" in loaded_html[0]


def test_shell_js_api_stop_service_reports_not_running(tmp_path):
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(tmp_path)
    shell.controller.stop_results = [{"stopped": False, "reason": "not_running"}]
    api = shell_module.ShellJsApi()
    api.bind_shell(shell)
    result = api.stop_service()
    assert result["ok"] is True
    assert result["reason"] == "not_running"


def test_shell_js_api_get_status_reports_running(tmp_path):
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(tmp_path, log_url="http://127.0.0.1:9010")
    shell.target = ServerTarget.local("http://127.0.0.1:9010")
    api = shell_module.ShellJsApi()
    api.bind_shell(shell)
    status = api.get_status()
    assert status["running"] is True
    assert status["pid"] == 4242
    assert status["url"] == "http://127.0.0.1:9010"
    # The reported port must follow the actual listening address, not the
    # stale configured port, so the top bar input never disagrees with the
    # service the user is talking to.
    assert status["port"] == 9010


def test_placeholder_and_topbar_carry_manual_controls_without_literals():
    from agentchatroom import shell as shell_module

    source = Path(shell_module.__file__).read_text(encoding="utf-8")
    assert "服务未启动" in shell_module.PLACEHOLDER_HTML
    assert "启动服务" in shell_module.PLACEHOLDER_HTML
    assert "shell-port" in shell_module.PLACEHOLDER_HTML
    assert "启动服务" in shell_module.TOPBAR_JS
    assert "停止服务" in shell_module.TOPBAR_JS
    assert "start_service" in shell_module.TOPBAR_JS
    assert "stop_service" in shell_module.TOPBAR_JS
    assert "get_status" in shell_module.TOPBAR_JS
    assert "agentchatroom-shell-bar-active" in shell_module.TOPBAR_JS
    assert "calc(100vh - 46px)" in shell_module.TOPBAR_JS
    # The shell must stay free of hardcoded network targets.
    for literal in ("127.0.0.1", "localhost", "http://", "8765"):
        assert literal not in source, literal


def test_run_shell_manual_default_skips_auto_start(monkeypatch, tmp_path):
    stub_target = type("StubTarget", (), {"mode": "local"})()
    shell_module = _install_run_shell_stubs(monkeypatch, tmp_path, stub_target)

    events = []

    class ManualShell:
        def __init__(self, settings, target, client_config):
            self.settings = settings
            self.target = target
            self.client_config = client_config

        def ensure_server_running(self):
            events.append("ensure")

        def launch(self, debug=False):
            events.append("launch")
            return 0

    monkeypatch.setattr(shell_module, "GuiShell", ManualShell)
    assert shell_module.run_shell() == 0
    assert events == ["launch"]


def test_run_shell_auto_start_option_restores_legacy_flow(monkeypatch, tmp_path):
    from agentchatroom.client_config import ProbeResult, ServerTarget

    class Target:
        mode = "local"
        base_url = "http://127.0.0.1:8765"

        def probe(self, timeout=3.0):
            return ProbeResult(ok=True, version="1.0")

    shell_module = _install_run_shell_stubs(
        monkeypatch, tmp_path, Target(), ClientConfig(auto_start=True)
    )
    events = []

    class AutoShell:
        def __init__(self, settings, target, client_config):
            self.controller = type(
                "Controller", (), {"log_path": tmp_path / "server.log"}
            )()
            self.target = target

        def ensure_server_running(self):
            events.append("ensure")

        def launch(self, debug=False):
            events.append("launch")
            return 0

    monkeypatch.setattr(shell_module, "GuiShell", AutoShell)
    assert shell_module.run_shell() == 0
    assert events == ["ensure", "launch"]


# ---------------------------------------------------------------------------
# Task #85: minimize-to-tray. #86: default window 1440x900.
# ---------------------------------------------------------------------------

def test_panel_tray_image_is_generated_programmatically():
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(Path("."))
    tray = shell_module.PanelTray(shell)
    image = tray._image()
    assert image.size == (64, 64)
    assert image.mode == "RGBA"


def test_panel_tray_start_starts_detached_icon(monkeypatch):
    import sys
    import types

    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(Path("."))
    tray = shell_module.PanelTray(shell)

    recorded = []

    class FakeIcon:
        def __init__(self, name, image, title, menu=None):
            recorded.append(("icon", name, title))
            self.menu = menu

        def run_detached(self):
            recorded.append(("detached",))

        def stop(self):
            recorded.append(("stop",))

    class FakeMenu:
        def __init__(self, *items):
            recorded.append(("menu", items))

    class FakeMenuItem:
        def __init__(self, text, handler, default=False, **kwargs):
            recorded.append(("item", text, default))

    fake_module = types.ModuleType("pystray")
    fake_module.Icon = FakeIcon
    fake_module.Menu = FakeMenu
    fake_module.MenuItem = FakeMenuItem
    monkeypatch.setitem(sys.modules, "pystray", fake_module)

    assert tray.available() is True
    assert tray.start() is True
    assert tray.started is True
    kinds = [entry[0] for entry in recorded]
    assert "detached" in kinds
    items = [entry for entry in recorded if entry[0] == "item"]
    # left-click default item restores the panel; a separate exit item exists
    assert any(entry[1] == "打开面板" and entry[2] for entry in items)
    assert any(entry[1] == "退出" and not entry[2] for entry in items)


def test_panel_tray_start_falls_back_when_unavailable(monkeypatch):
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(Path("."))
    tray = shell_module.PanelTray(shell)
    monkeypatch.setattr(tray, "available", lambda: False)
    assert tray.start() is False
    assert tray.started is False
    assert tray.icon is None


def test_on_window_minimized_hides_only_when_tray_started():
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(Path("."))
    hidden = []

    class FakeWindow:
        def hide(self):
            hidden.append(True)

    shell.window = FakeWindow()

    shell.tray.started = False
    shell.on_window_minimized()
    assert hidden == []

    shell.tray.started = True
    shell.on_window_minimized()
    assert hidden == [True]


def test_tray_quit_respects_closing_semantics():
    from agentchatroom import shell as shell_module

    shell = _build_manual_shell(Path("."))
    tray = shell_module.PanelTray(shell)
    tray.started = True

    stopped = []
    destroyed = []

    class FakeIcon:
        def stop(self):
            stopped.append(True)

    class FakeWindow:
        def destroy(self):
            destroyed.append(True)

    tray.icon = FakeIcon()
    shell.window = FakeWindow()
    monkeypatch_flags = {"allowed": True}
    monkeypatch = monkeypatch_flags  # placeholder to keep flake calm

    shell.on_window_closing = lambda: monkeypatch_flags["allowed"]
    tray.quit_from_tray()
    assert stopped == [True]
    assert destroyed == [True]

    stopped.clear()
    destroyed.clear()
    monkeypatch_flags["allowed"] = False
    tray.started = True
    tray.quit_from_tray()
    assert stopped == []
    assert destroyed == []
    assert tray.started is True


def test_default_panel_window_is_1440x900():
    from agentchatroom import shell as shell_module

    source = Path(shell_module.__file__).read_text(encoding="utf-8")
    assert "width=1440" in source
    assert "height=900" in source
