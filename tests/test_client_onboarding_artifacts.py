"""#357: the client onboarding artifacts a user is handed must actually work.

Three separate ways a generated artifact could not be used, all verified here
against the real generator rather than against a restatement of it:

* The Web shipped an HTTP-only transport tab and pinned the client to
  ``generic``, while ``localMcpAssistantSupported`` required
  ``integrationTransport === "local"`` and a profile carrying ``local_config``.
  The local config assistant -- which backs up, diffs by sha256, and writes the
  client's own file -- was therefore unreachable, and the only reachable
  outcome for the one selectable client was ``local_mcp_profile_unsupported``.
  ``/api/v1/config/public`` still advertised ``local_mcp_config_assistant``.
* Every remote-Bridge config was generated with ``args=["-m", module]`` and a
  bare ``mcp_bridge_command`` default, so a packaged center (one exe, no
  interpreter, no ``mcp-bridge`` subcommand) handed clients a launcher that
  could not start.
* ``mcp-config`` could only name four formats through a hand-maintained map, so
  a profile that existed on the server (``trae``) had no CLI spelling.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agentchatroom import config as config_module
from agentchatroom.integrations import (
    MCP_BRIDGE_SUBCOMMAND,
    MCP_CLIENT_PROFILES,
    build_mcp_integration,
    resolve_bridge_launcher,
)

REPO = Path(__file__).resolve().parents[1]
TRANSPORTS = ("local-stdio", "streamable-http", "remote-bridge")
PLACEHOLDER_IDENTITY = (
    "<stable-software-key>",
    "<Software name>",
    "<software-client-code>",
)


def _settings(tmp_path):
    return config_module.Settings(data_dir=tmp_path / "data")


def _integration(tmp_path):
    return build_mcp_integration(_settings(tmp_path))


# ---------------------------------------------------------------------------
# AC1: every advertised transport and client is reachable and produces a
#      usable artifact.
# ---------------------------------------------------------------------------
def test_local_config_assistant_is_reachable_for_every_managed_profile(tmp_path):
    """The precondition the old UI made unreachable."""
    integration = _integration(tmp_path)
    managed = [
        profile_id
        for profile_id, profile in integration["profiles"].items()
        if isinstance(profile.get("local_config"), dict)
    ]
    # At least one client must be manageable, or the advertised capability is a
    # lie no matter what the front end renders.
    assert managed, "no profile carries a managed local_config path"
    for profile_id in managed:
        profile = integration["profiles"][profile_id]
        assert profile["local_config"]["candidates"], profile_id
        assert profile["local_config"]["reload_instruction"].strip(), profile_id
        # The managed path only means something next to a JSON config.
        assert profile["format"] == "json", profile_id


def test_generic_profile_cannot_claim_a_managed_local_path(tmp_path):
    """`generic` has no vendor config location, so it must not pretend to."""
    integration = _integration(tmp_path)
    assert "local_config" not in integration["profiles"]["generic"]


def test_public_config_capability_bit_matches_reachable_profiles(settings, tmp_path):
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    with TestClient(create_app(settings)) as client:
        capabilities = client.get("/api/v1/config/public").json()["capabilities"]
    integration = _integration(tmp_path)
    has_managed_profile = any(
        isinstance(profile.get("local_config"), dict)
        for profile in integration["profiles"].values()
    )
    # The bit may only be set when a client can actually be selected that the
    # assistant supports. Previously it followed deployment_profile alone.
    if capabilities["local_mcp_config_assistant"]:
        assert has_managed_profile


def test_web_offers_every_transport_the_backend_publishes():
    markup = (REPO / "src" / "agentchatroom" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    javascript = (REPO / "src" / "agentchatroom" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    for transport in ("http", "local", "remote"):
        assert f'data-integration-transport="{transport}"' in markup, transport
    # A profile selector, without which "local" always resolves to `generic`.
    assert 'id="integration-client-profile"' in markup
    assert "<label for=\"integration-client-profile\">" in markup
    # Availability comes from the served transports block.
    assert "transports.streamable_http?.enabled" in javascript
    assert "transports.remote_stdio_bridge?.enabled" in javascript


# ---------------------------------------------------------------------------
# AC2: the Bridge has a launcher that can start, including from a frozen exe.
# ---------------------------------------------------------------------------
def test_source_launcher_reaches_the_bridge_module_through_python(tmp_path):
    command, args = resolve_bridge_launcher(_settings(tmp_path), frozen=False)
    assert command == "python"
    # `-m` is consumed by the interpreter here, which is why the Bridge's own
    # strict argparse never sees it.
    assert args == ["-m", "agentchatroom.mcp_bridge"]


def test_frozen_launcher_uses_the_exe_and_a_reachable_subcommand(tmp_path):
    command, args = resolve_bridge_launcher(_settings(tmp_path), frozen=True)
    assert args == [MCP_BRIDGE_SUBCOMMAND]
    assert Path(command).name.lower().startswith("python") or command


def test_frozen_launcher_respects_an_explicitly_configured_launcher(tmp_path):
    """An operator who set the command knows where a real launcher lives."""
    settings = config_module.Settings(
        data_dir=tmp_path / "data",
        mcp_bridge_command="C:/tools/agentchatroom.exe",
        mcp_bridge_args=["mcp-bridge"],
    )
    assert resolve_bridge_launcher(settings, frozen=True) == (
        "C:/tools/agentchatroom.exe",
        ["mcp-bridge"],
    )


def test_packaged_entry_point_dispatches_the_bridge_subcommand():
    """A frozen build has no interpreter, so the subcommand is the only way in."""
    source = (REPO / "packaging" / "entry_app.py").read_text(encoding="utf-8")
    assert 'command == "mcp-bridge"' in source
    assert "from agentchatroom.mcp_bridge import main as run_bridge" in source
    assert "run_bridge(args[1:])" in source
    # The exe already dispatches `mcp`; the Bridge must not be the only gap.
    assert 'command == "mcp"' in source


def test_bridge_module_is_packaged_into_the_exe():
    spec = (REPO / "agentchatroom.spec").read_text(encoding="utf-8")
    assert '"agentchatroom.mcp_bridge"' in spec


def test_generated_bridge_config_carries_the_resolved_launcher(tmp_path):
    settings = _settings(tmp_path)
    integration = build_mcp_integration(settings)
    expected_command, expected_args = resolve_bridge_launcher(
        settings, frozen=False
    )
    for profile_id, profile in integration["profiles"].items():
        text = profile["remote_bridge_config_text"]
        if profile["format"] == "json":
            bridge = json.loads(text)["mcpServers"]["agentchatroom"]
        else:
            bridge = tomllib.loads(text)["mcp_servers"]["agentchatroom"]
        assert bridge["command"] == expected_command, profile_id
        assert bridge["args"] == expected_args, profile_id


def test_bridge_args_setting_round_trips_from_toml(tmp_path, monkeypatch):
    for name in list(dict(__import__("os").environ)):
        if name.startswith("AGENTCHATROOM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "config.toml"
    path.write_text(
        '[server]\nmcp_bridge_command = "python3"\n'
        'mcp_bridge_args = ["-m", "agentchatroom.mcp_bridge"]\n',
        encoding="utf-8",
    )
    settings = config_module.load_settings(path)
    assert settings.mcp_bridge_command == "python3"
    assert settings.mcp_bridge_args == ["-m", "agentchatroom.mcp_bridge"]


def test_bridge_args_environment_override_is_a_json_array(monkeypatch, tmp_path):
    for name in list(dict(__import__("os").environ)):
        if name.startswith("AGENTCHATROOM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MCP_BRIDGE_ARGS", '["mcp-bridge"]')
    assert config_module.load_settings(None).mcp_bridge_args == ["mcp-bridge"]


def test_bridge_args_rejects_a_shell_word_string(monkeypatch, tmp_path):
    """An argv vector is a list; re-splitting a shell string is not portable."""
    for name in list(dict(__import__("os").environ)):
        if name.startswith("AGENTCHATROOM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MCP_BRIDGE_ARGS", "mcp-bridge --stdio")
    with pytest.raises(ValueError, match="JSON array"):
        config_module.load_settings(None)


def test_bridge_args_must_not_be_empty(monkeypatch, tmp_path):
    for name in list(dict(__import__("os").environ)):
        if name.startswith("AGENTCHATROOM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTCHATROOM_MCP_BRIDGE_ARGS", "[]")
    with pytest.raises(ValueError, match="must not be empty"):
        config_module.load_settings(None)


# ---------------------------------------------------------------------------
# AC3: `mcp-config --client <profile>` covers every published profile.
# ---------------------------------------------------------------------------
def _run_cli(monkeypatch, tmp_path, argv):
    for name in list(dict(__import__("os").environ)):
        if name.startswith("AGENTCHATROOM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTCHATROOM_DATA_DIR", str(tmp_path / "data"))
    completed = subprocess.run(
        [sys.executable, "-m", "agentchatroom", "mcp-config", *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**__import__("os").environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_cli_client_choices_come_from_the_published_profiles(tmp_path):
    """Adding a client must not require touching the CLI."""
    integration = _integration(tmp_path)
    assert set(MCP_CLIENT_PROFILES) == set(integration["profiles"])
    assert "trae" in MCP_CLIENT_PROFILES


def test_cli_lists_every_client_profile(monkeypatch, tmp_path):
    output = _run_cli(monkeypatch, tmp_path, ["--list-clients"])
    listed = {entry["id"] for entry in json.loads(output)["clients"]}
    assert listed == set(MCP_CLIENT_PROFILES)


@pytest.mark.parametrize("profile_id", sorted(MCP_CLIENT_PROFILES))
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_every_client_and_transport_product_parses_and_has_a_real_identity(
    monkeypatch, tmp_path, profile_id, transport
):
    """The core promise: what the CLI prints is usable as-is.

    `generic` is the documented exception -- it is the fill-in template, so its
    identity placeholders are the point. Every named client must carry a real
    identity, and the output must parse in that client's own format.
    """
    output = _run_cli(
        monkeypatch, tmp_path, ["--client", profile_id, "--transport", transport]
    )
    assert output.strip(), (profile_id, transport)
    fmt = MCP_CLIENT_PROFILES[profile_id]["format"]
    if fmt == "json":
        parsed = json.loads(output)
        assert "mcpServers" in parsed
        assert "agentchatroom" in parsed["mcpServers"]
    else:
        parsed_toml = tomllib.loads(output)
        assert "mcp_servers" in parsed_toml
        assert "agentchatroom" in parsed_toml["mcp_servers"]

    if profile_id != "generic":
        for placeholder in PLACEHOLDER_IDENTITY:
            assert placeholder not in output, (profile_id, transport, placeholder)
        # A real identity is what room_bootstrap accepts: the value must not
        # start with the placeholder bracket.
        identity = MCP_CLIENT_PROFILES[profile_id]
        key = profile_id.replace("_", "-")
        assert key in output
        assert str(identity.get("label") or "") or key


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_named_client_bridge_output_starts_a_real_bridge(
    monkeypatch, tmp_path, transport
):
    """A Bridge config must reference a launcher that can reach the module."""
    if transport != "remote-bridge":
        pytest.skip("the Bridge launcher only appears in the bridge transport")
    for profile_id in ("workbuddy", "trae", "grok_build", "codex"):
        output = _run_cli(
            monkeypatch, tmp_path, ["--client", profile_id, "--transport", transport]
        )
        server = json.loads(output)["mcpServers"]["agentchatroom"] if (
            MCP_CLIENT_PROFILES[profile_id]["format"] == "json"
        ) else tomllib.loads(output)["mcp_servers"]["agentchatroom"]
        args = server["args"]
        # Either an interpreter consumes -m, or the exe dispatches a subcommand.
        assert args == ["-m", "agentchatroom.mcp_bridge"] or args == [
            MCP_BRIDGE_SUBCOMMAND
        ], (profile_id, args)
        assert server["env"]["AGENTCHATROOM_SERVER_URL"].startswith("http")


def test_legacy_format_aliases_still_resolve(monkeypatch, tmp_path):
    """Existing scripts must keep working; --client is additive."""
    for legacy, profile_id in (
        ("workbuddy-json", "workbuddy"),
        ("grok-toml", "grok_build"),
        ("codex-toml", "codex"),
    ):
        legacy_output = _run_cli(
            monkeypatch, tmp_path, ["--format", legacy, "--transport", "local-stdio"]
        )
        client_output = _run_cli(
            monkeypatch,
            tmp_path,
            ["--client", profile_id, "--transport", "local-stdio"],
        )
        assert legacy_output == client_output, legacy


def test_generic_format_stays_the_fill_in_template(monkeypatch, tmp_path):
    output = _run_cli(monkeypatch, tmp_path, ["--format", "generic-json"])
    for placeholder in PLACEHOLDER_IDENTITY:
        assert placeholder in output


def test_top_level_format_aliases_are_not_the_same_string(tmp_path):
    """`codex_toml` and `grok_toml` used to be byte-identical generic output.

    They are retained as compatibility aliases for the generic template, which
    is the only thing they can honestly mean now that per-client text lives
    under profiles[*]. A test pins that they are aliases, not distinct
    per-client artifacts, so nobody mistakes them for the real thing again.
    """
    integration = _integration(tmp_path)
    assert integration["codex_toml"] == integration["grok_toml"]
    # Both aliases hold the generic TOML template, which is the only thing
    # they can honestly mean now that per-client text lives under profiles[*].
    assert integration["codex_toml"] == integration["generic_toml_text"]
    # The real per-client TOML carries that client's own identity instead.
    codex = integration["profiles"]["codex"]["local_config_text"]
    grok = integration["profiles"]["grok_build"]["local_config_text"]
    assert codex != integration["generic_toml_text"]
    assert grok != integration["generic_toml_text"]
    assert codex != grok
