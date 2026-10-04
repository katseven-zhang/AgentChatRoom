"""#358: the onboarding prompt must not send an Agent down a dead end.

Three ways the handoff text could mislead the Agent reading it:

* Placeholder detection was ``"<" in config_text and ">" in config_text``. In an
  HTTP config the only angle-bracket string is the credential bundle, so the one
  instruction given was "replace the software identity placeholders with a
  display name" -- applied to the bearer slot it yields ``Bearer WorkBuddy``.
  ``bootstrap`` only rejects an identity value that *starts* with ``<``, so
  nothing catches it client-side and the Agent loops on an unreadable 401.
* The migrate prompt hard-coded a bare ``room_bootstrap`` while the mode already
  had a ``bootstrap_call`` computed for it, and printed ``%APPDATA%/...`` as a
  "config location" an Agent then had to resolve on a machine it cannot see.
* ``app.js`` carried its own second copy of the handoff prompt. The
  ``service_unavailable`` constraint existed in the server copy only, which is
  why grepping ``app.js`` for it found nothing.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from agentchatroom.config import Settings
from agentchatroom.credential_bundle import decode_project_credential_bundle
from agentchatroom.integrations import (
    AGENT_TOKEN_PLACEHOLDER,
    SERVICE_UNAVAILABLE_LINE,
    SOFTWARE_IDENTITY_PLACEHOLDERS,
    build_issued_credential_prompt,
    build_mcp_integration,
    build_onboarding_prompt,
    issued_http_config_text,
    resolve_config_path_hint,
)

REPO = Path(__file__).resolve().parents[1]
WEB_DIR = REPO / "src" / "agentchatroom" / "web"
CREDENTIALS = [
    {"name": "Project A", "token": "acr.credential-a.secret-a"},
    {"name": "Project B", "token": "acr.credential-b.secret-b"},
]
IDENTITY = {
    "software_key": "chosen",
    "software_name": "Chosen Agent",
    "software_client": "chosen-client",
}
PROJECT = {"name": "Demo Project", "root_path": "/srv/checkout"}


def _integration(tmp_path, project=None):
    return build_mcp_integration(Settings(data_dir=tmp_path / "data"), project=project)


# ---------------------------------------------------------------------------
# AC1: placeholder instructions are typed by what the slot actually holds.
# ---------------------------------------------------------------------------
def test_bearer_slot_is_never_described_as_a_display_name(tmp_path):
    """The exact failure: `Bearer WorkBuddy` and an unreadable 401 loop."""
    integration = _integration(tmp_path, PROJECT)
    for profile_id, profile in integration["profiles"].items():
        for transport in ("http", "local", "remote"):
            config_text = profile[
                {
                    "http": "streamable_http_config_text",
                    "local": "local_config_text",
                    "remote": "remote_bridge_config_text",
                }[transport]
            ]
            if AGENT_TOKEN_PLACEHOLDER not in config_text:
                continue
            prompt = build_onboarding_prompt(
                profile_id=profile_id,
                profile=profile,
                transport=transport,
                config_text=config_text,
                project=PROJECT,
            )
            assert "凭据占位符" in prompt, (profile_id, transport)
            assert "acrb.v1" in prompt, (profile_id, transport)
            # The credential instruction is its own sentence, and it names the
            # bundle rather than a display name.
            credential_clause = prompt.split("凭据占位符", 1)[1][:200]
            assert "凭据包" in credential_clause, (profile_id, transport)
            assert "acrb.v1" in credential_clause, (profile_id, transport)
            # Where "显示名称" appears here it must be the prohibition.
            assert "不能替换为显示名称" in credential_clause, (
                profile_id,
                transport,
            )
            # The identity sentence must not be the one carrying the bearer slot.
            identity_clause = prompt.split("凭据占位符", 1)[0]
            assert AGENT_TOKEN_PLACEHOLDER not in identity_clause, (
                profile_id,
                transport,
            )


def test_identity_placeholder_instruction_names_the_three_fields(tmp_path):
    integration = _integration(tmp_path, PROJECT)
    prompt = build_onboarding_prompt(
        profile_id="generic",
        profile=integration["profiles"]["generic"],
        transport="local",
        config_text=integration["profiles"]["generic"]["local_config_text"],
        project=PROJECT,
    )
    for placeholder in SOFTWARE_IDENTITY_PLACEHOLDERS:
        assert placeholder in prompt, placeholder
    assert "实际接入端显示名称" in prompt


def test_a_named_client_prompt_does_not_invent_identity_placeholders(tmp_path):
    """A named profile has a real identity; claiming otherwise wastes a step."""
    integration = _integration(tmp_path, PROJECT)
    prompt = build_onboarding_prompt(
        profile_id="workbuddy",
        profile=integration["profiles"]["workbuddy"],
        transport="local",
        config_text=integration["profiles"]["workbuddy"]["local_config_text"],
        project=PROJECT,
    )
    for placeholder in SOFTWARE_IDENTITY_PLACEHOLDERS:
        assert placeholder not in prompt, placeholder
    assert "实际接入端显示名称" not in prompt


def test_bootstrap_identity_guard_cannot_catch_a_name_in_the_bearer_slot(monkeypatch):
    """Why the wrong instruction is unrecoverable rather than merely noisy.

    bootstrap rejects an identity value that starts with `<`. A display name
    substituted into the Authorization slot produces a well-formed request that
    the server answers 401, with nothing client-side to distinguish it from a
    real credential problem.
    """
    from agentchatroom import bootstrap
    from agentchatroom.bootstrap import configured_software_identity

    def _guard(software_key: str, software_name: str, software_client: str):
        for name, value in (
            (bootstrap.SOFTWARE_KEY_ENV, software_key),
            (bootstrap.SOFTWARE_NAME_ENV, software_name),
            (bootstrap.SOFTWARE_CLIENT_ENV, software_client),
        ):
            monkeypatch.setenv(name, value)
        return configured_software_identity()

    # A display name passes: the guard only rejects a value that starts with "<".
    assert _guard("WorkBuddy", "WorkBuddy", "workbuddy") is not None
    assert _guard(
        "<stable-software-key>",
        "<Software name>",
        "<software-client-code>",
    ) is None


# ---------------------------------------------------------------------------
# AC2: the computed bootstrap variant and a resolvable config path.
# ---------------------------------------------------------------------------
def test_migrate_prompt_uses_the_computed_bootstrap_call(tmp_path):
    integration = _integration(tmp_path, PROJECT)
    for profile_id, profile in integration["profiles"].items():
        prompt = build_onboarding_prompt(
            profile_id=profile_id,
            profile=profile,
            transport="http",
            config_text=profile["streamable_http_config_text"],
            project=PROJECT,
            mode="migrate_http",
        )
        # The bare form belongs to stdio, not to an HTTP migration, and would
        # let the Agent bind whatever Project it happens to select.
        assert f'room_bootstrap(project_name="{PROJECT["name"]}")' in prompt, (
            profile_id
        )
        assert "3. 调用 `room_bootstrap`，" not in prompt, profile_id


def test_migrate_prompt_prints_a_resolved_config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    integration = _integration(tmp_path, PROJECT)
    trae = integration["profiles"]["trae"]
    assert "%APPDATA%" in str(trae["config_path_hint"])
    resolved = resolve_config_path_hint(trae)
    assert "%APPDATA%" not in resolved
    assert resolved.startswith(str(tmp_path))

    prompt = build_onboarding_prompt(
        profile_id="trae",
        profile=trae,
        transport="http",
        config_text=trae["streamable_http_config_text"],
        project=PROJECT,
        mode="migrate_http",
    )
    assert f"配置位置：{resolved}" in prompt
    assert "%APPDATA%" not in prompt


def test_every_profile_publishes_a_resolved_path_or_its_own_hint(tmp_path):
    integration = _integration(tmp_path, PROJECT)
    for profile_id, profile in integration["profiles"].items():
        assert profile["resolved_config_path"].strip(), profile_id
        if isinstance(profile.get("local_config"), dict):
            # A managed client resolves to a real file, never to a pattern.
            assert "%APPDATA%" not in profile["resolved_config_path"], profile_id


# ---------------------------------------------------------------------------
# AC3: one generator. The browser injects the real bundle and nothing else.
# ---------------------------------------------------------------------------
def test_the_browser_keeps_no_second_copy_of_the_prompt():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for removed in (
        "function issuedHttpPrompt(",
        "function incrementalHttpPrompt(",
        "function issuedHttpConfig(",
    ):
        assert removed not in javascript, removed
    # It asks the server for the prompt and shows the result verbatim.
    assert "integrations/mcp/issued-prompt" in javascript
    assert "async function loadIssuedServerPrompt(" in javascript
    assert "elements[\"token-config-value\"].textContent = result.prompt;" in javascript


def test_the_browser_prompt_uses_the_resolved_config_path():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # No more handing an Agent a %APPDATA% pattern to resolve itself.
    assert "profile.resolved_config_path || profile.config_path_hint" in javascript


@pytest.mark.parametrize("profile_id", ["workbuddy", "trae", "codex", "grok_build"])
def test_every_issued_prompt_carries_the_service_unavailable_constraint(
    tmp_path, profile_id
):
    integration = _integration(tmp_path, PROJECT)
    for mode in ("first_setup", "add_project"):
        result = build_issued_credential_prompt(
            profile_id,
            integration["profiles"][profile_id],
            mode=mode,
            project_name=PROJECT["name"],
            root_path=PROJECT["root_path"],
            project_credentials=CREDENTIALS,
            software_identity=IDENTITY,
            linked_member_name="Chosen Agent",
        )
        assert SERVICE_UNAVAILABLE_LINE in result["prompt"], (profile_id, mode)
        assert "service_unavailable" in result["prompt"], (profile_id, mode)
        assert "不要把 Token 发布到 Room、日志或仓库" in result["prompt"]


@pytest.mark.parametrize("profile_id", ["workbuddy", "trae", "codex", "grok_build"])
def test_every_issued_config_carries_a_decodable_real_bundle(tmp_path, profile_id):
    """The bytes the Agent is told to write are the bytes we accept."""
    integration = _integration(tmp_path, PROJECT)
    profile = integration["profiles"][profile_id]
    config_text = issued_http_config_text(
        profile,
        project_credentials=CREDENTIALS,
        software_identity=IDENTITY,
    )
    assert AGENT_TOKEN_PLACEHOLDER not in config_text, profile_id
    if profile["format"] == "json":
        parsed = json.loads(config_text)
        headers = parsed["mcpServers"]["agentchatroom"]["headers"]
    else:
        parsed_toml = tomllib.loads(config_text)
        headers = parsed_toml["mcp_servers"]["agentchatroom"].get(
            "http_headers"
        ) or parsed_toml["mcp_servers"]["agentchatroom"]["headers"]
        assert "bearer_token_env_var" not in config_text, profile_id
    authorization = headers["Authorization"]
    assert authorization.startswith("Bearer acrb.v1.")
    assert decode_project_credential_bundle(authorization[len("Bearer "):]) == (
        CREDENTIALS
    )
    assert headers["X-AgentChatRoom-Software-Key"] == IDENTITY["software_key"]
    assert headers["X-AgentChatRoom-Software-Name"] == IDENTITY["software_name"]


def test_incremental_prompt_merges_one_credential_and_keeps_the_rest(tmp_path):
    """An incremental merge must not carry another Project's token."""
    integration = _integration(tmp_path, PROJECT)
    result = build_issued_credential_prompt(
        "workbuddy",
        integration["profiles"]["workbuddy"],
        mode="add_project",
        project_name="Project B",
        project_credentials=[CREDENTIALS[1]],
        software_identity=IDENTITY,
    )
    prompt = result["prompt"]
    assert "不新建第二个 agentchatroom 连接器" in prompt
    assert "credential-b" in prompt
    assert "credential-a" not in prompt
    assert "本次 Token 已关联成员" not in prompt


