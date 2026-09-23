"""Local GUI controller for starting and stopping the AgentChatRoom service.

The window is a thin adapter over the same lifecycle functions the CLI uses:
starting reuses the detached ``serve`` path (pid file, log file, health check)
and stopping reuses the normal stop plus Windows process-tree cleanup. No
second startup business logic lives here.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import sys
import threading
import tomllib
import webbrowser
from dataclasses import replace
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

from .cli import (
    process_is_running,
    service_url,
    start_detached_server,
    stop_detached_server,
)
from .config import Settings, load_settings

MIN_PORT = 1
MAX_PORT = 65535
_DIGITS_ONLY = re.compile(r"\A[0-9]+\Z")
_RUNNING_URL = re.compile(r"running on (https?://\S+)")

# Mirrors the server config rule: only loopback targets may bind without
# management authentication.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

CLOSE = "close"
STOP_AND_CLOSE = "stop_and_close"
KEEP_RUNNING = "keep_running"
STAY = "stay"


class PortError(ValueError):
    """Raised when the port input is not a usable TCP port."""


class HostError(ValueError):
    """Raised when the host input is not a usable listen address."""


def validate_port(raw: str) -> int:
    """Validate a user-supplied port string and return it as an integer."""
    text = str(raw).strip()
    if not _DIGITS_ONLY.fullmatch(text):
        raise PortError(f"端口必须是 {MIN_PORT}-{MAX_PORT} 之间的整数")
    port = int(text)
    if not MIN_PORT <= port <= MAX_PORT:
        raise PortError(f"端口必须在 {MIN_PORT}-{MAX_PORT} 之间")
    return port


def validate_host(raw: str) -> str:
    """Validate a user-supplied listen address and return it normalized."""
    text = str(raw).strip()
    if not text:
        raise HostError("监听地址不能为空（例如 127.0.0.1 或 0.0.0.0）")
    lowered = text.lower()
    if lowered in LOOPBACK_HOSTS:
        return lowered
    try:
        ipaddress.ip_address(text)
    except ValueError as error:
        raise HostError(
            f"监听地址必须是合法 IP（如 127.0.0.1、0.0.0.0、::），收到：{text}"
        ) from error
    return text


def host_requires_management_auth(host: str) -> bool:
    """Mirror the config rule: non-loopback binding requires management auth."""
    return str(host).strip().lower() not in LOOPBACK_HOSTS


def restart_steps(service_running: bool) -> tuple[str, ...]:
    """Return the worker steps behind the one-click restart button."""
    return ("stop", "start") if service_running else ("start",)


def resolve_frontend_url(running_url: str | None, host: str, port: int) -> str:
    """Prefer the address the server actually logged, fall back to config."""
    return running_url if running_url else service_url(host, port)


def port_is_free(host: str, port: int) -> bool:
    """Return True when nothing is listening on the address yet."""
    family = socket.AF_INET6 if ":" in (host or "") else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            # Match the POSIX server's restart behavior after TCP TIME_WAIT.
            # Windows SO_REUSEADDR can share a live listener, so do not enable it there.
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host or "127.0.0.1", port))
            probe.listen(1)
        except OSError:
            return False
    return True


def redact_line(line: str) -> str:
    """Mask credential-looking fragments before showing a log line.

    Delegates to the shared API policy so GUI tails match server.log and
    the admin_runtime log view (single redaction standard).
    """
    from .api import redact_log_line

    return redact_log_line(line)


def button_states(
    service_running: bool, action_active: bool
) -> tuple[bool, bool]:
    """Return (start_enabled, stop_enabled) for the current UI state."""
    if action_active:
        return (False, False)
    return (not service_running, service_running)


# Sink event kinds that end an in-flight start/restart/stop action (#191).
WORKER_TERMINAL_EVENTS = frozenset(
    {"started", "start_failed", "restart_aborted", "stopped", "stop_failed"}
)


def action_after_worker_event(action_active: bool, kind: str) -> bool:
    """Return the action_active flag after applying a worker sink event.

    Terminal worker events always clear the flag so the buttons re-enable;
    plain ``log`` lines never change it. Workers must therefore always emit a
    terminal event — including for unexpected exceptions — or the console
    stays stuck with every button disabled.
    """
    if kind in WORKER_TERMINAL_EVENTS:
        return False
    return action_active


def format_worker_error(error: BaseException) -> str:
    """Render a worker exception for the GUI log area (#191)."""
    if isinstance(error, SystemExit):
        return str(error)
    return f"{type(error).__name__}: {error}"


def close_action(service_running: bool, user_choice: str | None) -> str:
    """Map the close dialog outcome to a GUI close decision."""
    if not service_running:
        return CLOSE
    if user_choice == "stop":
        return STOP_AND_CLOSE
    if user_choice == "keep":
        return KEEP_RUNNING
    return STAY


def running_url_from_log(log_path: Path) -> str | None:
    """Return the newest actual listening address recorded in the server log."""
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = _RUNNING_URL.findall(content)
    return matches[-1] if matches else None


def config_file_path(settings: Settings) -> Path:
    """Return the config file backing the current effective settings."""
    if settings.config_path:
        return Path(settings.config_path)
    return settings.data_dir / "config.toml"


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        inner = ", ".join(_format_toml_value(item) for item in value)
        return f"[{inner}]"
    raise ValueError(f"Unsupported config value: {value!r}")


def update_config_values(
    config_path: Path,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Persist the chosen host/port into the local config file for later launches."""
    config_path = Path(config_path)
    document: dict[str, dict[str, Any]] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            loaded = tomllib.load(handle)
        document = {name: dict(values) for name, values in loaded.items()}
    server = dict(document.get("server", {}))
    if host is not None:
        server["host"] = host
    if port is not None:
        server["port"] = port
    rebuilt: dict[str, dict[str, Any]] = {}
    for name, values in document.items():
        rebuilt[name] = server if name == "server" else values
    if "server" not in rebuilt:
        rebuilt["server"] = server
    lines: list[str] = []
    for name, values in rebuilt.items():
        lines.append(f"[{name}]")
        for key, value in values.items():
            lines.append(f"{key} = {_format_toml_value(value)}")
        lines.append("")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines), encoding="utf-8")


def update_config_port(config_path: Path, port: int) -> None:
    """Persist the chosen port into the local config file for later launches."""
    update_config_values(config_path, port=port)


class ServiceController:
    """Adapter over the CLI detached-service lifecycle used by the GUI."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def pid_path(self) -> Path:
        return self.settings.data_dir / "server.pid"

    @property
    def log_path(self) -> Path:
        return self.settings.data_dir / "server.log"

    def running_pid(self) -> int | None:
        try:
            pid = int(self.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None
        return pid if process_is_running(pid) else None

    def is_running(self) -> bool:
        return self.running_pid() is not None

    def display_url(self, host: str, port: int) -> str:
        return service_url(host, port)

    def start(self, host: str, port: int) -> dict[str, Any]:
        effective = replace(self.settings, host=host, port=port)
        config_path = (
            str(self.settings.config_path) if self.settings.config_path else None
        )
        return start_detached_server(effective, host, port, config_path)

    def stop(self) -> dict[str, Any]:
        return stop_detached_server(self.settings)


class LogTail(threading.Thread):
    """Follow a log file and deliver redacted lines to a queue."""

    def __init__(
        self,
        path: Path,
        sink: Queue,
        stop_event: threading.Event,
        *,
        tail_lines: int = 200,
    ) -> None:
        super().__init__(daemon=True, name="agentchatroom-gui-log")
        self.path = Path(path)
        self.sink = sink
        self.stop_event = stop_event
        self.tail_lines = tail_lines

    def run(self) -> None:
        handle = None
        try:
            while not self.stop_event.is_set():
                if handle is None:
                    if not self.path.exists():
                        self.stop_event.wait(0.5)
                        continue
                    handle = self.path.open("r", encoding="utf-8", errors="replace")
                    for line in handle.readlines()[-self.tail_lines :]:
                        self.sink.put(("log", redact_line(line.rstrip("\r\n"))))
                    continue
                line = handle.readline()
                if line:
                    self.sink.put(("log", redact_line(line.rstrip("\r\n"))))
                else:
                    self.stop_event.wait(0.2)
        finally:
            if handle is not None:
                handle.close()


def _drain(sink: Queue) -> list[Any]:
    items = []
    while True:
        try:
            items.append(sink.get_nowait())
        except Empty:
            return items


def run_start_worker(
    sink: Queue,
    *,
    host: str,
    port: int,
    settings: Settings,
    start_fn: Callable[[str, int], dict[str, Any]],
    port_probe: Callable[[str, int], bool] | None = None,
) -> None:
    """Start the detached service; always emit a terminal sink event (#191)."""
    probe = port_probe if port_probe is not None else port_is_free
    try:
        if not probe(host, port):
            sink.put(
                (
                    "start_failed",
                    f"端口 {port} 已被占用：可能已有一个服务在运行，"
                    "请先停止或更换端口。",
                )
            )
            return
        result = start_fn(host, port)
        sink.put(("started", result))
        if host != settings.host or port != settings.port:
            try:
                update_config_values(
                    config_file_path(settings), host=host, port=port
                )
                sink.put(
                    (
                        "log",
                        f"{host}:{port} 已写入配置，后续启动将使用该配置。",
                    )
                )
            except (OSError, ValueError) as error:
                sink.put(("log", f"[配置写入失败] {error}"))
            if os.getenv("AGENTCHATROOM_PORT", "").strip() or os.getenv(
                "AGENTCHATROOM_HOST", ""
            ).strip():
                sink.put(
                    (
                        "log",
                        "检测到 AGENTCHATROOM_HOST/PORT 环境变量：其他启动入口仍以环境变量优先。",
                    )
                )
    except SystemExit as error:
        sink.put(("start_failed", format_worker_error(error)))
    except Exception as error:
        # Catch-all (#191): unexpected errors must clear action_active via a
        # terminal event and show the failure in the log area.
        sink.put(("start_failed", format_worker_error(error)))


def run_restart_worker(
    sink: Queue,
    *,
    is_running: Callable[[], bool],
    stop_fn: Callable[[], dict[str, Any]],
    start_body: Callable[[], None],
) -> None:
    """Stop then start; always emit a terminal sink event (#191)."""
    try:
        if restart_steps(is_running()) == ("stop", "start"):
            result = stop_fn()
            if not result.get("stopped"):
                sink.put(
                    (
                        "restart_aborted",
                        f"旧服务停止未完成：{result.get('reason')}",
                    )
                )
                return
            sink.put(("log", "旧服务已停止，正在重新启动..."))
        start_body()
    except SystemExit as error:
        sink.put(("restart_aborted", format_worker_error(error)))
    except Exception as error:
        # Not stop_failed: a restart failure is reported as restart_aborted.
        sink.put(("restart_aborted", format_worker_error(error)))


def run_stop_worker(
    sink: Queue,
    *,
    stop_fn: Callable[[], dict[str, Any]],
) -> None:
    """Stop the detached service; always emit a terminal sink event (#191)."""
    try:
        sink.put(("stopped", stop_fn()))
    except SystemExit as error:
        sink.put(("stop_failed", format_worker_error(error)))
    except Exception as error:
        sink.put(("stop_failed", format_worker_error(error)))


def build_tray_menu_spec() -> list[tuple[str, str, bool]]:
    """Return (label, action_key, is_default) rows for the tray menu."""
    return [
        ("打开控制台", "show", True),
        ("打开前端", "frontend", False),
        ("启动服务", "start", False),
        ("重启服务", "restart", False),
        ("停止服务", "stop", False),
        ("退出", "quit", False),
    ]


def tray_icon_image():
    """Draw the tray icon with Pillow; return None when Pillow is missing."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(0, 113, 227, 255))
    try:
        font = ImageFont.load_default(size=34)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((32, 33), "A", font=font, fill=(255, 255, 255, 255), anchor="mm")
    return image


def create_tray_icon(handle_action):
    """Build a detached pystray icon wired to ``handle_action``; None if unavailable.

    ``handle_action`` receives one action key from :func:`build_tray_menu_spec`;
    pystray invokes callbacks on its own thread, so handlers must marshal back
    to the Tk loop themselves.
    """
    try:
        import pystray
    except ImportError:
        return None
    image = tray_icon_image()
    if image is None:
        return None
    def menu_action(action_key: str):
        def invoke(icon, item):
            handle_action(action_key)

        return invoke

    menu = pystray.Menu(
        *[
            pystray.MenuItem(
                label,
                menu_action(action),
                default=default,
            )
            for label, action, default in build_tray_menu_spec()
        ]
    )
    icon = pystray.Icon("agentchatroom", image, "AgentChatRoom 控制台", menu)
    try:
        icon.run_detached()
        icon.visible = True
    except Exception:
        return None
    return icon


def main() -> None:
    """Console-script entry: ``agentchatroom-gui [--config PATH]``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="agentchatroom-gui", description="打开 AgentChatRoom 轻量控制台"
    )
    parser.add_argument("--config", default=None, help="配置文件路径")
    arguments = parser.parse_args()
    run_gui(arguments.config)


def _show_fatal_gui_dialog(message: str) -> None:
    """Surface a fatal startup diagnostic without tkinter (#161).

    A windowed PyInstaller exe has no console, so when Tcl/Tk itself is
    broken the native message box is the only channel the user actually
    sees. Only the packaged (frozen) form pops the dialog — console runs
    and tests read the SystemExit text directly.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "AgentChatRoom", 0x10)
    except Exception:
        pass


def run_gui(config_path: str | None = None) -> None:
    """Open the local GUI window (requires tkinter, imported lazily)."""
    os.environ.setdefault("AGENTCHATROOM_ACCESS_LOG", "0")
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except ImportError as error:
        message = (
            "无法加载 tkinter 图形界面模块。请安装包含 tkinter 的 Python"
            "（python.org 官方安装器默认包含，安装时勾选 tcl/tk），"
            "删除本仓库的 .venv 目录后重新双击启动入口。"
        )
        _show_fatal_gui_dialog(message)
        raise SystemExit(message) from error

    try:
        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
    except tk.TclError as error:
        # import 成功但 Tcl/Tk 运行库初始化失败——典型场景是打包环境缺少
        # init.tcl（PyInstaller 探测到损坏的 tkinter 后静默排除）。
        message = (
            "tkinter 模块已找到，但 Tcl/Tk 运行库初始化失败：\n"
            f"{error}\n\n"
            "常见原因：打包时构建环境的 Tcl/Tk 不完整，exe 内缺少 "
            "init.tcl。请改用自带完整 Tcl/Tk 的官方 Python 重新打包，"
            "并在构建日志中确认 tkinter 未被排除；开发环境请安装完整 "
            "Python 运行时后再启动。"
        )
        _show_fatal_gui_dialog(message)
        raise SystemExit(message) from error

    settings = load_settings(config_path)
    controller = ServiceController(settings)
    sink: Queue = Queue()
    stop_log_event = threading.Event()

    class ControllerWindow(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(f"{settings.product_name} 控制台")
            self.geometry("780x520")
            self.minsize(620, 420)
            self.action_active = False
            self.pending_exit_after_stop = False
            self.running_port: int | None = None
            self.tray = None
            self.tray_hint_shown = False

            top = ttk.Frame(self, padding=(10, 10, 10, 4))
            top.pack(fill="x")
            controls = ttk.Frame(top)
            controls.pack(fill="x")
            ttk.Label(controls, text="IP:").pack(side="left")
            self.host_entry = ttk.Entry(controls, width=13, justify="center")
            self.host_entry.insert(0, settings.host)
            self.host_entry.pack(side="left", padx=(4, 8))
            ttk.Label(controls, text="端口:").pack(side="left")
            self.port_entry = ttk.Entry(controls, width=7, justify="center")
            self.port_entry.insert(0, str(settings.port))
            self.port_entry.pack(side="left", padx=(4, 14))
            self.start_button = ttk.Button(controls, text="启动服务", command=self.on_start)
            self.start_button.pack(side="left", padx=(0, 6))
            self.restart_button = ttk.Button(
                controls, text="重启服务", command=self.on_restart
            )
            self.restart_button.pack(side="left", padx=(0, 6))
            self.stop_button = ttk.Button(controls, text="停止服务", command=self.on_stop)
            self.stop_button.pack(side="left", padx=(0, 6))
            self.frontend_button = ttk.Button(
                controls, text="打开前端", command=self.on_frontend
            )
            self.frontend_button.pack(side="left")
            self.status_label = ttk.Label(top, text="")
            self.status_label.pack(fill="x", anchor="w", pady=(6, 0))

            self.log_view = scrolledtext.ScrolledText(
                self, height=20, state="disabled", wrap="word"
            )
            self.log_view.pack(fill="both", expand=True, padx=10, pady=(6, 10))

            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.bind("<Unmap>", self.on_unmap)
            self.append_log(f"数据目录: {settings.data_dir}")
            self.append_log("就绪。点击「启动服务」开始；关闭或最小化窗口会收起到系统托盘。")
            self.refresh_state()
            self.after(100, self.drain_events)
            self.after(2000, self.poll_state)

        def append_log(self, text: str) -> None:
            self.log_view.configure(state="normal")
            self.log_view.insert("end", text + "\n")
            self.log_view.see("end")
            if int(self.log_view.index("end-1c").split(".")[0]) > 5000:
                self.log_view.delete("1.0", "2000.0")
            self.log_view.configure(state="disabled")

        def refresh_state(self) -> None:
            pid = controller.running_pid()
            running = pid is not None
            start_enabled, stop_enabled = button_states(running, self.action_active)
            self.start_button.state(["!disabled"] if start_enabled else ["disabled"])
            self.restart_button.state(
                ["!disabled"] if (not self.action_active) else ["disabled"]
            )
            self.stop_button.state(["!disabled"] if stop_enabled else ["disabled"])
            self.frontend_button.state(
                ["!disabled"] if running and not self.action_active else ["disabled"]
            )
            if running:
                url = (
                    controller.display_url(self._safe_host(), self.running_port)
                    if self.running_port
                    else running_url_from_log(controller.log_path)
                )
                address = f" {url}" if url else ""
                self.status_label.configure(
                    text=f"● 运行中{address} (pid {pid})", foreground="#1a7f37"
                )
            else:
                self.status_label.configure(text="○ 已停止", foreground="#6b7280")

        def _safe_host(self) -> str:
            try:
                return validate_host(self.host_entry.get())
            except HostError:
                return settings.host

        def poll_state(self) -> None:
            was_running = controller.is_running()
            self.refresh_state()
            if was_running and not controller.is_running():
                self.append_log("服务进程已退出（外部停止或异常退出）。")
            self.after(2000, self.poll_state)

        def drain_events(self) -> None:
            for message in _drain(sink):
                self.handle_event(message)
            self.after(100, self.drain_events)

        def handle_event(self, message: tuple[str, Any]) -> None:
            kind, payload = message
            if kind == "log":
                self.append_log(str(payload))
                return
            self.action_active = action_after_worker_event(
                self.action_active, kind
            )
            if kind == "started":
                self.running_port = self._launched_port(payload)
                note = "服务已在运行" if payload.get("already_running") else "服务已启动"
                self.append_log(
                    f"{note}：{payload.get('url', '')}（pid {payload.get('pid')}）"
                )
                self.refresh_state()
            elif kind == "start_failed":
                self.refresh_state()
                self.append_log(f"[启动失败] {payload}")
            elif kind == "restart_aborted":
                self.refresh_state()
                self.append_log(f"[重启中止] {payload}")
            elif kind == "stopped":
                stopped = bool(payload.get("stopped"))
                if stopped:
                    self.append_log("服务已停止。")
                else:
                    self.append_log(f"[停止未完成] 原因：{payload.get('reason')}")
                    if payload.get("reason") == "timeout":
                        self.append_log(
                            "停止超时：请运行「关闭 AgentChatRoom.cmd」完成残留清理。"
                        )
                self.refresh_state()
                if self.pending_exit_after_stop:
                    self.finish_exit_after_stop(stopped)
            elif kind == "stop_failed":
                self.refresh_state()
                self.append_log(f"[停止失败] {payload}")
                if self.pending_exit_after_stop:
                    self.finish_exit_after_stop(False)

        def _launched_port(self, payload: dict[str, Any]) -> int | None:
            url = str(payload.get("url", ""))
            suffix = url.rsplit(":", 1)[-1]
            try:
                return int(suffix)
            except ValueError:
                return None

        def _validated_host_port(self) -> tuple[str, int] | None:
            try:
                host = validate_host(self.host_entry.get())
                port = validate_port(self.port_entry.get())
            except (HostError, PortError) as error:
                self.append_log(f"[输入错误] {error}")
                return None
            if host_requires_management_auth(host) and not (
                settings.management_auth_required
            ):
                self.append_log(
                    "[绑定拒绝] 非环回地址（如 0.0.0.0 或局域网 IP）必须先在配置中"
                    "开启管理认证（[security] management_auth_required），"
                    "否则服务会拒绝启动。"
                )
                return None
            return host, port

        def on_start(self) -> None:
            if self.action_active:
                return
            target = self._validated_host_port()
            if target is None:
                return
            host, port = target
            self.action_active = True
            self.refresh_state()
            self.append_log(f"正在启动服务（{host}:{port}）...")
            threading.Thread(
                target=self.start_worker, args=(host, port), daemon=True
            ).start()

        def start_worker(self, host: str, port: int) -> None:
            run_start_worker(
                sink,
                host=host,
                port=port,
                settings=settings,
                start_fn=controller.start,
            )

        def on_restart(self) -> None:
            if self.action_active:
                return
            target = self._validated_host_port()
            if target is None:
                return
            host, port = target
            self.action_active = True
            self.refresh_state()
            steps = restart_steps(controller.is_running())
            self.append_log(
                "正在重启服务（先停止后启动）..."
                if steps == ("stop", "start")
                else f"服务未在运行，直接启动（{host}:{port}）..."
            )
            threading.Thread(
                target=self.restart_worker, args=(host, port), daemon=True
            ).start()

        def restart_worker(self, host: str, port: int) -> None:
            run_restart_worker(
                sink,
                is_running=controller.is_running,
                stop_fn=controller.stop,
                start_body=lambda: self.start_worker(host, port),
            )

        def on_frontend(self) -> None:
            if controller.running_pid() is None:
                self.append_log("服务未运行，请先启动服务再打开前端。")
                return
            host = self._safe_host()
            url = resolve_frontend_url(
                running_url_from_log(controller.log_path)
                if not self.running_port
                else None,
                host,
                self.running_port or settings.port,
            )
            self.append_log(f"正在用系统默认浏览器打开前端：{url}")
            threading.Thread(
                target=lambda: webbrowser.open(url), daemon=True
            ).start()

        def on_stop(self) -> None:
            if self.action_active:
                return
            self.action_active = True
            self.refresh_state()
            self.append_log("正在停止服务...")
            threading.Thread(target=self.stop_worker, daemon=True).start()

        def stop_worker(self) -> None:
            run_stop_worker(sink, stop_fn=controller.stop)

        def on_close(self) -> None:
            if self.tray is not None:
                self.hide_to_tray()
                return
            running = controller.is_running()
            choice: str | None = None
            if running:
                answer = messagebox.askyesnocancel(
                    "关闭 AgentChatRoom 控制台",
                    "服务仍在运行。\n\n"
                    "是：结束服务并关闭\n"
                    "否：保留服务运行，仅关闭窗口\n"
                    "取消：不关闭",
                )
                choice = {True: "stop", False: "keep", None: "cancel"}[answer]
            action = close_action(running, choice)
            if action == STAY:
                return
            if action == KEEP_RUNNING:
                self.shutdown_app()
                return
            if action == STOP_AND_CLOSE:
                self.action_active = True
                self.pending_exit_after_stop = True
                self.refresh_state()
                self.append_log("正在结束服务并关闭...")
                threading.Thread(target=self.stop_worker, daemon=True).start()
                return
            self.shutdown_app()

        def on_unmap(self, event) -> None:
            if self.tray is None or event.widget is not self:
                return
            try:
                iconic = self.state() == "iconic"
            except tk.TclError:
                return
            if iconic:
                self.hide_to_tray()

        def hide_to_tray(self) -> None:
            self.withdraw()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                self.append_log(
                    "控制台已收起到系统托盘：双击托盘图标恢复窗口，"
                    "托盘右键菜单可打开前端、启动/重启/停止服务或退出。"
                )

        def restore_from_tray(self) -> None:
            self.deiconify()
            self.lift()
            self.focus_force()

        def handle_tray_action(self, action: str) -> None:
            if action == "show":
                self.restore_from_tray()
            elif action == "frontend":
                self.on_frontend()
            elif action == "start":
                self.on_start()
            elif action == "restart":
                self.on_restart()
            elif action == "stop":
                self.on_stop()
            elif action == "quit":
                self.on_tray_quit()

        def on_tray_quit(self) -> None:
            running = controller.is_running()
            choice: str | None = None
            if running:
                answer = messagebox.askyesnocancel(
                    "退出 AgentChatRoom 控制台",
                    "服务仍在运行。\n\n"
                    "是：结束服务并退出\n"
                    "否：保留后台服务，仅退出控制台\n"
                    "取消：不退出",
                )
                choice = {True: "stop", False: "keep", None: "cancel"}[answer]
            action = close_action(running, choice)
            if action == STAY:
                return
            if action == STOP_AND_CLOSE:
                self.action_active = True
                self.pending_exit_after_stop = True
                self.refresh_state()
                self.append_log("正在结束服务并退出...")
                threading.Thread(target=self.stop_worker, daemon=True).start()
                return
            self.shutdown_app()

        def finish_exit_after_stop(self, stopped: bool) -> None:
            self.pending_exit_after_stop = False
            if stopped:
                self.shutdown_app()
                return
            force = messagebox.askyesno(
                "停止未完成",
                "服务停止未完成。稍后可运行「关闭 AgentChatRoom.cmd」清理残留进程。\n\n"
                "仍要退出控制台吗？",
            )
            if force:
                self.shutdown_app()
            else:
                self.action_active = False
                self.refresh_state()

        def shutdown_app(self) -> None:
            stop_log_event.set()
            if self.tray is not None:
                self.tray.stop()
            self.destroy()

    log_tail = LogTail(controller.log_path, sink, stop_log_event)
    log_tail.start()
    window = ControllerWindow()
    window.tray = create_tray_icon(
        lambda action: window.after(0, window.handle_tray_action, action)
    )
    if window.tray is None:
        window.append_log(
            "系统托盘不可用（需要 pystray 与 Pillow，可通过 pip install "
            "\"agentchatroom[gui]\" 安装）：关闭窗口将按原来的方式询问，"
            "最小化只到任务栏。"
        )
    try:
        window.mainloop()
    finally:
        stop_log_event.set()
