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
import time
import tomllib
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from .cli import (
    process_is_running,
    service_url,
    start_detached_server,
    stop_detached_server,
)
from .config import (
    Settings,
    default_data_dir,
    load_settings,
    parse_service_url,
    rewrite_config_text,
)
from .errors import DomainError
from .gui_autostart import GuiInstanceGuard, LoginStartup, instance_guard_name
from .gui_event_log import RoomEventTail, format_service_line, sanitize_gui_text
from .runtime_log import running_url_from_log as _running_url_from_log
from .runtime_log import tail_text_lines

MIN_PORT = 1
MAX_PORT = 65535
_DIGITS_ONLY = re.compile(r"\A[0-9]+\Z")

# The detached service appends to ``server.log`` forever and nothing rotates
# it (a live checkout log measured 23 MB), and the address lookup runs on the
# Tk thread from the 2 second poll, so the reverse tail it reads is bounded
# twice over. The pattern and the bounds live in ``runtime_log`` so the CLI
# resolves an already-running service the same way; they are re-exported here
# because they are part of this module's contract about that bounded read.
from .runtime_log import RUNNING_URL_TAIL_BYTES, RUNNING_URL_TAIL_LINES  # noqa: F401

# Mirrors the server config rule: only loopback targets may bind without
# management authentication.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

CLOSE = "close"
STOP_AND_CLOSE = "stop_and_close"
KEEP_RUNNING = "keep_running"
STAY = "stay"

# Why a port cannot be bound, which decides whether waiting or changing the
# port is the right advice: a listener answers a connect, a bare TIME_WAIT
# reservation does not.
PORT_FREE = "free"
PORT_OCCUPIED = "occupied"
PORT_RESERVED = "reserved"

# A refused bind that no listener answers is retried briefly before the user
# is told anything, so a restart racing a socket the OS is still releasing
# succeeds on its own.
PORT_PROBE_ATTEMPTS = 4
PORT_PROBE_BACKOFF_SECONDS = 0.15


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


def _bind_probe(host: str, port: int) -> bool:
    """Try to bind and listen once; False when the address refuses us."""
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


def _listener_reachable(host: str, port: int, timeout: float = 0.25) -> bool:
    """Whether anything already accepts connections on the address.

    A refused bind on its own cannot tell a running service apart from a
    lingering TIME_WAIT reservation, and the two need opposite advice. A
    successful connect proves a live listener is the cause.
    """
    family = socket.AF_INET6 if ":" in (host or "") else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            probe.connect((host or "127.0.0.1", port))
    except OSError:
        return False
    return True


def port_probe_state(host: str, port: int) -> str:
    """Classify the address as free, occupied by a listener, or not bindable.

    A refused bind on its own cannot tell a running service apart from a
    lingering TIME_WAIT reservation, and the two need opposite advice: stop
    the service, or simply wait. A successful connect proves a live listener
    is the cause, so that case is decided without any retry.
    """
    if _bind_probe(host, port):
        return PORT_FREE
    if _listener_reachable(host, port):
        return PORT_OCCUPIED
    # Nothing is listening, so the refusal is a reservation the OS has not
    # released yet. Retry it briefly so a restart racing that release
    # succeeds on its own instead of telling the user to pick another port.
    for attempt in range(1, PORT_PROBE_ATTEMPTS):
        time.sleep(PORT_PROBE_BACKOFF_SECONDS)
        if _bind_probe(host, port):
            return PORT_FREE
        if _listener_reachable(host, port):
            return PORT_OCCUPIED
    return PORT_RESERVED


def port_is_free(host: str, port: int) -> bool:
    """Return True when nothing is listening on the address yet."""
    return port_probe_state(host, port) == PORT_FREE


