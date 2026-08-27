from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_remote_hosts.py"


def test_remote_hosts_acceptance_script_has_help_entrypoint():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Verify two isolated remote AgentChatRoom Hosts" in result.stdout
