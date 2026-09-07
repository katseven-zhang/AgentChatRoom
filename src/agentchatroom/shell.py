"""Pywebview GUI shell adapter for AgentChatRoom.

This module acts as an independent adapter layer:
1. Bootstrap + window + child process lifecycle orchestration, zero business logic.
2. Shell crash never affects the detached serve process.
3. Edge WebView2 with fallback to system browser if unavailable.
4. Window URL strictly resolved from ServerTarget abstraction (no hardcoded host/port).
5. Single-instance enforcement (focuses existing window on duplicate launch).
6. Closing semantics with three choices: stop & close, keep running & close, cancel.
7. Native folder dialog bridge using pywebview.create_file_dialog.
8. Manual service control: a shell-owned top bar (status / port / start / stop)
   plus a local placeholder page so the user never sees 404 or connection
   errors; auto-start only via the client.toml ``auto_start`` option.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Any

from .client_config import (
    ClientConfig,
    ProbeResult,
    ServerTarget,
    TargetError,
    load_client_config,
    resolve_server_target,
)
from .config import Settings, load_settings
from .gui import (
    CLOSE,
    KEEP_RUNNING,
    STAY,
    STOP_AND_CLOSE,
    PortError,
    ServiceController,
    close_action,
    port_is_free,
    redact_line,
    running_url_from_log,
    update_config_port,
    validate_port,
)

logger = logging.getLogger(__name__)

SINGLE_INSTANCE_MUTEX_NAME = "Local\\AgentChatRoom_GUI_SingleInstance"
WINDOW_TITLE = "AgentChatRoom"


def enforce_single_instance(window_title: str = WINDOW_TITLE) -> bool:
    """Check if another GUI instance is running on Windows.

    If another instance exists, focuses its window and returns False (should exit).
    Returns True if this is the primary instance.
    """
    if os.name != "nt":
        return True

    try:
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        last_error = ctypes.windll.kernel32.GetLastError()
        # ERROR_ALREADY_EXISTS = 183
        if last_error == 183:
            hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
            if hwnd:
                # SW_RESTORE = 9
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            return False
        return True
    except Exception as error:
        logger.warning("Single instance check failed: %s", redact_line(str(error)))
        return True


PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentChatRoom</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         font: 14px/1.6 "Segoe UI", system-ui, sans-serif; background: #f4f6f5; color: #1c2430; }
  @media (prefers-color-scheme: dark) { body { background: #14181d; color: #e8ecef; } }
  .card { width: min(430px, 88vw); padding: 38px 34px; border: 1px solid rgba(125, 140, 150, 0.35);
          border-radius: 12px; text-align: center; background: #ffffff;
          box-shadow: 0 8px 30px rgba(20, 30, 40, 0.06); }
  @media (prefers-color-scheme: dark) { .card { background: #1b2129; box-shadow: none; } }
  .logo { width: 52px; height: 52px; margin: 0 auto 14px; border-radius: 12px;
          background: #0d7a5f; color: #ffffff; font-weight: 700; font-size: 20px; line-height: 52px; }
  h1 { font-size: 18px; margin: 0 0 6px; }
  .sub { margin: 0 0 24px; font-size: 13px; opacity: 0.75; }
  .row { display: flex; gap: 10px; justify-content: center; }
  input { width: 118px; padding: 9px 10px; font-size: 14px; text-align: center;
          border: 1px solid rgba(125, 140, 150, 0.45); border-radius: 8px;
          background: transparent; color: inherit; }
  button { padding: 9px 24px; font-size: 14px; border: 0; border-radius: 8px; cursor: pointer;
           background: #0d7a5f; color: #ffffff; }
  button:disabled { opacity: 0.55; cursor: default; }
  #status { margin-top: 18px; min-height: 20px; font-size: 13px; opacity: 0.85; }
  #status.err { color: #d64550; opacity: 1; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">AC</div>
  <h1>AgentChatRoom</h1>
  <p class="sub">服务未启动。确认端口后点击「启动服务」，管理界面将在此加载。</p>
  <div class="row">
    <input id="shell-port" inputmode="numeric" value="%PORT%" aria-label="服务端口">
    <button id="shell-start" onclick="startService()">启动服务</button>
  </div>
  <div id="status">就绪</div>
</div>
<script>
function setStatus(text, isError) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = isError ? 'err' : '';
}
async function startService() {
  const btn = document.getElementById('shell-start');
  btn.disabled = true;
  setStatus('正在启动服务…');
  try {
    const result = await window.pywebview.api.start_service(
      document.getElementById('shell-port').value
    );
    if (result && result.ok) {
      setStatus('服务已启动，正在加载管理界面…');
    } else {
      setStatus((result && result.error) || '启动失败', true);
      btn.disabled = false;
    }
  } catch (error) {
    setStatus('启动失败：' + error, true);
    btn.disabled = false;
  }
}
</script>
</body>
</html>
"""

