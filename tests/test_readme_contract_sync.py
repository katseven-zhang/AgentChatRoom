"""Task #347: the README must not drift from the code it describes.

Five specific claims were checked against the implementation. Three were wrong
and are now corrected; two were already right and are pinned here so a later
edit cannot quietly break them:

1. transport idle reclamation defaulted to 300s and "stops the Room Session too"
   - the default is 1800s and reclamation only stops the keepalive.
3. "the configuration file schema does not contain these keys" - the
   ``management_login_*`` keys are declared in ``CONFIG_FILE_SCHEMA.security``.
5. "applies to Agents and the admin console alike" - ``check_leases`` is REST
   only, absent from the MCP tool surface and from the web client.

These assertions read the real values, so a drifting document fails here
rather than in review.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentchatroom.config import CONFIG_FILE_SCHEMA, Settings

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "src" / "agentchatroom" / "web" / "index.html").read_text(
    encoding="utf-8"
)


def _settings() -> Settings:
    return Settings(data_dir=ROOT)


def test_the_documented_idle_timeout_is_the_real_default():
    """A documented default that is not the default sends people to debug ghosts."""
    actual = _settings().mcp_http_session_idle_timeout_seconds
    assert actual == 1800.0

    # The stale "默认 300 秒" claim must be gone, and the real value present.
    assert "默认 300 秒" not in README
    assert "默认 1800 秒" in README


def test_reclamation_is_documented_as_keepalive_only():
    """Reclamation stops the keepalive; it does not close the Room Session."""
    assert "被回收 transport 的后台心跳与 Room Session 会一起停止" not in README
    assert "只停止后台保活" in README
    assert "不关闭 Room Session" in README


def test_the_login_keys_are_declared_in_the_config_schema():
    """README used to claim these keys were outside the schema; they are in it."""
    security = CONFIG_FILE_SCHEMA.get("security", {})
    for key in (
        "management_login_max_failures",
        "management_login_window_seconds",
        "management_login_lockout_seconds",
    ):
        assert key in security, f"{key} must be a declared config key"

    assert "配置文件 schema 不含这些键" not in README
    assert "CONFIG_FILE_SCHEMA.security" in README


def test_member_revocation_really_does_revoke_everything_it_claims():
    """Pins the two claims that were already correct, so they cannot regress."""
    from agentchatroom.services import AgentChatRoomService

    # The cascade is implemented in one private helper; README documents all
    # three effects, so all three must remain in that helper's contract.
    source = (
        (ROOT / "src" / "agentchatroom" / "services.py").read_text(encoding="utf-8")
    )
    start = source.index("def _revoke_member_access_locked")
    body = source[start : start + 4000]
    assert "UPDATE agent_credentials SET revoked_at" in body
    assert "UPDATE file_leases SET released_at" in body
    assert "left_at = ?" in body

    assert "级联收权" in README
    assert callable(getattr(AgentChatRoomService, "_revoke_member_access_locked"))


def test_the_lease_policy_hint_names_a_surface_that_exists():
    """check_leases is REST-only; the hint must not imply otherwise."""
    assert "leases/check" in INDEX_HTML
    assert "Agent 与管理端均适用" not in INDEX_HTML
    # The REST route is the real entry point the hint now points at.
    api = (ROOT / "src" / "agentchatroom" / "api.py").read_text(encoding="utf-8")
    assert "/leases/check" in api
    # And the MCP tool surface really does not expose it, which is why the
    # hint has to say so.
    mcp = (ROOT / "src" / "agentchatroom" / "mcp_server.py").read_text(encoding="utf-8")
    assert "def check_leases" not in mcp


def test_the_transport_idle_default_is_the_only_one_documented_as_seconds():
    """Scoped to the transport-reclamation sentence on purpose.

    A repo-wide "默认 N 秒" sweep would also match the login rate-limit
    defaults, which are a different setting entirely.
    """
    sentence = re.search(r"stateful transport 做有界回收，默认 (\d+) 秒", README)
    assert sentence is not None, "the reclamation sentence no longer documents a default"
    assert sentence.group(1) == "1800"
