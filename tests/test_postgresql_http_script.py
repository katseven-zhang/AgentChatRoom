from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_postgresql_http.py"


def test_postgresql_http_acceptance_script_has_help_entrypoint():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "real HTTP server process" in result.stdout


def test_postgresql_http_data_stays_under_verification_directory():
    source = SCRIPT.read_text(encoding="utf-8")

    assert ' / "verification"' in source
    assert ' / "postgres-http-acceptance"' in source
