"""Task #325: the release surface must not carry local data, and the Windows
launchers must not silently disagree with the generator that produces them.

``deploy/compose.yaml`` builds with ``context: ..``, and the repository had no
``.dockerignore`` at all, so every build uploaded the local Room database
(``.agentchatroom/`` -- live Session tokens, message bodies, machine paths), the
ignored ``docs/`` and ``evidence/`` notes, ``.git``, ``.venv`` and the build
output to the Docker daemon. ``python/regen_launcher_cmds.py`` writes GBK+CRLF
while ``.gitattributes`` normalised every ``.cmd`` to LF, so a checkout produced
files that contradicted the generator, and the existing test only ever looked at
files it generated itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Written by python/regen_launcher_cmds.py; scripts/bootstrap.cmd is a different,
# hand-maintained file and is not part of this contract.
LAUNCHERS = (
    "启动 AgentChatRoom.cmd",
    "关闭 AgentChatRoom.cmd",
    "AgentChatRoom 控制台.cmd",
)

PRIVATE_PATTERNS = (
    ".agentchatroom/",
    "docs/",
    "evidence/",
    ".git/",
    ".venv/",
    "build/",
    "dist/",
    ".env",
    "*.log",
    "*.db",
    "tests/",
)


def _ignore_lines() -> list[str]:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _dockerfile_copy_sources() -> list[str]:
    text = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    sources: list[str] = []
    for match in re.finditer(r"(?m)^COPY\s+(--\S+\s+)?(.+)$", text):
        for token in match.group(2).split():
            if not token.startswith("--"):
                sources.append(token)
    return sources


@pytest.mark.parametrize("pattern", PRIVATE_PATTERNS)
def test_build_context_excludes_local_and_heavy_paths(pattern: str):
    assert pattern in _ignore_lines(), (
        f".dockerignore must exclude {pattern!r}: the compose build context is the"
        " repository root, and .agentchatroom/ in particular holds the live Room"
        " database"
    )


def test_dockerignore_still_ships_everything_the_dockerfile_copies():
    ignored_dirs = {
        line.rstrip("/") for line in _ignore_lines() if line.endswith("/")
    }
    missing = [
        source
        for source in _dockerfile_copy_sources()
        if source.split("/")[0] in ignored_dirs or source in ignored_dirs
    ]
    assert not missing, f".dockerignore would break the image build: {missing}"


def test_gitattributes_declares_windows_launchers_as_crlf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.cmd text eol=crlf" in text


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launchers_match_the_generator_after_a_git_round_trip(name: str, tmp_path: Path):
    """The checked-out bytes must be exactly what the generator intends.

    ``text eol=crlf`` stores LF inside the repository by design and materialises
    CRLF in the working tree, so the committed blob is compared with newlines
    folded: the contract is that a checkout reproduces GBK+CRLF and that git
    round-trips it without corrupting the encoding.
    """
    generator = ROOT / "python" / "regen_launcher_cmds.py"
    spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        "regen_launcher_cmds", generator
    )
    module = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
    spec.loader.exec_module(module)

    worktree = (ROOT / name).read_bytes()
    assert b"\r\n" in worktree, f"{name} lost CRLF in the working tree"
    worktree_text = worktree.decode("gbk")
    assert module.launcher_bytes(worktree_text) == worktree

    committed = subprocess.run(
        ["git", "show", "HEAD:" + name],
        capture_output=True,
        cwd=ROOT,
    ).stdout
    assert committed == module.launcher_bytes(worktree_text).replace(b"\r\n", b"\n")
    assert committed.decode("gbk").replace("\r\n", "\n") == worktree_text.replace(
        "\r\n", "\n"
    )
