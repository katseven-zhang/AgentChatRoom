"""Bounded reading of the growing local service log.

`server.log` is the whole stdout/stderr of the detached service and nothing
rotates it yet, so "show me the last 80 lines" must not cost memory and time
proportional to the file. Both surfaces that show the log — the management
runtime panel and `agentchatroom logs` — read it through this helper.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

TAIL_CHUNK_BYTES = 65536
TAIL_SCAN_BYTE_LIMIT = 8 * 1024 * 1024


def tail_text_lines(
    path: Path,
    limit: int,
    *,
    chunk_bytes: int = TAIL_CHUNK_BYTES,
    max_scan_bytes: int = TAIL_SCAN_BYTE_LIMIT,
) -> tuple[list[str], bool]:
    """Return the last ``limit`` lines and whether the scan hit its byte budget."""
    if limit <= 0:
        return [], False
    try:
        size = path.stat().st_size
    except OSError:
        return [], False
    scanned = 0
    end = size
    buffer = b""
    truncated = False
    with path.open("rb") as handle:
        while end > 0 and buffer.count(b"\n") <= limit:
            start = max(0, end - chunk_bytes)
            handle.seek(start)
            buffer = handle.read(end - start) + buffer
            scanned += end - start
            end = start
            if scanned >= max_scan_bytes:
                truncated = end > 0
                break
    if truncated and end > 0:
        # Drop the first partial line so the caller never sees a fragment.
        newline = buffer.find(b"\n")
        if newline != -1:
            buffer = buffer[newline + 1 :]
    lines = buffer.decode("utf-8", errors="replace").splitlines()
    return lines[-limit:], truncated


# The address a detached service announces on startup, e.g.
# "INFO: Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)".
_RUNNING_URL = re.compile(r"running on (https?://\S+)")

# server.log grows forever and nothing rotates it, and the lookup runs on a
# poll tick, so the reverse tail is bounded twice: a line window for the common
# case and a byte cap for a pathological one.
RUNNING_URL_TAIL_LINES = 400
RUNNING_URL_TAIL_BYTES = 256 * 1024


def running_url_from_log(
    log_path: Path,
    tail_reader: Callable[..., tuple[list[str], bool]] | None = None,
) -> str | None:
    """Return the newest actual listening address recorded in the server log.

    Callers must prefer this over an address they were merely asked for: the
    requested host/port may not be what the running process bound, and
    reporting the request back as "already running at ..." sends a caller to
    the wrong port.

    ``tail_reader`` is the bounded reverse reader to use. It is a parameter so
    each caller keeps its own injection point: the GUI replaces its module
    attribute to prove the read stays bounded, and a caller that wants a
    different window can pass its own.
    """
    reader = tail_reader or tail_text_lines
    try:
        lines, _truncated = reader(
            Path(log_path),
            RUNNING_URL_TAIL_LINES,
            max_scan_bytes=RUNNING_URL_TAIL_BYTES,
        )
    except OSError:
        return None
    found: str | None = None
    for line in lines:
        match = _RUNNING_URL.search(line)
        if match:
            found = match.group(1)
    return found
