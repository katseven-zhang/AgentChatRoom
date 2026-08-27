from __future__ import annotations

from pathlib import Path

from agentchatroom.config import load_settings
from agentchatroom.database import Database
from agentchatroom.services import AgentChatRoomService
from scripts.audit_public_release import ROOT, audit_working_tree, scan_text


def test_secret_findings_do_not_include_the_secret_value():
    candidate = "github_pat_" + ("A" * 40)
    assignment = "api_" + f'key = "{candidate}"'

    findings = scan_text(
        assignment,
        path="config.example.toml",
        source="fixture",
    )

    assert [finding.rule for finding in findings] == ["github_token"]
    assert candidate not in repr(findings)


def test_public_surface_rejects_live_ids_and_user_paths():
    findings = scan_text(
        (
            "Project project_0123456789abcdef is under "
            "C:\\Users\\someone\\work via 10.24.8.9"
        ),
        path="docs/example.md",
        source="fixture",
    )

    assert {finding.rule for finding in findings} == {
        "live_domain_id",
        "private_lan_address",
        "windows_absolute_path",
    }


def test_source_surface_rejects_environment_specific_paths():
    findings = scan_text(
        'placeholder="X:\\private\\workspace"',
        path="src/agentchatroom/web/index.html",
        source="fixture",
    )

    assert [finding.rule for finding in findings] == ["windows_absolute_path"]


def test_repository_public_release_surface_is_clean():
    findings = audit_working_tree(Path(ROOT))

    assert findings == []


def test_fresh_checkout_creates_only_an_empty_ignored_runtime(monkeypatch, tmp_path):
    checkout = tmp_path / "fresh-checkout"
    (checkout / "src" / "agentchatroom").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "agentchatroom"\n', encoding="utf-8"
    )
    monkeypatch.delenv("AGENTCHATROOM_DATA_DIR", raising=False)
    monkeypatch.setenv("AGENTCHATROOM_ROOT", str(checkout))

    settings = load_settings()
    service = AgentChatRoomService(Database(settings.database_path), settings)
    service.initialize()

    expected_data_dir = (checkout / ".agentchatroom" / "runtime").resolve()
    assert settings.data_dir == expected_data_dir
    assert settings.database_path == expected_data_dir / "agentchatroom.db"
    assert settings.database_path.is_file()
    assert service.list_projects() == []
    assert list(checkout.rglob("*.db")) == [settings.database_path]
