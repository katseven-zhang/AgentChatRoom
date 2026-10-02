"""Bounded reading of the growing local service log.

`server.log` is the whole stdout/stderr of the detached service and nothing
rotates it yet, so "show me the last 80 lines" must not cost memory and time
proportional to the file. Both surfaces that show the log — the management
runtime panel and `agentchatroom logs` — read it through this helper.
"""

from __future__ import annotations

from pathlib import Path

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
