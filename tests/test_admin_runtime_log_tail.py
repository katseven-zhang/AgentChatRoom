"""Task #282: the runtime view must not read the whole log, and a bad pid file must not 500.

``GET /api/v1/admin/runtime`` asked for the last ``lines`` entries but obtained
them with ``read_text()`` of the entire ``server.log`` - which nothing rotates -
and decoded ``server.pid`` with ``encoding="ascii"``, so a pid file holding any
non-ASCII byte raised UnicodeDecodeError and the management endpoint returned 500.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agentchatroom.api import create_app
from agentchatroom.runtime_log import tail_text_lines


def _write_log(path: Path, count: int, *, width: int = 40) -> None:
    path.write_text(
        "".join(f"{index:0>{width}} line\n" for index in range(count)),
        encoding="utf-8",
    )


def test_tail_returns_the_newest_lines_without_reading_the_head(tmp_path):
    log = tmp_path / "server.log"
    _write_log(log, 5000)

    lines, truncated = tail_text_lines(log, 80)

    assert truncated is False
    assert len(lines) == 80
    assert lines[-1].endswith("4999 line")
    assert lines[0].endswith("4920 line")


def test_tail_stops_at_its_byte_budget_and_keeps_the_newest(tmp_path):
    log = tmp_path / "server.log"
    _write_log(log, 20_000)

    # Asking for more lines than the byte budget can reach proves the scan is
    # bounded: it returns the newest block it managed to read and says so.
    lines, truncated = tail_text_lines(
        log, 100_000, chunk_bytes=4096, max_scan_bytes=8192
    )

    assert truncated is True
    assert 0 < len(lines) <= 100_000
    assert lines[-1].endswith("19999 line")
    assert lines[0].endswith("line"), lines[0]


def test_tail_zero_limit_reads_nothing(tmp_path):
    log = tmp_path / "server.log"
    _write_log(log, 100)
    assert tail_text_lines(log, 0) == ([], False)


def test_tail_survives_undecodable_bytes_and_a_missing_file(tmp_path):
    log = tmp_path / "server.log"
    log.write_bytes(b"line one\n\xff\xfe broken byte\nline three\n")

    lines, truncated = tail_text_lines(log, 10)

    assert truncated is False
    assert len(lines) == 3
    assert lines[0] == "line one"
    assert lines[2] == "line three"
    assert tail_text_lines(tmp_path / "absent.log", 10) == ([], False)


def test_admin_runtime_tolerates_a_corrupt_pid_file(settings, tmp_path):
    (Path(settings.data_dir)).mkdir(parents=True, exist_ok=True)
    (Path(settings.data_dir) / "server.pid").write_bytes(b"not-ascii-\xff\xfe")
    _write_log(Path(settings.data_dir) / "server.log", 500)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/admin/runtime?lines=50")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["process"]["pid"] == ""
    assert body["process"]["managed"] is False
    assert body["log"]["returned"] == 50
    assert body["log"]["size_bytes"] > 0
    assert body["log"]["truncated"] is False


def test_admin_runtime_reports_a_live_pid(settings):
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    (Path(settings.data_dir) / "server.pid").write_text("4242\n", encoding="ascii")

    with TestClient(create_app(settings)) as client:
        body = client.get("/api/v1/admin/runtime").json()

    assert body["process"]["pid"] == "4242"
    assert body["process"]["managed"] is True