TOPBAR_JS = r"""
(function() {
    if (window.__agentchatroom_shell_topbar) return;
    window.__agentchatroom_shell_topbar = true;
    const css = (el, text) => { el.style.cssText = text; return el; };
    const bar = document.createElement('div');
    bar.id = 'agentchatroom-shell-topbar';
    css(bar, [
        'position:sticky', 'top:0', 'left:0', 'right:0', 'z-index:2147483000',
        'height:46px', 'display:flex', 'align-items:center', 'gap:10px',
        'padding:0 14px', 'box-sizing:border-box', 'flex:none',
        'font:13px/1.4 "Segoe UI", system-ui, sans-serif',
        'background:var(--surface, #ffffff)', 'color:var(--text, #1c2430)',
        'border-bottom:1px solid var(--border, rgba(125,140,150,0.4))'
    ].join(';'));
    const dot = css(document.createElement('span'),
        'width:9px;height:9px;border-radius:50%;flex:none;background:var(--success,#1a7f37)');
    const label = css(document.createElement('span'),
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis');
    label.textContent = '服务状态检测中…';
    const spacer = css(document.createElement('span'), 'flex:1');
    const port = css(document.createElement('input'),
        'width:84px;padding:5px 8px;font-size:13px;text-align:center;border-radius:6px;'
        + 'border:1px solid var(--border,rgba(125,140,150,0.45));background:transparent;color:inherit');
    port.inputMode = 'numeric';
    port.setAttribute('aria-label', '服务端口');
    port.placeholder = '端口';
    const buttonCss = (bg) => [
        'padding:6px 16px','font-size:13px','border:0','border-radius:6px','cursor:pointer',
        'color:#ffffff','flex:none','background:' + bg
    ].join(';');
    const startBtn = css(document.createElement('button'), buttonCss('#0d7a5f'));
    startBtn.textContent = '启动服务';
    const stopBtn = css(document.createElement('button'), buttonCss('#b3556b'));
    stopBtn.textContent = '停止服务';
    bar.append(dot, label, spacer, port, startBtn, stopBtn);
    document.body.prepend(bar);

    // The SPA is a fixed 100vh app shell with internal scrolling; shrink it
    // by the bar height so the bottom of the page stays inside the viewport.
    document.documentElement.classList.add('agentchatroom-shell-bar-active');
    const style = document.createElement('style');
    style.id = 'agentchatroom-shell-topbar-style';
    style.textContent =
        '.agentchatroom-shell-bar-active .app-shell{'
        + 'height:calc(100vh - 46px)!important;min-height:calc(100vh - 46px)!important;}';
    document.head.appendChild(style);

    const apply = (status) => {
        if (!status) return;
        const running = Boolean(status.running);
        dot.style.background = running ? 'var(--success,#1a7f37)' : 'var(--muted,#9aa4ad)';
        label.textContent = running
            ? '● 运行中 ' + (status.url || '') + (status.pid ? '（pid ' + status.pid + '）' : '')
            : '○ 服务已停止';
        startBtn.disabled = running;
        stopBtn.disabled = !running;
        if (document.activeElement !== port && status.port) {
            port.value = String(status.port);
        }
    };
    const poll = async () => {
        try { apply(await window.pywebview.api.get_status()); } catch (error) { /* next tick */ }
    };
    startBtn.addEventListener('click', async () => {
        startBtn.disabled = true;
        startBtn.textContent = '启动中…';
        label.textContent = '正在启动服务…';
        try {
            const result = await window.pywebview.api.start_service(port.value);
            if (!(result && result.ok)) {
                label.textContent = (result && result.error) || '启动失败';
            }
        } catch (error) {
            label.textContent = '启动失败：' + error;
        } finally {
            startBtn.textContent = '启动服务';
            poll();
        }
    });
    stopBtn.addEventListener('click', async () => {
        stopBtn.disabled = true;
        label.textContent = '正在停止服务…';
        try { await window.pywebview.api.stop_service(); } catch (error) { /* reported below */ }
        poll();
    });
    poll();
    setInterval(poll, 2000);
})();
"""


