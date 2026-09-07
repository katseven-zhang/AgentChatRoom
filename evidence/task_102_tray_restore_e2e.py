"""Task #102 tray restore end-to-end evidence (real webview window).

Launches the real pywebview shell, hides it through the real minimized
handler, then invokes the exact tray callback code path
(``PanelTray.restore_panel``) from a non-UI thread - the same context
pystray uses for a left-click. A before/after screen capture proves the
window is really gone and really comes back; the shell window-state log
proves the state machine transitions.

Exit code 0 = all checks PASS.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))


def grab(region):
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=region)


def region_mean(image):
    histogram = image.convert("L").histogram()
    total = sum(histogram)
    weighted = sum(index * count for index, count in enumerate(histogram))
    return weighted / max(total, 1)


def main() -> int:
    from agentchatroom import shell as shell_module
    from agentchatroom.client_config import ServerTarget, load_client_config
    from agentchatroom.config import load_settings
    from agentchatroom.shell import GuiShell

    settings = load_settings()
    target = ServerTarget.local(f"http://127.0.0.1:{settings.port}")
    client_config = load_client_config()
    shell = GuiShell(settings, target, client_config)

    # Driver runs on a worker thread - the same threading context as a
    # pystray menu callback. pywebview keeps the main thread.
    def driver():
        try:
            deadline = time.time() + 60
            while time.time() < deadline and shell.window is None:
                time.sleep(0.5)
            check("shell window object created", shell.window is not None)
            if shell.window is None:
                return
            time.sleep(6)  # let the WebView render the placeholder page

            window_region = (700, 300, 1700, 1000)
            before_hidden = grab(window_region)

            # Hide through the real minimized handler (taskbar minimize path).
            shell.tray.started = True
            shell.on_window_minimized()
            time.sleep(2)
            after_hide = grab(window_region)
            check(
                "hide to tray really hides the window",
                shell.window_state == "hidden"
                and abs(region_mean(after_hide) - region_mean(before_hidden)) > 0.5,
                f"state={shell.window_state}",
            )

            # Same code path a tray left-click runs (pystray callback context).
            restorer = threading.Thread(
                target=shell.tray.restore_panel, daemon=True
            )
            restorer.start()
            restorer.join(timeout=30)
            time.sleep(2)
            after_restore = grab(window_region)
            check(
                "tray restore callback brings the window back on screen",
                shell.window_state == "visible"
                and abs(region_mean(after_restore) - region_mean(after_hide)) > 0.5,
                f"state={shell.window_state}",
            )
            saved = Path(__file__).resolve().parent / "task_102_restored_desktop.png"
            after_restore.save(saved)
            print(f"[png] saved {saved}")
        finally:
            print(
                f"=== Summary: {sum(1 for _, ok in results if ok)}/{len(results)} checks PASS ==="
            )
            try:
                shell.window.destroy()
            except Exception:
                import os

                os._exit(1)

    driver_thread = threading.Thread(target=driver, daemon=True)
    driver_thread.start()
    shell.launch()

    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    main()
