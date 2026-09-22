import os
import subprocess
import sys
import tomllib
from pathlib import Path

from agentchatroom import __version__


ROOT = Path(__file__).parents[1]


def test_container_deployment_artifacts_are_present_and_secret_free() -> None:
    compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    env_example = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy" / "Caddyfile.example").read_text(encoding="utf-8")
    serverctl = (ROOT / "deploy" / "serverctl.py").read_text(encoding="utf-8")

    assert "AGENTCHATROOM_DATABASE_URL" in compose
    assert "AGENTCHATROOM_ADMIN_TOKEN" in compose
    server_config = (ROOT / "deploy" / "config.server.example.toml").read_text(
        encoding="utf-8"
    )
    assert "trusted_proxy_headers" in server_config
    assert "trusted_proxy_ips" in server_config
    assert "postgres:16" in compose
    assert "room-data" in compose
    assert "/health/ready" in compose
    assert '127.0.0.1:${AGENTCHATROOM_BIND_PORT:-8765}:8765' in compose
    assert '"${AGENTCHATROOM_BIND_PORT:-8765}:8765"' not in compose
    assert "postgresql-client" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "pip install --no-cache-dir \".[postgresql]\"" in dockerfile
    assert "replace-with" in env_example
    assert "room.example.com" in caddy
    assert "def deployment_preflight" in serverctl
    assert '"--volumes"' in serverctl
    assert "AGENTCHATROOM_DATABASE_URL" in serverctl
    assert "proxy_file" in serverctl
    assert "postgresql://user:password" not in compose + dockerfile + env_example
    assert "acr." not in compose + dockerfile + env_example


def test_deploy_config_is_ignored_but_example_is_trackable() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "deploy/.env" in gitignore
    assert "deploy/config.server.toml" in gitignore
    assert "deploy/Caddyfile" in gitignore
    assert (ROOT / "deploy" / ".env.example").is_file()


def test_local_agent_runtime_state_is_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for path in [".codex/", ".grok/", ".trae/", ".workbuddy/"]:
        assert path in gitignore
    assert "docs/" in gitignore
    assert ".agentchatroom/" in gitignore
    assert "python/post_room_update.py" in gitignore


def test_runtime_and_package_versions_match() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == __version__ == "0.2.4"


def test_readme_documents_cross_platform_support_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "核心 Python 后端、Web 前端、CLI 和 MCP 服务支持" in readme
    assert "Windows、Linux 和 macOS" in readme
    assert "完整的 Windows 一键启动与关闭入口" in readme
    assert ".venv/bin/agentchatroom serve --open-browser" in readme
    assert ".venv/bin/agentchatroom stop" in readme


