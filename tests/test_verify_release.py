from __future__ import annotations

import json

import scripts.verify_release as verify_release


def test_normalize_base_url_accepts_mcp_endpoint() -> None:
    assert verify_release.normalize_base_url(" https://room.example.com/mcp/ ") == "https://room.example.com"


def test_release_preflight_uses_read_only_checks(monkeypatch, capsys) -> None:
    def fake_request(url, *, timeout, headers):
        if url.endswith("/health/live"):
            return 200, {"status": "ok"}
        if url.endswith("/health/ready"):
            return 200, {"status": "ready"}
        if url.endswith("/api/v1/config/public"):
            return 200, {"domain": {}}
        raise AssertionError(url)

    class StaticResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"asset"

    monkeypatch.setattr(verify_release, "request_json", fake_request)
    monkeypatch.setattr(verify_release, "urlopen", lambda *args, **kwargs: StaticResponse())
    monkeypatch.setenv("AGENTCHATROOM_SERVER_URL", "http://example.test/mcp")

    assert verify_release.main(["--no-source-checks"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["base_url"] == "http://example.test"
    assert output["credential_used"] is False
    assert output["passed"] is True


def test_release_preflight_reports_unready_center(monkeypatch, capsys) -> None:
    def fake_request(url, *, timeout, headers):
        if url.endswith("/health/live"):
            return 200, {"status": "ok"}
        if url.endswith("/health/ready"):
            return 503, {"status": "not_ready"}
        if url.endswith("/api/v1/config/public"):
            return 200, {"domain": {}}
        raise AssertionError(url)

    class StaticResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"asset"

    monkeypatch.setattr(verify_release, "request_json", fake_request)
    monkeypatch.setattr(verify_release, "urlopen", lambda *args, **kwargs: StaticResponse())

    assert verify_release.main(["--no-source-checks"]) == 1
    output = json.loads(capsys.readouterr().out)
    ready = next(item for item in output["checks"] if item["name"] == "health_ready")
    assert ready["passed"] is False
