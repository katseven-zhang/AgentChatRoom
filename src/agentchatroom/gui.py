"""Local GUI controller for starting and stopping the AgentChatRoom service.

The window is a thin adapter over the same lifecycle functions the CLI uses:
starting reuses the detached ``serve`` path (pid file, log file, health check)
and stopping reuses the normal stop plus Windows process-tree cleanup. No
second startup business logic lives here.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import tomllib
from dataclasses import replace
from pathlib import Path
from queue import Empty, Queue
from typing import Any

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
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key)\s*[=:]\s*([^\s&\"']+)"
)
_SENSITIVE_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*")
_SENSITIVE_PROVIDER_KEY = re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{8,}\b")

CLOSE = "close"
STOP_AND_CLOSE = "stop_and_close"
KEEP_RUNNING = "keep_running"
STAY = "stay"


class PortError(ValueError):
    """Raised when the port input is not a usable TCP port."""


def validate_port(raw: str) -> int:
    """Validate a user-supplied port string and return it as an integer."""
    text = str(raw).strip()
    if not _DIGITS_ONLY.fullmatch(text):
        raise PortError(f"端口必须是 {MIN_PORT}-{MAX_PORT} 之间的整数")
    port = int(text)
    if not MIN_PORT <= port <= MAX_PORT:
        raise PortError(f"端口必须在 {MIN_PORT}-{MAX_PORT} 之间")
    return port


def port_is_free(host: str, port: int) -> bool:
    """Return True when nothing is listening on the address yet."""
    family = socket.AF_INET6 if ":" in (host or "") else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host or "127.0.0.1", port))
        except OSError:
            return False
    return True


def redact_line(line: str) -> str:
    """Mask credential-looking fragments before showing a log line."""
    redacted = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=***", line
    )
    redacted = _SENSITIVE_BEARER.sub("Bearer ***", redacted)
    return _SENSITIVE_PROVIDER_KEY.sub("sk-***", redacted)


def button_states(
    service_running: bool, action_active: bool
) -> tuple[bool, bool]:
    """Return (start_enabled, stop_enabled) for the current UI state."""
    if action_active:
        return (False, False)
    return (not service_running, service_running)


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


def update_config_port(config_path: Path, port: int) -> None:
    """Persist the chosen port into the local config file for later launches."""
    config_path = Path(config_path)
    document: dict[str, dict[str, Any]] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            loaded = tomllib.load(handle)
        document = {name: dict(values) for name, values in loaded.items()}
    server = dict(document.get("server", {}))
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

    def display_url(self, port: int) -> str:
        return service_url(self.settings.host, port)

    def start(self, port: int) -> dict[str, Any]:
        effective = replace(self.settings, port=port)
        return start_detached_server(effective, self.settings.host, port)

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


def run_gui(config_path: str | None = None) -> None:
    """Open the local GUI window (requires tkinter, imported lazily)."""
    os.environ.setdefault("AGENTCHATROOM_ACCESS_LOG", "0")
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except ImportError as error:
        raise SystemExit(
            "无法加载 tkinter 图形界面模块。请安装包含 tkinter 的 Python"
            "（python.org 官方安装器默认包含，安装时勾选 tcl/tk），"
            "删除本仓库的 .venv 目录后重新双击启动入口。"
        ) from error

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
            self.pending_close_after_stop = False
            self.running_port: int | None = None

            top = ttk.Frame(self, padding=(10, 10, 10, 4))
            top.pack(fill="x")
            ttk.Label(top, text="端口:").pack(side="left")
            self.port_entry = ttk.Entry(top, width=9, justify="center")
            self.port_entry.insert(0, str(settings.port))
            self.port_entry.pack(side="left", padx=(4, 14))
            self.start_button = ttk.Button(top, text="启动服务", command=self.on_start)
            self.start_button.pack(side="left", padx=(0, 8))
            self.stop_button = ttk.Button(top, text="停止服务", command=self.on_stop)
            self.stop_button.pack(side="left")
            self.status_label = ttk.Label(top, text="", padding=(14, 0, 0, 0))
            self.status_label.pack(side="left")

            self.log_view = scrolledtext.ScrolledText(
                self, height=20, state="disabled", wrap="word"
            )
            self.log_view.pack(fill="both", expand=True, padx=10, pady=(6, 10))

            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.append_log(f"数据目录: {settings.data_dir}")
            self.append_log("就绪。点击「启动服务」开始。")
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
            self.stop_button.state(["!disabled"] if stop_enabled else ["disabled"])
            if running:
                url = (
                    controller.display_url(self.running_port)
                    if self.running_port
                    else running_url_from_log(controller.log_path)
                )
                address = f" {url}" if url else ""
                self.status_label.configure(
                    text=f"● 运行中{address} (pid {pid})", foreground="#1a7f37"
                )
            else:
                self.status_label.configure(text="○ 已停止", foreground="#6b7280")

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
            elif kind == "started":
                self.action_active = False
                self.running_port = self._launched_port(payload)
                note = "服务已在运行" if payload.get("already_running") else "服务已启动"
                self.append_log(
                    f"{note}：{payload.get('url', '')}（pid {payload.get('pid')}）"
                )
                self.refresh_state()
            elif kind == "start_failed":
                self.action_active = False
                self.refresh_state()
                self.append_log(f"[启动失败] {payload}")
            elif kind == "stopped":
                self.action_active = False
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
                if self.pending_close_after_stop:
                    self.finish_close_after_stop(stopped)
            elif kind == "stop_failed":
                self.action_active = False
                self.refresh_state()
                self.append_log(f"[停止失败] {payload}")
                if self.pending_close_after_stop:
                    self.finish_close_after_stop(False)

        def _launched_port(self, payload: dict[str, Any]) -> int | None:
            url = str(payload.get("url", ""))
            suffix = url.rsplit(":", 1)[-1]
            try:
                return int(suffix)
            except ValueError:
                return None

        def on_start(self) -> None:
            if self.action_active:
                return
            try:
                port = validate_port(self.port_entry.get())
            except PortError as error:
                self.append_log(f"[端口错误] {error}")
                return
            self.action_active = True
            self.refresh_state()
            self.append_log(f"正在启动服务（端口 {port}）...")
            threading.Thread(
                target=self.start_worker, args=(port,), daemon=True
            ).start()

        def start_worker(self, port: int) -> None:
            try:
                if not port_is_free(settings.host, port):
                    sink.put(
                        (
                            "start_failed",
                            f"端口 {port} 已被占用：可能已有一个服务在运行，"
                            "请先停止或更换端口。",
                        )
                    )
                    return
                result = controller.start(port)
                sink.put(("started", result))
                if port != settings.port:
                    try:
                        update_config_port(config_file_path(settings), port)
                        sink.put(("log", f"端口 {port} 已写入配置，后续启动将使用该端口。"))
                    except (OSError, ValueError) as error:
                        sink.put(("log", f"[配置写入失败] {error}"))
                    if os.getenv("AGENTCHATROOM_PORT", "").strip():
                        sink.put(
                            (
                                "log",
                                "检测到 AGENTCHATROOM_PORT 环境变量：其他启动入口仍以环境变量优先。",
                            )
                        )
            except SystemExit as error:
                sink.put(("start_failed", str(error)))
            except OSError as error:
                sink.put(("start_failed", f"{type(error).__name__}: {error}"))

        def on_stop(self) -> None:
            if self.action_active:
                return
            self.action_active = True
            self.refresh_state()
            self.append_log("正在停止服务...")
            threading.Thread(target=self.stop_worker, daemon=True).start()

        def stop_worker(self) -> None:
            try:
                sink.put(("stopped", controller.stop()))
            except OSError as error:
                sink.put(("stop_failed", f"{type(error).__name__}: {error}"))

        def on_close(self) -> None:
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
                self.shutdown_window()
                return
            if action == STOP_AND_CLOSE:
                self.action_active = True
                self.pending_close_after_stop = True
                self.refresh_state()
                self.append_log("正在结束服务并关闭...")
                threading.Thread(target=self.stop_worker, daemon=True).start()
                return
            self.shutdown_window()

        def finish_close_after_stop(self, stopped: bool) -> None:
            self.pending_close_after_stop = False
            if stopped:
                self.shutdown_window()
                return
            force = messagebox.askyesno(
                "停止未完成",
                "服务停止未完成。稍后可运行「关闭 AgentChatRoom.cmd」清理残留进程。\n\n"
                "仍要关闭 GUI 吗？",
            )
            if force:
                self.shutdown_window()
            else:
                self.action_active = False
                self.refresh_state()

        def shutdown_window(self) -> None:
            stop_log_event.set()
            self.destroy()

    log_tail = LogTail(controller.log_path, sink, stop_log_event)
    log_tail.start()
    window = ControllerWindow()
    try:
        window.mainloop()
    finally:
        stop_log_event.set()
