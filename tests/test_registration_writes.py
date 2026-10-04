"""#365: writing the checkout registration must not quietly grow or lose.

Three ways it went wrong, all on a file every Agent in the repo shares: a
marker that stopped being recognised so a second block was appended, a
read-modify-write with no lock so two bootstraps lost one another's entry, and
an entry rebuilt from scratch so keys written by another build disappeared.
"""

from __future__ import annotations

import pytest

from agentchatroom import mcp_server
from agentchatroom.errors import DomainError


def _local_identity(monkeypatch):
    """Local MCP refuses to join without a stable software identity."""
    monkeypatch.setenv(mcp_server.SOFTWARE_KEY_ENV, "reg-client")
    monkeypatch.setenv(mcp_server.SOFTWARE_NAME_ENV, "RegClient")
    monkeypatch.setenv(mcp_server.SOFTWARE_CLIENT_ENV, "reg-client")
from agentchatroom.project_registration import (
    PROJECT_INSTRUCTIONS_BEGIN,
    PROJECT_INSTRUCTIONS_END,
    _load_document,
    _managed_instructions_range,
    _write_document,
    project_registration_path,
)


def test_a_marker_with_surrounding_blanks_is_still_the_marker(tmp_path):
    """An editor that trims or indents the line does not create a second block.

    The old pattern anchored the marker to column zero, so the range came back
    empty, the writer appended a fresh block, and AGENTS.md grew one copy per
    bootstrap.
    """
    text = "\n".join(
        [
            "# My notes",
            "",
            f"  {PROJECT_INSTRUCTIONS_BEGIN}  ",
            "managed content",
            f"\t{PROJECT_INSTRUCTIONS_END}\t",
            "",
        ]
    )
    target = tmp_path / "AGENTS.md"
    target.write_text(text, encoding="utf-8")

    found = _managed_instructions_range(target, text)
    assert found is not None, "an indented marker was not recognised"
    start, end = found
    assert text[start : end + 1].strip().startswith(
        PROJECT_INSTRUCTIONS_BEGIN.strip()
    )


def test_asymmetric_or_duplicated_markers_are_refused(tmp_path):
    """Already refuse - pinned so the blank tolerance cannot weaken it."""
    target = tmp_path / "AGENTS.md"

    only_begin = f"intro\n{PROJECT_INSTRUCTIONS_BEGIN}\nbody\n"
    with pytest.raises(DomainError):
        _managed_instructions_range(target, only_begin)

    duplicated = (
        f"{PROJECT_INSTRUCTIONS_BEGIN}\na\n{PROJECT_INSTRUCTIONS_END}\n"
        f"{PROJECT_INSTRUCTIONS_BEGIN}\nb\n{PROJECT_INSTRUCTIONS_END}\n"
    )
    with pytest.raises(DomainError):
        _managed_instructions_range(target, duplicated)


def test_a_locked_write_leaves_no_trace_for_the_next_reader(tmp_path):
    """The lock is taken and released around each write, not held afterwards.

    A lock file that outlives the write would block every later bootstrap; a
    lock that is not taken would let two Agents interleave their read-modify-
    write. Both are observable here.
    """
    path = project_registration_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_document(path, {"schema_version": 1, "registrations": []})
    _write_document(path, {"schema_version": 1, "registrations": []})

    leftovers = [
        item.name for item in path.parent.iterdir() if ".lock" in item.name
    ]
    assert leftovers == [], leftovers


def test_the_lock_is_exclusive_while_held(tmp_path):
    """A held lock refuses another writer rather than interleaving with it."""
    from agentchatroom.project_registration import _acquire_registration_lock

    path = project_registration_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    held = _acquire_registration_lock(path)
    try:
        import agentchatroom.project_registration as module

        # A second attempt inside the wait window gives up with a refusal that
        # tells the caller to retry, rather than a 500 or a corrupted file.
        original_wait = module._REGISTRATION_LOCK_WAIT_SECONDS
        module._REGISTRATION_LOCK_WAIT_SECONDS = 0.2
        try:
            with pytest.raises(DomainError) as refused:
                _acquire_registration_lock(path)
        finally:
            module._REGISTRATION_LOCK_WAIT_SECONDS = original_wait
        assert refused.value.code == "project_registration_locked"
        assert refused.value.status_code == 409
        assert refused.value.details["required_action"]
    finally:
        held.unlink()


def test_a_failed_write_leaves_no_temporary_file_and_no_lock(tmp_path):
    """Litter inside the user's checkout is also the evidence of the failure."""
    # A directory where the document should be makes write_text fail.
    blocker = tmp_path / "blocked.json"
    blocker.mkdir(parents=True)

    with pytest.raises(DomainError):
        _write_document(blocker, {"schema_version": 1, "registrations": []})

    leftovers = [
        item.name
        for item in blocker.parent.iterdir()
        if item.name.endswith(".tmp") or item.name.endswith(".lock")
    ]
    assert leftovers == [], leftovers


def test_an_unknown_key_written_by_another_build_survives(service, project):
    """The entry is merged, not rebuilt.

    The registration file is shared by every checkout in a repo and written by
    every build in use; replacing the entry dropped whatever the other side had
    recorded.
    """
    from agentchatroom import project_registration as module

    root = project["root_path"]
    path = project_registration_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    created = service.create_project(root_path=str(root), logical_path="")
    module.register_checkout_project(root, created)

    # A newer build records a key this one knows nothing about.
    document = module._load_document(path)
    document["registrations"][0]["recorded_by_a_future_build"] = "keep me"
    module._write_document(path, document)

    module.register_checkout_project(root, dict(created))

    entry = module._load_document(path)["registrations"][0]
    assert entry["recorded_by_a_future_build"] == "keep me", entry


def test_the_lock_is_released_after_a_successful_write(tmp_path):
    path = project_registration_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_document(path, {"schema_version": 1, "registrations": []})

    locks = [item.name for item in path.parent.iterdir() if item.name.endswith(".lock")]
    assert locks == [], locks
    # And a second write is not blocked by the first one's leftovers.
    _write_document(path, {"schema_version": 1, "registrations": []})


def test_a_stale_lock_is_broken_rather_than_blocking_forever(tmp_path):
    """A writer that was killed must not lock the checkout permanently."""
    import os
    import time

    import agentchatroom.project_registration as module

    path = project_registration_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(f".{path.name}.lock")
    handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(handle, b"999999")
    os.close(handle)
    # Backdate it past the stale threshold.
    old = time.time() - module._REGISTRATION_LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))

    _write_document(path, {"schema_version": 1, "registrations": []})
    assert not lock.exists(), "the stale lock was not broken"