def test_windows_launcher_generator_is_portable_and_config_driven(tmp_path) -> None:
    generator = ROOT / "python" / "regen_launcher_cmds.py"
    source = generator.read_text(encoding="utf-8")

    assert "D:\\\\claw" not in source
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "ascii"
    result = subprocess.run(
        [sys.executable, str(generator), "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("generated:") == 3

    start_bytes = (tmp_path / "启动 AgentChatRoom.cmd").read_bytes()
    stop_bytes = (tmp_path / "关闭 AgentChatRoom.cmd").read_bytes()
    gui_bytes = (tmp_path / "AgentChatRoom 控制台.cmd").read_bytes()
    start = start_bytes.decode("gbk")
    stop = stop_bytes.decode("gbk")
    gui = gui_bytes.decode("gbk")

    assert b"\r\n" in start_bytes
    assert b"\n" not in start_bytes.replace(b"\r\n", b"")
    assert b"\r\n" in stop_bytes
    assert b"\n" not in stop_bytes.replace(b"\r\n", b"")
    assert b"\r\n" in gui_bytes
    assert b"\n" not in gui_bytes.replace(b"\r\n", b"")
    assert "serve --open-browser" in start
    assert "serve --detach" not in start
    assert "日志实时显示在本窗口" in start
    assert "load_settings().port" in stop
    assert ":8765" not in stop
    assert "bootstrap.cmd" not in stop
    assert "taskkill /T /F /PID" in stop
    assert "没有发现占用 %AGENTCHATROOM_PORT% 端口" in stop
    assert "没有发现占用 8765 端口" not in stop
    assert "bootstrap.cmd" in gui
    assert "-m agentchatroom gui" in gui
    assert "AGENTCHATROOM_ACCESS_LOG" in gui
    assert "8765" not in gui


def test_ci_uses_node_24_action_runtimes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/checkout@v7" in workflow
    assert "actions/setup-python@v7" in workflow


def test_windows_package_workflow_builds_and_uploads_single_exe() -> None:
    workflow = (ROOT / ".github" / "workflows" / "package-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "name: Package Windows EXE" in workflow
    assert "runs-on: windows-latest" in workflow
    assert 'python -m pip install -e ".[dev,gui]"' in workflow
    assert 'python -m pip install "pyinstaller==6.22.2"' in workflow
    assert "python -m PyInstaller --clean --noconfirm agentchatroom.spec" in workflow
    assert "dist\\agentchatroom\\agentchatroom.exe" in workflow
    assert "agentchatroom-windows-x64.zip" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "types:" in workflow and "published" in workflow
    assert "softprops/action-gh-release@v2" in workflow
    assert "github.event.release.tag_name" in workflow
    assert "release_tag:" in workflow
    assert "inputs.release_tag" in workflow
    assert "AGENTCHATROOM_TEST_EXE" in workflow
    assert 'if ($LASTEXITCODE -ne 0) { throw "Source regression tests failed" }' in workflow
    assert "tests/test_stdio_runtime.py" in workflow
    assert "test_local_mcp_stdio_waits_for_bootstrap_and_disconnects_on_exit" in workflow


def test_spec_hidden_imports_match_package_modules() -> None:
    """#201: agentchatroom.* hidden_imports must all exist under src/agentchatroom."""
    import re

    spec_text = (ROOT / "agentchatroom.spec").read_text(encoding="utf-8")
    entries = re.findall(r'"(agentchatroom(?:\.[A-Za-z0-9_]+)*)"', spec_text)
    assert entries, "spec must declare agentchatroom hidden imports"
    package_dir = ROOT / "src" / "agentchatroom"
    missing = []
    for entry in entries:
        parts = entry.split(".")[1:]  # drop package root
        if not parts:
            continue
        module_path = package_dir.joinpath(*parts)
        if not (
            module_path.with_suffix(".py").is_file()
            or (module_path / "__init__.py").is_file()
            or module_path.is_dir()
        ):
            missing.append(entry)
    assert missing == [], f"stale hidden imports: {missing}"
    assert "agentchatroom.models" not in entries

    # Modules called out by R8 review must stay on the packaging list.
    for required in (
        "agentchatroom.mcp_compat",
        "agentchatroom.mcp_http_adoption",
        "agentchatroom.mcp_http_recovery",
        "agentchatroom.http_identity",
        "agentchatroom.credential_bundle",
        "agentchatroom.postgres_database",
        "agentchatroom.backup",
    ):
        assert required in entries, required


def test_example_configs_include_documents_inject_max_chars() -> None:
    """#201: README-documented [documents].inject_max_chars must be discoverable."""
    for relative in ("config.example.toml", "deploy/config.server.example.toml"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "[documents]" in text, relative
        assert "inject_max_chars = 12000" in text, relative


def test_example_configs_parse_as_valid_toml() -> None:
    """#175/#201: both example files must be loadable TOML (no duplicate tables)."""
    for relative in ("config.example.toml", "deploy/config.server.example.toml"):
        tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_readme_config_keys_appear_in_example_configs() -> None:
    """#201: every CONFIG_FILE_SCHEMA key documented in README appears in an example.

    Key-level (not section-level): build a single parseable inventory of
    section.key pairs from both example TOMLs and require each README-mentioned
    schema key to be present.
    """
    import re

    from agentchatroom.config import CONFIG_FILE_SCHEMA

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example_docs = [
        tomllib.loads((ROOT / path).read_text(encoding="utf-8"))
        for path in ("config.example.toml", "deploy/config.server.example.toml")
    ]

    present: set[tuple[str, str]] = set()
    for doc in example_docs:
        for section, values in doc.items():
            if isinstance(values, dict):
                for key in values:
                    present.add((section, key))

    mentioned_section_keys = set(
        re.findall(r"`([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)`", readme)
    )
    schema_key_names = {
        key for keys in CONFIG_FILE_SCHEMA.values() for key in keys
    }
    mentioned_bare = {
        key
        for key in re.findall(r"`([a-z][a-z0-9_]{3,})`", readme)
        if key in schema_key_names
    }

    missing: list[str] = []
    for section, keys in CONFIG_FILE_SCHEMA.items():
        for key in keys:
            documented = (section, key) in mentioned_section_keys or key in mentioned_bare
            if not documented:
                continue
            if (section, key) not in present:
                missing.append(f"{section}.{key}")
    assert missing == [], (
        "README documents config keys missing from example TOMLs: "
        + ", ".join(missing)
    )


def test_readme_config_sections_appear_in_example_configs() -> None:
    """#201: every CONFIG_FILE_SCHEMA section README mentions is in some example."""
    from agentchatroom.config import CONFIG_FILE_SCHEMA

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    examples = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in ("config.example.toml", "deploy/config.server.example.toml")
    )
    for section, keys in CONFIG_FILE_SCHEMA.items():
        mentioned = (
            f"[{section}]" in readme
            or any(f"{section}.{key}" in readme for key in keys)
        )
        if not mentioned:
            continue
        discoverable = f"[{section}]" in examples or any(
            key in examples for key in keys
        )
        assert discoverable, (
            f"README documents section/key [{section}] but no example config shows it"
        )