class ShellJsApi:
    """JavaScript API bridge exposed to the webview window.

    Pure adapter over :class:`ServiceController`: no business logic lives here.
    """

    def __init__(self) -> None:
        self._window: Any = None
        self._shell: "GuiShell | None" = None

    def set_window(self, window: Any) -> None:
        self._window = window

    def bind_shell(self, shell: "GuiShell") -> None:
        self._shell = shell

    def _require_shell(self) -> "GuiShell":
        if self._shell is None:
            raise RuntimeError("shell is not ready")
        return self._shell

    def pick_directory(self, initial_path: str = "") -> str | None:
        """Open native system folder dialog via pywebview."""
        if self._window is None:
            return None
        try:
            import webview

            dialog_type = getattr(
                getattr(webview, "FileDialog", None),
                "FOLDER",
                getattr(webview, "FOLDER_DIALOG", 20),
            )
            result = self._window.create_file_dialog(
                dialog_type,
                directory=str(initial_path or ""),
            )
            if result and len(result) > 0:
                selected = str(result[0]).strip()
                if selected and Path(selected).is_dir():
                    return selected
            return None
        except Exception as error:
            logger.warning("Native folder picker failed: %s", redact_line(str(error)))
            return None

    def start_service(self, port: str = "") -> dict[str, Any]:
        """Start the detached local service on the requested port."""
        try:
            shell = self._require_shell()
        except RuntimeError as error:
            return {"ok": False, "error": str(error)}
        try:
            port_value = validate_port(str(port).strip() or str(shell.settings.port))
        except PortError as error:
            return {"ok": False, "error": str(error)}
        if not port_is_free(shell.settings.host, port_value):
            return {
                "ok": False,
                "error": (
                    f"端口 {port_value} 已被占用：可能已有一个服务在运行，"
                    "请先停止或更换端口。"
                ),
            }
        try:
            result = shell.controller.start(port_value)
        except (SystemExit, OSError) as error:
            return {"ok": False, "error": redact_line(str(error))}
        if not result.get("started") and not result.get("already_running"):
            return {"ok": False, "error": "服务启动失败，请查看服务日志"}

        note = ""
        if port_value != shell.settings.port:
            try:
                update_config_port(shell.config_file_path(), port_value)
                note = f"端口 {port_value} 已写入配置，后续启动将使用该端口。"
            except (OSError, ValueError) as error:
                note = f"配置写入失败：{error}"
            if os.getenv("AGENTCHATROOM_PORT", "").strip():
                note = f"{note} 检测到 AGENTCHATROOM_PORT 环境变量：环境变量优先。"

        shell.follow_running_service(port_value)
        if self._window is not None:
            try:
                self._window.load_url(shell.target.base_url)
            except Exception as error:
                logger.warning(
                    "Failed to navigate to service: %s", redact_line(str(error))
                )
        return {
            "ok": True,
            "running": True,
            "url": shell.target.base_url,
            "pid": result.get("pid"),
            "note": note,
        }

    def stop_service(self) -> dict[str, Any]:
        """Stop the detached local service and return to the placeholder page."""
        try:
            shell = self._require_shell()
        except RuntimeError as error:
            return {"ok": False, "error": str(error)}
        try:
            result = shell.controller.stop()
        except OSError as error:
            return {"ok": False, "error": redact_line(f"{type(error).__name__}: {error}")}
        stopped = bool(result.get("stopped")) or result.get("reason") == "not_running"
        if stopped and self._window is not None:
            try:
                self._window.load_html(shell.placeholder_html())
            except Exception as error:
                logger.warning(
                    "Failed to load placeholder page: %s", redact_line(str(error))
                )
        return {
            "ok": stopped,
            "reason": result.get("reason"),
            "running": shell.controller.is_running(),
        }

    def get_status(self) -> dict[str, Any]:
        """Report the current service state for the shell top bar."""
        try:
            shell = self._require_shell()
        except RuntimeError as error:
            return {"running": False, "error": str(error)}
        running = shell.controller.is_running()
        url = shell.target.base_url if running else None
        port = shell.settings.port
        if running:
            url = running_url_from_log(shell.controller.log_path) or url
            if url:
                try:
                    port = int(str(url).rsplit(":", 1)[-1])
                except ValueError:
                    pass
        return {
            "running": running,
            "pid": shell.controller.running_pid(),
            "url": url,
            "port": port,
        }


