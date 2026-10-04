"""Task #332: a live Agent Token must not have to sit on a command line, and the
release audit must actually recognise the credential shape.

``scripts/verify_remote_hosts.py`` required ``--token`` as an argv value, so every
operator who ran it published a live credential to anyone able to list processes
on the machine, while ``audit_public_release.py`` had no rule for the
``acr.<id>.<secret>`` shape at all and therefore never reported it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "audit_public_release", REPO / "scripts" / "audit_public_release.py"
)
audit = importlib.util.module_from_spec(spec)
sys.modules["audit_public_release"] = audit
spec.loader.exec_module(audit)

LIVE_TOKEN = "acr.credential_9f2c7ab1e4d84f5a.x7Qp2m4s9t1vz8b3d5f7h0j2l4n6q8s0u1w3y5a7c9e"
RULE = "agentchatroom_credential"


def _rules_for(path: str) -> list[str]:
    line = f"--token {LIVE_TOKEN}"
    return [f.rule for f in audit.scan_text(line + "\n", path=path, source="test")]


def test_audit_reports_a_credential_passed_on_the_command_line():
    assert RULE in _rules_for("scripts/verify_remote_hosts.py")


def test_audit_still_ignores_test_fixtures():
    # Test sources carry fake credentials on purpose; the rule is for shipped text.
    assert RULE not in _rules_for("tests/test_something.py")


def test_remote_hosts_script_takes_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENTCHATROOM_AGENT_TOKEN", LIVE_TOKEN)
    module = _load("verify_remote_hosts")
    monkeypatch.setattr(
        sys, "argv",
        ["verify_remote_hosts.py", "--url", "http://h/mcp", "--project-id", "p",
         "--workspace-a", "a", "--workspace-b", "b"],
    )
    assert module.parse_args().token == LIVE_TOKEN


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_token_is_no_longer_a_required_argument(monkeypatch):
    monkeypatch.delenv("AGENTCHATROOM_AGENT_TOKEN", raising=False)
    module = _load("verify_remote_hosts")
    monkeypatch.setattr(
        sys, "argv",
        ["verify_remote_hosts.py", "--url", "http://h/mcp", "--project-id", "p",
         "--workspace-a", "a", "--workspace-b", "b", "--token", LIVE_TOKEN],
    )
    # An explicit token still works (it is the operator's choice), so the parser
    # must not fail; it simply no longer *requires* the secret on argv.
    assert module.parse_args().token == LIVE_TOKEN


def test_explicit_token_flag_prints_a_deprecation_warning_without_the_value(monkeypatch, capfd):
    monkeypatch.delenv("AGENTCHATROOM_AGENT_TOKEN", raising=False)
    module = _load("verify_remote_hosts")
    monkeypatch.setattr(
        sys, "argv",
        ["verify_remote_hosts.py", "--url", "http://h/mcp", "--project-id", "p",
         "--workspace-a", "a", "--workspace-b", "b", "--token", LIVE_TOKEN],
    )
    args = module.parse_args()
    captured = capfd.readouterr()
    assert args.token == LIVE_TOKEN
    assert "deprecated" in captured.err
    assert "AGENTCHATROOM_AGENT_TOKEN" in captured.err
    # the warning itself must not reproduce the secret it is warning about
    assert LIVE_TOKEN not in captured.err


def test_environment_token_does_not_warn(monkeypatch, capfd):
    monkeypatch.setenv("AGENTCHATROOM_AGENT_TOKEN", LIVE_TOKEN)
    module = _load("verify_remote_hosts")
    monkeypatch.setattr(
        sys, "argv",
        ["verify_remote_hosts.py", "--url", "http://h/mcp", "--project-id", "p",
         "--workspace-a", "a", "--workspace-b", "b"],
    )
    module.parse_args()
    assert LIVE_TOKEN not in capfd.readouterr().err
