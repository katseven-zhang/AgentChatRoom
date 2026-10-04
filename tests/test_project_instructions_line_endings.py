"""Task #310: registering a checkout must not rewrite a user file's line endings.

The writer already detects the separator of the existing AGENTS.md and rebuilds
its block with that same separator -- and then handed the text to a text-mode
write that translated every ``\n`` to ``os.linesep``. On Windows that turned a
user's LF AGENTS.md (and the LF JSON registration file) into CRLF, so a tool that
only manages a marked block rewrote the whole file in the diff.
"""

from __future__ import annotations

from pathlib import Path

from agentchatroom.project_registration import (
    register_checkout_project,
    write_project_coordination_instructions,
)

LF_SEED = b"# User rules\n\n- keep LF line endings\n"
CRLF_SEED = b"# User rules\r\n\r\n- keep CRLF line endings\r\n"


def _project(name: str = "line-endings") -> dict:
    return {
        "id": "project_lineendings",
        "name": name,
        "project_key": "pkey-lineendings",
        "root_path": "",
    }


def test_lf_agents_md_stays_lf_after_the_managed_block_is_written(tmp_path: Path):
    root = tmp_path / "lf-checkout"
    root.mkdir()
    path = root / "AGENTS.md"
    path.write_bytes(LF_SEED)

    result = write_project_coordination_instructions(root, _project())

    assert result["action"] in {"appended", "created"}
    data = path.read_bytes()
    assert b"\r\n" not in data, "the managed block rewrote the file to CRLF"
    assert b"# User rules" in data


def test_crlf_agents_md_keeps_crlf_in_the_managed_block_too(tmp_path: Path):
    root = tmp_path / "crlf-checkout"
    root.mkdir()
    path = root / "AGENTS.md"
    path.write_bytes(CRLF_SEED)

    write_project_coordination_instructions(root, _project())

    data = path.read_bytes()
    assert b"\r\n" in data
    # Every newline must be CRLF: a lone LF means the block was written with the
    # other separator and the file is now mixed.
    assert b"\n" not in data.replace(b"\r\n", b"")


def test_refresh_of_an_lf_file_does_not_change_its_line_endings(tmp_path: Path):
    root = tmp_path / "refresh-checkout"
    root.mkdir()
    path = root / "AGENTS.md"
    path.write_bytes(LF_SEED)

    write_project_coordination_instructions(root, _project())
    after_first = path.read_bytes()
    write_project_coordination_instructions(root, _project("renamed"))
    after_second = path.read_bytes()

    assert after_first.count(b"\r") == 0
    assert after_second.count(b"\r") == 0
    assert b"renamed" in after_second


def test_registered_checkout_document_is_written_with_lf(tmp_path: Path):
    root = tmp_path / "json-checkout"
    root.mkdir()
    project = _project()
    project["root_path"] = str(root)

    register_checkout_project(root, project)

    document = (root / ".agentchatroom" / "project.json").read_bytes()
    assert b"\r\n" not in document
