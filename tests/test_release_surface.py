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


# --- #325 sub-item 2: both release paths install from uv.lock ---------------

DOCKER_REQUIREMENTS = ROOT / "requirements-docker.txt"
WINDOWS_REQUIREMENTS = ROOT / "requirements-windows.txt"
DOCKER_EXPORT = [
    "uv", "export", "--frozen", "--no-dev", "--extra", "postgresql",
    "--no-editable", "--no-emit-project",
]
WINDOWS_EXPORT = [
    "uv", "export", "--frozen", "--no-dev", "--extra", "dev", "--extra", "gui",
    "--no-editable", "--no-emit-project",
]


def _requirement_specs(path: Path) -> dict[str, str]:
    """Pinned ``name -> version`` pairs from a generated requirements file."""
    specs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]+)==([^ ;]+)", line)
        if match:
            specs[match.group(1).lower().replace("_", "-")] = match.group(2)
    assert specs, f"{path.name} carries no pinned requirement"
    return specs


def _declared_dependency_names(extra: str | None = None) -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    table = "dependencies = ["
    if extra is None:
        start = text.index(table)
    else:
        start = text.index(f"{extra} = [", text.index("[project.optional-dependencies]"))
    block = text[start : text.index("]", start)]
    return [
        match.group(1).lower().replace("_", "-").split("[")[0]
        for match in re.finditer(r'"([A-Za-z0-9][A-Za-z0-9._-]+)', block)
    ]


def _hash_count(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("--hash=sha256:")


def test_both_release_paths_install_from_generated_lock_files():
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "package-windows.yml").read_text(
        encoding="utf-8"
    )
    assert "pip install --no-cache-dir --require-hashes -r requirements-docker.txt" in dockerfile
    assert 'pip install --no-cache-dir ".[postgresql]"' not in dockerfile
    assert "python -m pip install --require-hashes -r requirements-windows.txt" in workflow
    assert 'python -m pip install -e ".[dev,gui]"' not in workflow
    assert 'python -m pip install --upgrade pip' not in workflow


def test_declared_dependencies_are_pinned_and_hashed_in_both_files():
    assert DOCKER_REQUIREMENTS.exists(), "uv.lock must be exported for the image path"
    assert WINDOWS_REQUIREMENTS.exists(), "uv.lock must be exported for the EXE path"
    docker_specs = _requirement_specs(DOCKER_REQUIREMENTS)
    windows_specs = _requirement_specs(WINDOWS_REQUIREMENTS)
    for name in _declared_dependency_names():
        assert docker_specs.get(name) == windows_specs.get(name), name
        assert name in docker_specs, f"{name} is declared but never pinned"
    for extra, expected in (
        ("postgresql", ("psycopg",)),
        ("dev", ("pytest", "httpx")),
        ("gui", ("pystray", "pillow")),
    ):
        specs = windows_specs if extra != "postgresql" else docker_specs
        for name in expected:
            assert name in specs, f"{extra} extra does not pin {name}"
    assert _hash_count(DOCKER_REQUIREMENTS) > 0
    assert _hash_count(WINDOWS_REQUIREMENTS) > 0


def test_build_backend_version_is_pinned():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires = ["setuptools==' in text
    assert 'requires = ["setuptools>=' not in text


@pytest.mark.parametrize(
    ("export_args", "committed"),
    [(DOCKER_EXPORT, DOCKER_REQUIREMENTS), (WINDOWS_EXPORT, WINDOWS_REQUIREMENTS)],
    ids=["docker", "windows"],
)
def test_generated_files_reproduce_byte_for_byte_from_the_lock(export_args, committed, tmp_path):
    """The committed files are exactly what ``uv export --frozen`` produces today."""
    import shutil

    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    out = tmp_path / committed.name
    subprocess.run(
        export_args + ["-o", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    # The header line records the -o path, so compare the resolved content only.
    def specs(path: Path) -> list[str]:
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    assert specs(out) == specs(committed)


def test_build_context_guard_accepts_the_repository_and_rejects_growth():
    import importlib

    guard = importlib.import_module("scripts.check_build_context")
    assert guard.main(["--context-root", str(ROOT)]) == 0
    assert guard.main(["--context-root", str(ROOT), "--max-mib", "0.001"]) == 1


def test_build_context_drops_local_data_and_keeps_application_code():
    import importlib

    guard = importlib.import_module("scripts.check_build_context")
    rules = guard.load_dockerignore(ROOT / ".dockerignore")
    assert guard.is_excluded(rules, ".agentchatroom/agentchatroom.db")
    assert guard.is_excluded(rules, "docs/notes.md")
    assert guard.is_excluded(rules, ".git/objects/pack/pack-1.idx")
    assert not guard.is_excluded(rules, "src/agentchatroom/web/app.js")
    assert not guard.is_excluded(rules, "requirements-docker.txt")


def test_ci_runs_the_release_reproducibility_guards():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Check release requirements are still derived from uv.lock" in ci
    assert "git diff --exit-code requirements-docker.txt requirements-windows.txt" in ci
    assert "python scripts/check_build_context.py" in ci