def port_unavailable_message(port: int, state: str) -> str:
    """Explain an unusable port without sending the user down a dead end."""
    if state == PORT_RESERVED:
        # No listener holds the address, so a different port cannot help; only
        # waiting for the OS to release the reservation can.
        return (
            f"端口 {port} 当前无法绑定，但没有服务在监听：通常是上次运行的连接"
            "仍处于 TCP 保留期，请稍候重试；更换端口不能解决这个问题。"
        )
    return f"端口 {port} 已被占用：可能已有一个服务在运行，请先停止或更换端口。"


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
    """Return the newest actual listening address recorded in the server log.

    Reads through the shared bounded reverse tail used by ``agentchatroom
    logs`` and the admin runtime panel: the previous full ``read_text`` cost
    tens of megabytes on the Tk thread every two seconds.

    The implementation lives in ``runtime_log`` so the CLI's
    already-running branch reads the same address from the same place;
    ``tail_text_lines`` is passed in so this module keeps its own injection
    point for the bounded-read assertion.
    """
    return _running_url_from_log(log_path, tail_reader=tail_text_lines)


def config_file_path(settings: Settings) -> Path:
    """Return the config file backing the current effective settings."""
    if settings.config_path:
        return Path(settings.config_path)
    return settings.data_dir / "config.toml"


def _require_parsable_config(config_path: Path, current: str) -> None:
    """Reject a hand-corrupted file before the GUI patches a line of it."""
    if not current:
        return
    try:
        tomllib.loads(current)
    except tomllib.TOMLDecodeError as error:
        raise DomainError(
            "config_file_unparsable",
            "The saved configuration cannot be read; repair or remove it first",
            status_code=409,
            details={
                "required_action": "restore_config_from_backup_or_delete_it",
                "config_path": str(config_path),
                "toml_error": str(error),
            },
        ) from error