def test_incremental_prompt_states_the_identity_requirement(tmp_path):
    integration = _integration(tmp_path, PROJECT)
    profile = integration["profiles"]["workbuddy"]
    linked = build_issued_credential_prompt(
        "workbuddy",
        profile,
        mode="add_project",
        project_name="Project B",
        project_credentials=[CREDENTIALS[1]],
        software_identity=IDENTITY,
        linked_member_name="Chosen Agent",
    )
    assert "本次 Token 已关联成员" in linked["prompt"]
    assert "三字段必须与" in linked["prompt"]
    unlinked = build_issued_credential_prompt(
        "workbuddy",
        profile,
        mode="add_project",
        project_name="Project B",
        project_credentials=[CREDENTIALS[1]],
        software_identity=IDENTITY,
    )
    assert "原样保留客户端现有的软件身份三字段" in unlinked["prompt"]


def test_issued_prompt_rejects_an_unknown_mode(tmp_path):
    integration = _integration(tmp_path, PROJECT)
    with pytest.raises(ValueError, match="issued-credential prompts cover"):
        build_issued_credential_prompt(
            "workbuddy",
            integration["profiles"]["workbuddy"],
            mode="reconnect",
            project_name="Project A",
            project_credentials=[CREDENTIALS[0]],
            software_identity=IDENTITY,
        )


