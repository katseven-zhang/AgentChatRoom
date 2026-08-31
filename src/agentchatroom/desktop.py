from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


class DirectoryPickerUnavailable(RuntimeError):
    """Raised when the local Python runtime cannot display a folder picker."""


_PICKER_SCRIPT = r"""
import json
import sys

root = None
try:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update_idletasks()
    except tk.TclError:
        pass
    selected = filedialog.askdirectory(
        parent=root,
        title="选择项目文件夹",
        initialdir=sys.argv[1],
        mustexist=True,
    )
    print(json.dumps({"path": selected}, ensure_ascii=True))
except Exception:
    print(json.dumps({"error": "folder_picker_unavailable"}))
    raise SystemExit(2)
finally:
    if root is not None:
        try:
            root.destroy()
        except Exception:
            pass
"""


def _initial_directory(value: str) -> Path:
    candidate = Path(value).expanduser() if value.strip() else None
    if candidate is not None and candidate.is_dir():
        return candidate.resolve()
    home = Path.home()
    return home.resolve() if home.is_dir() else Path.cwd().resolve()


def pick_directory(initial_path: str = "") -> str | None:
    """Open a native directory chooser in an isolated local Python process."""

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PICKER_SCRIPT, str(_initial_directory(initial_path))],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DirectoryPickerUnavailable("Local folder picker is unavailable") from error

    output = next(
        (line for line in reversed(completed.stdout.splitlines()) if line.strip()),
        "",
    )
    try:
        payload = json.loads(output) if output else {}
    except json.JSONDecodeError as error:
        raise DirectoryPickerUnavailable("Local folder picker returned invalid data") from error
    if completed.returncode != 0 or payload.get("error"):
        raise DirectoryPickerUnavailable("Local folder picker is unavailable")

    selected = str(payload.get("path", "")).strip()
    if not selected:
        return None
    resolved = Path(selected).expanduser().resolve()
    if not resolved.is_dir():
        raise DirectoryPickerUnavailable("Selected folder no longer exists")
    return str(resolved)