FOLDER_PICKER_INTERCEPT_JS = r"""
(function() {
    if (window.__agentchatroom_shell_bridge_ready) return;
    window.__agentchatroom_shell_bridge_ready = true;
    const origFetch = window.fetch;
    window.fetch = async function(url, options) {
        if (typeof url === 'string' && url.includes('/api/v1/local/folders/pick') && window.pywebview?.api?.pick_directory) {
            try {
                let initialPath = '';
                if (options?.body) {
                    try {
                        const parsed = JSON.parse(options.body);
                        initialPath = parsed.initial_path || '';
                    } catch (_) {}
                }
                const chosen = await window.pywebview.api.pick_directory(initialPath);
                if (chosen) {
                    return new Response(JSON.stringify({ cancelled: false, path: chosen }), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json' }
                    });
                } else {
                    return new Response(JSON.stringify({ cancelled: true, path: '' }), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json' }
                    });
                }
            } catch (err) {
                console.warn('Native folder dialog interception failed, falling back to network', err);
            }
        }
        return origFetch.apply(this, arguments);
    };
})();
"""


def prompt_close_action_win32(service_running: bool, window_title: str = WINDOW_TITLE) -> str:
    """Prompt user for closing action using native message box."""
    if not service_running:
        return CLOSE
    if os.name == "nt":
        # MB_YESNOCANCEL = 0x00000003 | MB_ICONQUESTION = 0x00000020
        msg = (
            "AgentChatRoom 服务仍在后台运行。\n\n"
            "【是】：结束后台服务并退出\n"
            "【否】：保留后台服务运行，仅关闭面板\n"
            "【取消】：不关闭窗口"
        )
        # IDYES = 6, IDNO = 7, IDCANCEL = 2
        ret = ctypes.windll.user32.MessageBoxW(0, msg, f"关闭 {window_title}", 0x00000003 | 0x00000020)
        if ret == 6:
            return STOP_AND_CLOSE
        if ret == 7:
            return KEEP_RUNNING
        return STAY
    return STOP_AND_CLOSE


