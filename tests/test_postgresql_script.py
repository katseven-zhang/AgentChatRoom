from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_postgresql.py"


def test_postgresql_acceptance_script_has_help_entrypoint():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Verify AgentChatRoom against a disposable PostgreSQL server" in result.stdout


def test_postgresql_acceptance_data_stays_under_verification_directory():
    source = SCRIPT.read_text(encoding="utf-8")

    assert (
        'ROOT / ".agentchatroom" / "verification" / "postgres-acceptance"'
        in source
    )