def update_config_values(
    config_path: Path,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Persist the chosen host/port into the local config file for later launches.

    Only the keys inside ``[server]`` are replaced, through the same shared
    in-section writer the management settings endpoint uses, and the result
    goes through the atomic read-modify-write path. The GUI used to rebuild the
    whole document from a parsed dict, which erased every explanatory comment
    the user copied from ``config.example.toml`` and raised ``TypeError``/
    ``ValueError`` on root scalars, nested tables and array tables.
    """
    config_path = Path(config_path)
    values: dict[str, Any] = {}
    if host is not None:
        values["host"] = host
    if port is not None:
        values["port"] = port
    if not values:
        return

    from .api import upsert_toml_section

    def build(current: str) -> str:
        _require_parsable_config(config_path, current)
        return upsert_toml_section(current, "server", values)

    rewrite_config_text(config_path, build)


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
        # One line per failure streak rather than one per poll tick: a share
        # that stays unreachable for an hour must not fill the panel.
        self._read_error_reported = False

    def run(self) -> None:
        handle = None
        historical = self.path.exists()
        try:
            while not self.stop_event.is_set():
                if handle is None:
                    if not self.path.exists():
                        self.stop_event.wait(0.5)
                        continue
                    handle = self._open()
                    if handle is None:
                        self.stop_event.wait(1.0)
                        continue
                    for line in handle.read().splitlines()[-self.tail_lines :]:
                        formatted = format_service_line(line.rstrip("\r\n"))
                        if formatted:
                            self.sink.put(("log", f"历史服务日志：{formatted}" if historical else formatted))
                    historical = False
                    continue
                try:
                    current = self.path.stat()
                    opened = os.fstat(handle.fileno())
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                        handle.close()
                        handle = None
                        continue
                    if current.st_size < handle.tell():
                        handle.seek(0)
                except FileNotFoundError:
                    handle.close()
                    handle = None
                    continue
                try:
                    line = handle.readline()
                except OSError:
                    # Antivirus, OneDrive and network shares all take the file
                    # away for a moment. Losing the thread here would freeze the
                    # log panel for the rest of the session with no explanation,
                    # so drop the handle, say so once, and reopen on the next
                    # pass - the same shape RoomEventTail uses.
                    self._report_read_error(handle)
                    handle = None
                    continue
                if line:
                    formatted = format_service_line(line.rstrip("\r\n"))
                    if formatted:
                        self.sink.put(("log", formatted))
                else:
                    self.stop_event.wait(0.2)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    def _open(self):
        """Open the log, tolerating a file that is temporarily unavailable."""
        try:
            handle = self.path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            self._note_unavailable()
            return None
        self._read_error_reported = False
        return handle

    def _note_unavailable(self) -> None:
        if self._read_error_reported:
            return
        self._read_error_reported = True
        self.sink.put(
            ("log", "[服务日志] 暂时无法读取日志文件，稍后自动重试。")
        )

    def _report_read_error(self, handle: Any) -> None:
        """Close what we have and say once that the panel is still retrying."""
        try:
            handle.close()
        except OSError:
            pass
        self._note_unavailable()


def _drain(sink: Queue, *, limit: int = 200) -> list[Any]:
    items = []
    while len(items) < limit:
        try:
            items.append(sink.get_nowait())
        except Empty:
            break
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
    try:
        if port_probe is None:
            state = port_probe_state(host, port)
            available = state == PORT_FREE
        else:
            # An injected probe only answers free/taken, so the reason behind a
            # refusal stays unknown and gets the generic occupied wording.
            available = port_probe(host, port)
            state = PORT_FREE if available else PORT_OCCUPIED
        if not available:
            sink.put(("start_failed", port_unavailable_message(port, state)))
            return
        result = start_fn(host, port)
        sink.put(("started", result))
        # The address to remember is the one the service reports, not the one
        # that was requested: on the already-running path nothing was bound
        # here, and writing the request back would cache a port this service
        # does not listen on. A URL that carries no port is not trusted, and
        # nothing is written for it.
        probe_host, probe_port = parse_service_url(result.get("url") or "")
        if probe_port and (
            probe_host != settings.host
            or int(probe_port) != settings.port
            or (host, port) != (settings.host, settings.port)
        ):
            config_host = probe_host or host
            config_port = int(probe_port)
            try:
                update_config_values(
                    config_file_path(settings), host=config_host, port=config_port
                )
                sink.put(
                    (
                        "log",
                        f"{config_host}:{config_port} 已写入配置，后续启动将使用该配置。",
                    )
                )
            except (OSError, TypeError, ValueError, DomainError) as error:
                # The service is already up: a config write problem is a
                # separate, non-fatal report, never a fake 「启动失败」.
                sink.put(
                    (
                        "log",
                        f"[配置写入失败] 服务已启动，但 {host}:{port} 未能写入"
                        f"配置文件（下次启动沿用原配置）：{type(error).__name__}: {error}",
                    )
                )
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


TRAY_READY_TIMEOUT_SECONDS = 3.0

# ``refresh_state`` probes the pid itself unless a caller already observed one;
# ``None`` is a real observation ("the service is not running"), so a distinct
# sentinel is required.
_UNPROBED = object()


def stop_tray_icon(icon) -> None:
    """Best-effort pystray shutdown so its loop thread never outlives the GUI.

    ``Icon.stop`` is a no-op while the backend never reached ``_running`` and
    it raises when the native window handle is missing, so neither outcome may
    be allowed to break Tk teardown.
    """
    if icon is None:
        return
    try:
        icon.stop()
    except Exception:
        pass


def tray_degraded_hint(reason: str, timeout_seconds: float) -> str:
    """Return the Chinese degrade notice shown when the tray never appeared."""
    detail = reason or (
        f"系统托盘图标在 {timeout_seconds:g} 秒内没有完成注册（NIM_ADD 未生效）"
    )
    return (
        f"{detail}：本次运行不会收起到系统托盘，关闭窗口时将询问结束服务或保留"
        "服务，最小化只到任务栏；如需托盘请安装/修复 pystray 与 Pillow 后重启。"
    )


def create_tray_icon(
    handle_action,
    *,
    ready_timeout: float = TRAY_READY_TIMEOUT_SECONDS,
    note: Callable[[str], None] | None = None,
):
    """Build a detached pystray icon wired to ``handle_action``; None if unavailable.

    ``handle_action`` receives one action key from :func:`build_tray_menu_spec`;
    pystray invokes callbacks on its own thread, so handlers must marshal back
    to the Tk loop themselves.

    The win32 backend creates its window handle on the loop thread and only
    then marks the icon ready, so showing the icon has to happen inside the
    ready setup callback: setting ``visible`` from this thread ran before
    ``_hwnd`` existed and, through pystray's ``if self._visible == value:
    return`` early exit, turned pystray's own ready-time show into a no-op —
    the shell never received NIM_ADD while the program believed it had.
    Availability is therefore judged on the icon really being visible;
    otherwise the loop is stopped and the caller degrades to the
    window/taskbar-only path.
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
    shown = threading.Event()
    failures: list[str] = []

    def ready_setup(running_icon) -> None:
        # pystray runs this only after the backend called ``_mark_ready``,
        # which on win32 is after the notify window handle exists.
        try:
            running_icon.visible = True
        except Exception as error:
            failures.append(format_worker_error(error))
            return
        if getattr(running_icon, "visible", False):
            shown.set()

    def degrade() -> None:
        stop_tray_icon(icon)
        if note is not None:
            note(tray_degraded_hint(failures[0] if failures else "", ready_timeout))

    try:
        icon.run_detached(ready_setup)
    except Exception as error:
        failures.append(format_worker_error(error))
        degrade()
        return None
    if not shown.wait(timeout=ready_timeout) or not getattr(icon, "visible", False):
        degrade()
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


def duplicate_instance_notice(product_name: str) -> str:
    """Return what the user is told when another console already owns this data dir.

    Returning quietly is the worst outcome available: the second launch looks
    like a program that refused to start, and the user has no way to tell that
    from a crash or a broken install.
    """
    return (
        f"已有一个 {product_name} 控制台正在运行（同一数据目录），本次启动已退出。"
        "\n\n已运行的控制台窗口已尝试恢复到前台；若找不到它，请从系统托盘图标"
        "重新打开，或先结束该进程。\n\n"
        "如需同时运行多个控制台，请为它们配置不同的数据目录"
        "（AGENTCHATROOM_DATA_DIR 或配置文件中的 data_dir）。"
    )


def notify_duplicate_instance(product_name: str, *, show=None) -> str:
    """Show the duplicate-instance notice through whichever channel exists.

    ``run_gui`` returns without a window here, so tkinter is not a usable
    channel yet. The native dialog covers the packaged windowed exe, stderr
    covers a console run, and ``show`` lets a caller that already has a Tk
    root present the message itself.
    """
    notice = duplicate_instance_notice(product_name)
    _show_fatal_gui_dialog(notice)
    print(notice, file=sys.stderr)
    if show is not None:
        try:
            show(notice)
        except Exception:
            # A failing notice must not turn a clean handoff into a crash.
            pass
    return notice


def run_gui(config_path: str | None = None, *, autostart: bool = False) -> None:
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

    startup_config_error: Exception | None = None
    try:
        settings = load_settings(config_path)
    except (OSError, TypeError, ValueError) as error:
        if not autostart:
            raise
        # Login startup must leave a usable diagnostic GUI rather than
        # disappearing when a user-edited config becomes invalid. No service
        # starts from these fallback values until the config loads again.
        startup_config_error = error
        settings = Settings(
            data_dir=default_data_dir(),
            config_path=Path(config_path) if config_path else None,
        )
    instance_guard = GuiInstanceGuard(
        name=instance_guard_name(settings.data_dir),
        window_title=f"{settings.product_name} 控制台",
    )
    if not instance_guard.acquire():
        # Returning silently looks exactly like a program that refused to
        # start, so say what happened and where to look for the window.
        notify_duplicate_instance(settings.product_name)
        return
    controller = ServiceController(settings)
    login_startup = LoginStartup()
    sink: Queue = Queue()
    tails: dict[str, tuple[threading.Event, threading.Thread]] = {}

    def stop_tails(*, wait: bool = False) -> None:
        for stop_event, _thread in tails.values():
            stop_event.set()
        if wait:
            for _stop_event, thread in tails.values():
                if isinstance(thread, threading.Thread) and thread.is_alive():
                    thread.join(timeout=2)

    def start_tails(
        active_settings: Settings,
        *,
        previous_settings: Settings | None = None,
        report: Callable[[str], None] | None = None,
    ) -> None:
        """Point the log and Room-event readers at ``active_settings``.

        The replacements are constructed before anything is stopped: a
        database that cannot be opened (an unset PostgreSQL URL, a locked
        file) must leave the previous readers running and must never keep the
        window from opening. Whatever could not be built is reported through
        ``report`` so the user learns why a panel went quiet.
        """
        old_room = tails.get("room")
        old_log = tails.get("log")
        reuse_log = bool(
            old_log is not None and previous_settings is not None
            and not old_log[0].is_set()
            and previous_settings.data_dir == active_settings.data_dir
        )
        if reuse_log and old_log is not None:
            log_stop, log_tail = old_log
        else:
            log_stop = threading.Event()
            log_tail = LogTail(active_settings.data_dir / "server.log", sink, log_stop)
        # Reuse the live service when the retry points at the same database.
        # RoomEventTail builds its own AgentChatRoomService otherwise, and each
        # one of those owns a database handle plus a registered TokenTouchBuffer
        # worker, so a config retry used to stack a second set per attempt and
        # leave the previous one's atexit entry pointing at a closed database.
        previous_room = old_room[1] if old_room is not None else None
        previous_service = getattr(previous_room, "service", None)
        same_database = bool(
            previous_service is not None
            and previous_settings is not None
            and previous_settings.database_path == active_settings.database_path
            and previous_settings.database_backend == active_settings.database_backend
        )
        room_stop = threading.Event()
        try:
            room_events: RoomEventTail | None = RoomEventTail(
                active_settings,
                sink,
                room_stop,
                service=previous_service if same_database else None,
            )
        except Exception as error:
            # create_database raises here for a postgresql backend with no
            # connection URL; that is a configuration problem the user can fix
            # in this very window, not a reason to refuse to show the window.
            room_events = None
            if report is not None:
                report(
                    f"Room 事件读取器未启动：{type(error).__name__}: {error}。"
                    "服务日志与启停功能不受影响；修复 [database] 配置后点击"
                    "「启动服务」即可重试。"
                )
        if room_events is not None and same_database and (
            not isinstance(previous_room, threading.Thread)
            or not previous_room.is_alive()
        ):
            # A configuration retry using the same database must not drop events
            # between the old tail stopping and the new one beginning.
            room_events.cursors = previous_room.cursors.copy()
            room_events.presence = {
                key: value.copy() for key, value in previous_room.presence.items()
            }
        # Retire the previous readers only now that a replacement exists for
        # each of them, so a failed rebuild cannot silence the panel for good.
        # The log reader is carried over by ``reuse_log``; the Room reader is
        # always rebuilt, so it survives only when the rebuild failed.
        keep = {"log"} if reuse_log else set()
        if room_events is None:
            keep.add("room")
        stale = [entry for key, entry in tails.items() if key not in keep]
        for stop_event, _thread in stale:
            stop_event.set()
        for _stop_event, thread in stale:
            if isinstance(thread, threading.Thread) and thread.is_alive():
                thread.join(timeout=2)
        tails["log"] = (log_stop, log_tail)
        if room_events is not None:
            tails["room"] = (room_stop, room_events)
        if not reuse_log:
            log_tail.start()
        if room_events is not None:
            room_events.start()

    class ControllerWindow(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(f"{settings.product_name} 控制台")
            self.geometry("780x520")
            self.minsize(620, 420)
            self.action_active = False
            self.pending_exit_after_stop = False
            self.running_port: int | None = None
            # The address the current service process actually listens on, plus
            # the pid it belongs to. Remembering the URL (not just the port) is
            # what keeps the 2 s poll off the log file entirely for a service
            # this window started, and bounds it to one tail read per process
            # for a service started elsewhere.
            self.running_url: str | None = None
            self.running_url_pid: int | None = None
            self.poll_saw_running = False
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
            self.login_startup_var = tk.BooleanVar(value=False)
            self.login_startup_checkbox = ttk.Checkbutton(
                top, text="开机启动", variable=self.login_startup_var,
                command=self.on_login_startup_toggle,
            )
            self.login_startup_checkbox.pack(anchor="w", pady=(4, 0))

            self.log_view = scrolledtext.ScrolledText(
                self, height=20, state="disabled", wrap="word"
            )
            self.log_view.pack(fill="both", expand=True, padx=10, pady=(6, 10))

            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.bind("<Unmap>", self.on_unmap)
            self._refresh_login_startup()
            self.append_log("数据目录已加载。")
            if startup_config_error is not None:
                self.append_log(f"配置读取失败：{startup_config_error}；修复配置后可点击启动服务重试。")
            self.append_log("就绪。点击「启动服务」开始；关闭或最小化窗口会收起到系统托盘。")
            self.refresh_state()
            self.after(100, self.drain_events)
            self.after(2000, self.poll_state)
            if autostart:
                self.after(250, self.start_after_login)

        def append_log(self, text: str) -> None:
            clean = sanitize_gui_text(text)
            if not clean:
                return
            if not re.match(r"^\[\d{4}-\d{2}-\d{2} ", clean):
                clean = f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {clean}"
            self.log_view.configure(state="normal")
            self.log_view.insert("end", clean + "\n")
            self.log_view.see("end")
            if int(self.log_view.index("end-1c").split(".")[0]) > 5000:
                self.log_view.delete("1.0", "2000.0")
            self.log_view.configure(state="disabled")

        def _refresh_login_startup(self) -> None:
            try:
                enabled = login_startup.enabled()
                self.login_startup_var.set(enabled)
                if enabled and not login_startup.is_current():
                    self.append_log("开机启动项指向旧程序位置；请取消勾选后重新勾选以更新。")
            except OSError as error:
                self.login_startup_var.set(False)
                self.append_log(f"读取开机启动项失败：{type(error).__name__}")
            if os.name != "nt":
                self.login_startup_checkbox.state(["disabled"])

        def on_login_startup_toggle(self) -> None:
            requested = self.login_startup_var.get()
            try:
                if requested:
                    login_startup.enable()
                    self.append_log("已启用开机启动：下次登录将打开控制台并启动 Room 服务。")
                else:
                    login_startup.disable()
                    self.append_log("已关闭开机启动；当前服务不会因此停止。")
            except OSError as error:
                self.append_log(f"设置开机启动失败：{error}")
                self._refresh_login_startup()
                messagebox.showerror("开机启动设置失败", sanitize_gui_text(error))

        def start_after_login(self) -> None:
            if controller.is_running():
                self.append_log("登录后启动：Room 服务已在运行，不重复启动。")
                self.refresh_state()
            else:
                self.append_log("登录后启动：正在使用现有配置启动 Room 服务。")
                self.on_start()

        def _retry_config_load(self) -> bool:
            nonlocal settings, startup_config_error
            if startup_config_error is None:
                return True
            try:
                refreshed = load_settings(config_path)
            except (OSError, TypeError, ValueError) as error:
                startup_config_error = error
                self.append_log(f"配置仍不可用：{error}；请修复后重试。")
                return False
            previous_settings = settings
            settings = refreshed
            controller.settings = refreshed
            start_tails(
                refreshed, previous_settings=previous_settings, report=self.append_log
            )
            startup_config_error = None
            self.host_entry.delete(0, "end")
            self.host_entry.insert(0, refreshed.host)
            self.port_entry.delete(0, "end")
            self.port_entry.insert(0, str(refreshed.port))
            self.append_log("配置已重新加载，可以启动服务。")
            return True

        def _running_url_for(self, pid: int | None) -> str | None:
            """Return the listening address of ``pid``, remembering it per process.

            A service this window started already reported its address in the
            start result, so its status/front-end path costs no log reads at
            all. Anything else is resolved once per process through the
            bounded tail rather than on every poll tick.
            """
            if pid is None:
                self.running_url = None
                self.running_url_pid = None
                return None
            if self.running_url_pid != pid:
                # A different process may listen somewhere else entirely.
                self.running_url = None
                self.running_url_pid = pid
            if not self.running_url:
                found = running_url_from_log(controller.log_path)
                if found:
                    self.running_url = found
            return self.running_url

        def refresh_state(self, observed_pid: Any = _UNPROBED) -> None:
            pid = (
                controller.running_pid()
                if observed_pid is _UNPROBED
                else observed_pid
            )
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
                url = resolve_frontend_url(
                    self._running_url_for(pid),
                    self._safe_host(),
                    self.running_port or settings.port,
                )
                address = f" {url}" if url else ""
                self.status_label.configure(
                    text=f"● 运行中{address} (pid {pid})", foreground="#1a7f37"
                )
            else:
                self.running_url = None
                self.running_url_pid = None
                self.status_label.configure(text="○ 已停止", foreground="#6b7280")

        def _safe_host(self) -> str:
            try:
                return validate_host(self.host_entry.get())
            except HostError:
                return settings.host

        def poll_state(self) -> None:
            # One pid observation per tick, shared with the status refresh: the
            # previous three separate probes around a whole-log read reported
            # 「服务已退出」 for a service that was merely slow to appear.
            pid = controller.running_pid()
            running = pid is not None
            if self.poll_saw_running and not running:
                self.append_log("服务进程已退出（外部停止或异常退出）。")
            self.poll_saw_running = running
            self.refresh_state(pid)
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
                launched_url = str(payload.get("url") or "") or None
                launched_pid = payload.get("pid")
                if launched_url and isinstance(launched_pid, int):
                    # Remember the address this window started, so neither the
                    # status line nor 「打开前端」 ever reads the server log.
                    self.running_url = launched_url
                    self.running_url_pid = launched_pid
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
            if not self._retry_config_load():
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
            if not self._retry_config_load():
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
            pid = controller.running_pid()
            if pid is None:
                self.append_log("服务未运行，请先启动服务再打开前端。")
                return
            host = self._safe_host()
            # Same remembered-address path as the status line: a service
            # started elsewhere costs one bounded tail read per process, never
            # a fresh full-log scan per click.
            url = resolve_frontend_url(
                self._running_url_for(pid),
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
            # A second close while a stop is already in flight reaches
            # finish_exit_after_stop with nothing left to stop, and the user is
            # asked to confirm a stop that already finished. Every other exit
            # entry guards on this pair; these two did not.
            if self.action_active or self.pending_exit_after_stop:
                return
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
            if self.action_active or self.pending_exit_after_stop:
                return
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
            # A queued worker event can land after the window is gone. Asking a
            # destroyed window a question raises TclError on the worker thread
            # and the user never learns the stop did not finish, so treat it as
            # the end of the line instead.
            if not self._window_alive():
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

        def _window_alive(self) -> bool:
            """Whether this window still exists to receive a dialog."""
            try:
                return bool(self.winfo_exists())
            except Exception:  # noqa: BLE001 - a dead Tk must not raise here
                return False

        def shutdown_app(self) -> None:
            stop_tails()
            stop_tray_icon(self.tray)
            self.destroy()

    window = ControllerWindow()
    start_tails(settings, report=window.append_log)

    def restore_on_second_launch() -> None:
        if instance_guard.activation_requested():
            window.deiconify()
            window.lift()
            window.focus_force()
            window.attributes("-topmost", True)
            window.after(300, lambda: window.attributes("-topmost", False))
        window.after(150, restore_on_second_launch)

    window.after(150, restore_on_second_launch)
    tray_notes: list[str] = []

    def report_tray_note(text: str) -> None:
        tray_notes.append(text)
        window.append_log(text)

    window.tray = create_tray_icon(
        lambda action: window.after(0, window.handle_tray_action, action),
        note=report_tray_note,
    )
    if window.tray is None and not tray_notes:
        window.append_log(
            "系统托盘不可用（需要 pystray 与 Pillow，可通过 pip install "
            "\"agentchatroom[gui]\" 安装）：关闭窗口将按原来的方式询问，"
            "最小化只到任务栏。"
        )
    try:
        window.mainloop()
    finally:
        # The pystray loop thread and its hidden window must not outlive the
        # Tk mainloop, otherwise a fast close leaves the process running.
        stop_tray_icon(window.tray)
        stop_tails()
        instance_guard.release()