def test_issued_prompt_requires_at_least_one_credential(tmp_path):
    """An empty bundle would produce a config that can never authenticate."""
    integration = _integration(tmp_path, PROJECT)
    with pytest.raises(Exception, match="credential"):
        build_issued_credential_prompt(
            "workbuddy",
            integration["profiles"]["workbuddy"],
            mode="first_setup",
            project_name="Project A",
            project_credentials=[],
            software_identity=IDENTITY,
        )


def test_rest_endpoint_returns_the_generated_prompt(settings, tmp_path):
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(checkout), "name": "Demo"},
        ).json()
        response = client.post(
            f"/api/v1/projects/{project['id']}/integrations/mcp/issued-prompt",
            json={
                "profile_id": "workbuddy",
                "mode": "first_setup",
                "project_credentials": CREDENTIALS,
                "software_identity": IDENTITY,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert SERVICE_UNAVAILABLE_LINE in body["prompt"]
    assert AGENT_TOKEN_PLACEHOLDER not in body["config_text"]


def test_rest_endpoint_refuses_an_unknown_profile(settings, tmp_path):
    from fastapi.testclient import TestClient

    from agentchatroom.api import create_app

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with TestClient(create_app(settings)) as client:
        project = client.post(
            "/api/v1/projects",
            json={"root_path": str(checkout), "name": "Demo"},
        ).json()
        response = client.post(
            f"/api/v1/projects/{project['id']}/integrations/mcp/issued-prompt",
            json={
                "profile_id": "no-such-client",
                "project_credentials": CREDENTIALS,
                "software_identity": IDENTITY,
            },
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "mcp_profile_unknown"


def test_no_prompt_anywhere_tells_an_agent_to_name_the_bearer_slot():
    """Sweep the whole shipped surface, not just the generator.

    A copy of this text in a template, a doc, or the browser would reintroduce
    the same dead end, so the phrase is searched for as a shape rather than at
    a known location.
    """
    offenders = []
    for path in list(WEB_DIR.glob("*.js")) + list(WEB_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"占位符", text):
            window = text[max(0, match.start() - 120) : match.start() + 160]
            if "凭据" in window and "显示名称" in window:
                offenders.append(f"{path.name}: {window}")
    assert not offenders, offenders
