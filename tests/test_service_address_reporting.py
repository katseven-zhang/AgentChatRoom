"""Task #350: report the address the service actually listens on.

``start_detached_server`` answered ``already_running`` with the host/port the
caller had *asked for* rather than the one the running process bound, so the
GUI cached a port the service was not on and wrote it into config.toml.

The address lookup now lives in ``runtime_log`` so the CLI and the GUI read the
same log the same way, and a runtime-learned address round-trips through
``config.parse_service_url`` instead of being re-derived from the request.
"""

from __future__ import annotations

from dataclasses import replace

from agentchatroom.config import build_service_base_url, parse_service_url
from agentchatroom.runtime_log import running_url_from_log


def test_the_log_address_is_found_and_ignores_a_stale_earlier_one(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(
        "\n".join(
            [
                "INFO: Started server process [1234]",
                "INFO: Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C)",
                "INFO: service restarted on a new port",
                "INFO: Uvicorn running on http://127.0.0.1:9100 (Press CTRL+C)",
            ]
        ),
        encoding="utf-8",
    )
    # The newest announcement wins: an old one would send the caller to a
    # port the service has since moved away from.
    assert running_url_from_log(log) == "http://127.0.0.1:9100"


def test_no_announced_address_yields_nothing_rather_than_a_guess(tmp_path):
    log = tmp_path / "server.log"
    log.write_text(
        "INFO: Started server process [1234]\nINFO: waiting for application startup\n",
        encoding="utf-8",
    )
    assert running_url_from_log(log) is None
    # A missing log is not an error: the caller falls back to the request.
    assert running_url_from_log(tmp_path / "absent.log") is None


def test_a_runtime_address_round_trips_back_to_host_and_port():
    """Storing a learned address must not produce a bracketed host."""
    for host, port in (("127.0.0.1", 8765), ("::1", 9100), ("203.0.113.7", 8080)):
        url = build_service_base_url(host, port)
        assert parse_service_url(url) == (host, port)
    assert parse_service_url("") == ("", None)
    assert parse_service_url("not a url") == ("", None)


def test_already_running_reports_the_bound_address_not_the_requested_one(
    settings, tmp_path, monkeypatch
):
    """The reported failure: asked for 9000, service on 8765, GUI cached 9000."""
    from agentchatroom import cli

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "server.pid").write_text("4242", encoding="ascii")
    (data_dir / "server.log").write_text(
        "INFO: Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)\n",
        encoding="utf-8",
    )
    local = replace(settings, data_dir=data_dir)
    monkeypatch.setattr(cli, "process_is_running", lambda pid: True)

    result = cli._start_detached_server_locked(local, "127.0.0.1", 9000)

    assert result["already_running"] is True
    # The bound address, not the one that was asked for.
    assert result["url"] == "http://127.0.0.1:8765"
    # The request stays visible so the caller can tell the two apart.
    assert result["requested_url"] == "http://127.0.0.1:9000"
