from __future__ import annotations

import json

import pytest

import scripts.verify_release as verify_release


def test_normalize_base_url_accepts_mcp_endpoint() -> None:
    assert verify_release.normalize_base_url(" https://room.example.com/mcp/ ") == "https://room.example.com"


def test_release_preflight_uses_read_only_checks(monkeypatch, capsys) -> None:
    def fake_request(url, *, timeout, headers):
        if url.endswith("/health/live"):
            return 200, {"status": "ok", "version": verify_release.SOURCE_VERSION}
        if url.endswith("/health/ready"):
            return 200, {"status": "ready"}
        if url.endswith("/api/v1/config/public"):
            return 200, {
                "domain": {},
                "config_schema_version": verify_release.SOURCE_SCHEMA_VERSION,
            }
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
            return 200, {
                "domain": {},
                "config_schema_version": verify_release.SOURCE_SCHEMA_VERSION,
            }
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


def _silent_urlopen(monkeypatch):
    """Neutralise the source-level gates; these tests are about the probes."""

    class StaticResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"asset"

    monkeypatch.setattr(verify_release, "urlopen", lambda *args, **kwargs: StaticResponse())


def _probe(monkeypatch, *, live_version, schema_version):
    def fake_request(url, *, timeout, headers):
        if url.endswith("/health/live"):
            return 200, {"status": "ok", "version": live_version}
        if url.endswith("/health/ready"):
            return 200, {"status": "ready"}
        if url.endswith("/api/v1/config/public"):
            return 200, {
                "domain": {},
                "config_schema_version": schema_version,
            }
        raise AssertionError(url)

    monkeypatch.setattr(verify_release, "request_json", fake_request)
    _silent_urlopen(monkeypatch)


def test_a_stale_build_in_place_fails_the_preflight(monkeypatch, capsys) -> None:
    """The reported gap: an old EXE answers 200 on every probe and passed.

    Reaching a server is not the claim. A release preflight that certifies a
    build other than this source tree is worse than no preflight, because the
    green result is what a release decision rests on.
    """
    _probe(
        monkeypatch,
        live_version="0.0.1",
        schema_version=verify_release.SOURCE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("AGENTCHATROOM_SERVER_URL", "http://example.test")

    assert verify_release.main(["--no-source-checks"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is False
    live = next(check for check in output["checks"] if check["name"] == "health_live")
    assert live["passed"] is False
    assert live["expected_value"] == verify_release.SOURCE_VERSION
    assert live["observed_value"] == "0.0.1"
    assert "version mismatch" in live["error"]


def test_a_mismatched_config_schema_fails_the_preflight(monkeypatch, capsys) -> None:
    """A server may answer correctly and still parse the config differently."""
    _probe(
        monkeypatch,
        live_version=verify_release.SOURCE_VERSION,
        schema_version=verify_release.SOURCE_SCHEMA_VERSION + 1,
    )
    monkeypatch.setenv("AGENTCHATROOM_SERVER_URL", "http://example.test")

    assert verify_release.main(["--no-source-checks"]) == 1
    output = json.loads(capsys.readouterr().out)
    public = next(
        check for check in output["checks"] if check["name"] == "public_config"
    )
    assert public["passed"] is False
    assert public["expected_value"] == verify_release.SOURCE_SCHEMA_VERSION


def test_an_admin_token_is_never_sent_over_cleartext(monkeypatch) -> None:
    """The module documents itself as credential-free; that has to hold."""
    sent: list[dict[str, str]] = []

    def fake_request(url, *, timeout, headers):
        sent.append(dict(headers))
        if url.endswith("/health/ready"):
            return 200, {"status": "ready"}
        return 200, {
            "status": "ok",
            "version": verify_release.SOURCE_VERSION,
            "domain": {},
            "config_schema_version": verify_release.SOURCE_SCHEMA_VERSION,
        }

    monkeypatch.setattr(verify_release, "request_json", fake_request)
    _silent_urlopen(monkeypatch)
    monkeypatch.setenv("AGENTCHATROOM_SERVER_URL", "http://example.test")
    monkeypatch.setenv("AGENTCHATROOM_ADMIN_TOKEN", "a-token-long-enough-to-be-real")

    with pytest.raises(SystemExit) as refused:
        verify_release.main(["--no-source-checks"])
    assert "refusing to send" in str(refused.value)
    assert sent == [], "the token reached the wire before the check"


def test_an_admin_token_is_sent_over_https(monkeypatch, capsys) -> None:
    """Refusing http must not mean refusing the credential entirely."""
    sent: list[dict[str, str]] = []

    def fake_request(url, *, timeout, headers):
        sent.append(dict(headers))
        if url.endswith("/health/ready"):
            return 200, {"status": "ready"}
        return 200, {
            "status": "ok",
            "version": verify_release.SOURCE_VERSION,
            "domain": {},
            "config_schema_version": verify_release.SOURCE_SCHEMA_VERSION,
        }

    monkeypatch.setattr(verify_release, "request_json", fake_request)
    _silent_urlopen(monkeypatch)
    monkeypatch.setenv("AGENTCHATROOM_SERVER_URL", "https://example.test")
    monkeypatch.setenv("AGENTCHATROOM_ADMIN_TOKEN", "a-token-long-enough-to-be-real")

    assert verify_release.main(["--no-source-checks"]) == 0
    assert sent and all("Authorization" in headers for headers in sent)
