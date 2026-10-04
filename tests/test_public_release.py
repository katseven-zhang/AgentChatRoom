# audit-allow-file:windows_absolute_path,private_lan_address,live_domain_id,developer_hostname,windows_user_home
from __future__ import annotations

from pathlib import Path

from agentchatroom.config import load_settings
from agentchatroom.database import Database
from agentchatroom.services import AgentChatRoomService
from scripts.audit_public_release import (
    ROOT,
    _is_public_surface,
    _path_findings,
    _scan_bytes,
    audit_working_tree,
    machine_value_rules,
    scan_text,
)


def test_every_published_path_is_a_public_surface():
    """#311: the scanner used to allowlist the surfaces it checked, which made
    it blind to exactly the places private data piles up."""
    for path in (
        "evidence/acceptance.xml",
        "packaging/build.cmd",
        "tests/test_something.py",
        "run_server.cmd",
        "pyproject.toml",
        "agentchatroom.spec",
        "README.md",
        "src/agentchatroom/services.py",
    ):
        assert _is_public_surface(path), path
    # Only the auditor itself is exempt: it carries the literal detectors.
    assert not _is_public_surface("scripts/audit_public_release.py")


def test_serialized_hostname_and_user_home_are_findings():
    xml = '<testsuite name="pytest" tests="1" hostname="DEVBOX-42">'
    rules = {
        finding.rule
        for finding in scan_text(xml, path="evidence/a.xml", source="fixture")
    }
    assert "developer_hostname" in rules

    dump_rules = {
        finding.rule
        for finding in scan_text(
            "COMPUTERNAME=DEVBOX-42", path="evidence/a.dump", source="fixture"
        )
    }
    assert "developer_hostname" in dump_rules

    home_rules = {
        finding.rule
        for finding in scan_text(
            "cwd=C:\\Users\\devuser\\repo", path="evidence/a.txt", source="fixture"
        )
    }
    assert {"windows_absolute_path", "windows_user_home"} <= home_rules


def test_binary_payloads_are_still_scanned():
    """A NUL byte used to mean 'skip this file entirely'."""
    payload = b"\x89PNG\r\n\x1a\n" + b'<testsuite hostname="DEVBOX-42"/>'
    png_findings = _scan_bytes(payload, path="evidence/shot.png", source="fixture")
    assert "developer_hostname" in {finding.rule for finding in png_findings}

    zip_like = b"PK\x03\x04\x00" + b"project_0123456789abcdef"
    assert "live_domain_id" in {
        finding.rule for finding in _scan_bytes(zip_like, path="evidence/a.zip", source="fixture")
    }

    # A genuinely opaque binary outside the container list stays skipped.
    assert _scan_bytes(b"\x00\x01\x02hostname=\"DEVBOX-42\"", path="a.bin", source="fixture") == []


def test_evidence_directory_is_forbidden_and_directives_are_per_rule():
    assert "runtime_path_tracked" in {
        finding.rule for finding in _path_findings("evidence/a.xml", "working-tree")
    }
    # Acceptance dumps and screenshots are local proof, never index content.
    assert "acceptance_dump_tracked" in {
        finding.rule for finding in _path_findings("other/a.xml", "working-tree")
    }
    assert "acceptance_dump_tracked" in {
        finding.rule for finding in _path_findings("docs/shot.png", "working-tree")
    }

    line = "workspace = 'C:\\Users\\devuser\\repo'"
    assert "windows_absolute_path" in {
        finding.rule for finding in scan_text(line, path="src/a.py", source="fixture")
    }
    # An exemption is a per-rule, visible decision, never a blind spot.
    assert scan_text(
        line + "  # audit-allow:windows_absolute_path,windows_user_home",
        path="src/a.py",
        source="fixture",
    ) == []


def test_machine_specific_values_are_reported_wherever_they_appear():
    import socket

    rules = {rule for rule, _ in machine_value_rules()}
    assert "this_machine_hostname" in rules
    findings = scan_text(
        f"log line mentioning {socket.gethostname()}",
        path="tests/a.py",
        source="fixture",
    )
    assert "this_machine_hostname" in {finding.rule for finding in findings}


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
        "windows_user_home",
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


def test_max_findings_turns_a_known_backlog_into_a_ratchet(monkeypatch, capsys) -> None:
    """#370: the history scan cannot gate a PR while its backlog exists.

    The tests carry Windows-shaped paths and hostnames on purpose, so the trunk
    already reports findings nobody can act on. Blocking on those would stall
    every pull request. A ceiling instead fails only when the count rises,
    which is the part that means a new leak arrived.
    """
    import scripts.audit_public_release as audit

    def fixed_findings(_root):
        return [
            audit.Finding("git:abc", "evidence/old.xml", None, "developer_hostname"),
            audit.Finding("git:abc", "tests/test_x.py", 12, "windows_absolute_path"),
        ]

    monkeypatch.setattr(audit, "audit_history", fixed_findings)
    monkeypatch.setattr(audit, "ROOT", audit.Path("."))
    monkeypatch.setattr(
        __import__("sys"), "argv", ["audit", "--history", "--max-findings", "5"]
    )
    assert audit.main() == 0, "a backlog under the ceiling must not fail"

    monkeypatch.setattr(
        __import__("sys"), "argv", ["audit", "--history", "--max-findings", "1"]
    )
    assert audit.main() == 1, "exceeding the ceiling must fail"
    assert "exceeds the baseline" in capsys.readouterr().out


def test_max_findings_defaults_to_no_ceiling(monkeypatch) -> None:
    """Omitting the flag keeps the original all-or-nothing behaviour."""
    import scripts.audit_public_release as audit

    monkeypatch.setattr(
        audit,
        "audit_working_tree",
        lambda _root: [audit.Finding("working-tree", "a", None, "runtime_path_tracked")],
    )
    monkeypatch.setattr(audit, "ROOT", audit.Path("."))
    monkeypatch.setattr(__import__("sys"), "argv", ["audit"])
    assert audit.main() == 1
    monkeypatch.setattr(
        __import__("sys"), "argv", ["audit", "--max-findings", "10"]
    )
    # The ceiling replaces the verdict: under it the finding is reported and
    # tolerated, which is the whole point - a backlog that still fails the job
    # would block every pull request and change nothing.
    assert audit.main() == 0