class PanelTray:
    """System tray presence so a minimized panel is never lost.

    Pure window orchestration: left-click restores the panel, the menu's
    exit routes through the same closing semantics as the title bar close
    button. No business logic lives here.
    """

    def __init__(self, shell: "GuiShell") -> None:
        self.shell = shell
        self.icon: Any = None
        self.started = False

    def available(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        return True

    def _image(self) -> Any:
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([3, 3, 61, 61], radius=14, fill=(13, 106, 88, 255))
        draw.ellipse([16, 24, 32, 40], fill=(255, 255, 255, 255))
        draw.ellipse([34, 24, 50, 40], fill=(122, 199, 178, 255))
        return image

    def start(self) -> bool:
        """Start the detached tray icon; return False to fall back to the taskbar."""
        if not self.available():
            logger.info("Tray unavailable; minimized panel stays in the taskbar.")
            return False
        import pystray

        try:
            self.icon = pystray.Icon(
                "agentchatroom",
                self._image(),
                WINDOW_TITLE,
                menu=pystray.Menu(
                    pystray.MenuItem("打开面板", self.restore_panel, default=True),
                    pystray.MenuItem("退出", self.quit_from_tray),
                ),
            )
            self.icon.run_detached()
            self.started = True
            return True
        except Exception as error:
            logger.warning("Tray init failed: %s", redact_line(str(error)))
            self.icon = None
            return False

    def restore_panel(self, icon: Any = None, item: Any = None) -> None:
        """Bring the panel back to the foreground from tray/minimized state."""
        window = self.shell.window
        if window is None:
            return
        try:
            window.restore()
        except Exception:
            pass
        try:
            window.show()
        except Exception as error:
            logger.warning("Tray restore failed: %s", redact_line(str(error)))

    def quit_from_tray(self, icon: Any = None, item: Any = None) -> None:
        """Exit via the same three-choice closing semantics as the close button."""
        window = self.shell.window
        if not self.shell.on_window_closing():
            return
        self.stop()
        if window is not None:
            try:
                window.destroy()
            except Exception as error:
                logger.warning("Tray quit failed: %s", redact_line(str(error)))

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
        self.started = False


class GuiShell:
    """Orchestrates GUI window lifecycle and child process without business logic."""

    def __init__(
        self,
        settings: Settings,
        target: ServerTarget,
        client_config: ClientConfig,
    ) -> None:
        self.settings = settings
        self.target = target
        self.client_config = client_config
        self.controller = ServiceController(settings)
        self.js_api = ShellJsApi()
        self.js_api.bind_shell(self)
        self.tray = PanelTray(self)
        self.window: Any = None

    def config_file_path(self) -> Path:
        if self.settings.config_path:
            return Path(self.settings.config_path)
        return self.settings.data_dir / "config.toml"

    def placeholder_html(self) -> str:
        return PLACEHOLDER_HTML.replace("%PORT%", str(self.settings.port))

    def ensure_server_running(self) -> None:
        """In local mode, ensure detached server is running and healthy."""
        if self.target.mode != "local":
            return

        if not self.controller.is_running():
            effective_port = self.settings.port
            if not port_is_free(self.settings.host, effective_port):
                # Try to probe if it's already an existing AgentChatRoom service
                probe = self.target.probe(timeout=1.0)
                if not probe.ok:
                    raise TargetError(
                        f"Port {effective_port} is already occupied by another process"
                    )
            else:
                start_result = self.controller.start(effective_port)
                if not start_result.get("started") and not start_result.get("already_running"):
                    raise TargetError(f"Failed to start local service on port {effective_port}")

    def follow_running_service(self, port: int) -> None:
        """Rebind the target to the freshly started service's listening address."""
        new_target = resolve_server_target(
            replace(self.settings, port=port),
            self.client_config,
            effective_port=port,
        )
        actual_url = running_url_from_log(self.controller.log_path)
        if actual_url:
            try:
                new_target = ServerTarget.local(actual_url)
            except TargetError:
                pass
        self.target = new_target

    def on_window_loaded(self) -> None:
        """Inject shell bridges into backend pages (never into the placeholder)."""
        if not self.window:
            return
        try:
            current_url = self.window.get_current_url() or ""
            if current_url.startswith(self.target.base_url):
                self.window.evaluate_js(FOLDER_PICKER_INTERCEPT_JS)
                self.window.evaluate_js(TOPBAR_JS)
        except Exception as error:
            logger.warning(
                "Failed to inject shell bridges: %s", redact_line(str(error))
            )

    def on_window_minimized(self) -> None:
        """Minimize to tray when available so the panel is never lost."""
        if not self.tray.started or not self.window:
            return
        try:
            self.window.hide()
        except Exception as error:
            logger.warning("Minimize-to-tray failed: %s", redact_line(str(error)))

    def on_window_closing(self) -> bool:
        """Handle window close event with three choices.

        Returns True to allow window close, False to cancel.
        """
        if self.target.mode != "local":
            return True

        running = self.controller.is_running()
        action = prompt_close_action_win32(running, WINDOW_TITLE)
        if action == STAY:
            return False
        if action == STOP_AND_CLOSE:
            try:
                self.controller.stop()
            except Exception as error:
                logger.error("Failed to stop local service: %s", redact_line(str(error)))
            return True
        if action == KEEP_RUNNING:
            return True
        return True

    def launch(self, debug: bool = False) -> int:
        """Launch pywebview window or fallback to system browser."""
        try:
            import webview
        except ImportError:
            logger.warning("pywebview is not installed; falling back to system browser")
            webbrowser.open(self.target.base_url)
            return 0

        if self.controller.is_running():
            self.window = webview.create_window(
                title=WINDOW_TITLE,
                url=self.target.base_url,
                js_api=self.js_api,
                width=1440,
                height=900,
                min_size=(900, 600),
                confirm_close=False,
            )
        else:
            self.window = webview.create_window(
                title=WINDOW_TITLE,
                html=self.placeholder_html(),
                js_api=self.js_api,
                width=1440,
                height=900,
                min_size=(900, 600),
                confirm_close=False,
            )
        self.js_api.set_window(self.window)

        # Wire window events
        self.window.events.loaded += self.on_window_loaded
        self.window.events.closing += self.on_window_closing
        self.window.events.minimized += self.on_window_minimized

        self.tray.start()

        try:
            webview.start(debug=debug)
            return 0
        except Exception as error:
            logger.error(
                "WebView runtime error: %s; falling back to system browser",
                redact_line(str(error)),
            )
            webbrowser.open(self.target.base_url)
            return 0
        finally:
            self.tray.stop()


def run_shell(
    config_path: str | None = None,
    *,
    mode: str | None = None,
    url: str | None = None,
    port: int | None = None,
    debug: bool = False,
) -> int:
    """Entry point for running the GUI shell."""
    if not enforce_single_instance(WINDOW_TITLE):
        logger.info("Another instance is already running; brought to foreground.")
        return 0

    settings = load_settings(config_path)
    if port is not None:
        settings = replace(settings, port=validate_port(str(port)))

    client_config = load_client_config()
    target = resolve_server_target(
        settings,
        client_config,
        override_mode=mode,
        override_url=url,
        effective_port=settings.port,
    )

    # P1 ships the local panel only. Remote targets still exercise the shared
    # resolve + version handshake seam, but never open a management window.
    if target.mode == "remote":
        probe = target.probe(timeout=5.0)
        if not probe.ok:
            print(
                redact_line(f"Remote target {target.base_url} unreachable: {probe.error}"),
                file=sys.stderr,
            )
            return 1
        print(
            redact_line(
                f"Remote target {target.base_url} is reachable "
                f"(version {probe.version}, schema {probe.schema_version}); "
                "remote management arrives with S1-S3."
            )
        )
        return 0

    shell = GuiShell(settings, target, client_config)

    # Manual control is the default: the panel opens on the placeholder page
    # and the user starts/stops the service from the shell top bar. The
    # legacy auto-start behavior stays available via client.toml auto_start.
    if client_config.auto_start:
        try:
            shell.ensure_server_running()
        except Exception as error:
            print(redact_line(f"Error starting local service: {error}"), file=sys.stderr)
            return 1

        # Follow the serve child's actual listening address from its log so
        # the panel never points at a stale configured port.
        running_url = running_url_from_log(shell.controller.log_path)
        if running_url and ServerTarget.normalize_url(running_url) != target.base_url:
            target = ServerTarget.local(running_url)
            shell.target = target

        # Probe target: local and remote share the exact same version handshake.
        # A 401/403 response still proves the server is reachable; credentials
        # stay with the SPA login flow inside the panel, so only hard connection
        # failures block the window.
        probe = target.probe(timeout=5.0)
        if not probe.ok and probe.status_code not in (401, 403):
            print(
                redact_line(f"Error connecting to target {target.base_url}: {probe.error}"),
                file=sys.stderr,
            )
            return 1

    return shell.launch(debug=debug)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AgentChatRoom GUI Shell")
    parser.add_argument("--config", help="Path to server config.toml")
    parser.add_argument("--mode", choices=["local", "remote"], help="Server target mode")
    parser.add_argument("--url", help="Target server URL (for remote mode)")
    parser.add_argument("--port", type=int, help="Override local service port")
    parser.add_argument("--debug", action="store_true", help="Enable webview developer tools")
    args = parser.parse_args(argv)

    sys.exit(
        run_shell(
            config_path=args.config,
            mode=args.mode,
            url=args.url,
            port=args.port,
            debug=args.debug,
        )
    )


if __name__ == "__main__":
    main()
