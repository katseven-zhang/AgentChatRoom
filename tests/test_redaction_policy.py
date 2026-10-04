"""One redaction policy across every surface (#348).

Six call sites used to redact independently: the API log filter, the GUI log
tail, task-history projections, runtime-binding dumps, the request-field guard
and the PostgreSQL tool wrapper. They disagreed on which key names counted
(9 / 10 / 7 entries, no set a superset) and on the replacement marker, so the
same secret came out as "[redacted]" from one surface and "[REDACTED]" from
another and could not be asserted or filtered uniformly.
"""

from __future__ import annotations

import pytest

from agentchatroom import api, bootstrap, redaction, services, task_history


def test_every_surface_uses_the_same_key_set():
    """No surface may keep a private subset: the guard must be a superset."""
    for name in ("bearer", "cookie", "api_key", "session_token", "access_token"):
        assert name in redaction.SENSITIVE_KEYS, name
    assert services.SENSITIVE_REQUEST_FIELDS == redaction.SENSITIVE_KEYS


def test_bootstrap_and_task_history_agree_byte_for_byte():
    """bootstrap used to be a near-copy that could silently drift again."""
    value = {
        "token": "secret-value",
        "authorization": "Bearer abcdefghijklmnop",
        "body": "please rotate token: hunter2",
        "nested": {"api_key": "k", "summary": "acr.deadbeef.cafefeed"},
    }
    assert (
        bootstrap.redact_runtime_value(value)
        == task_history.redact_runtime_value(value)
    )


def test_the_marker_is_one_string():
    assert redaction.REDACTED == "[REDACTED]"
    rendered = task_history.redact_text("token: hunter2")
    assert redaction.REDACTED in rendered
    assert "[redacted]" not in rendered
    assert "[REDACTED]" in api.redact_log_line("authorization: Bearer abcdefghijkl")


def test_a_sensitive_key_drops_its_value_from_mapping_output():
    out = redaction.redact_value({"token": "abc", "keep": "visible"})
    assert "token" not in out
    assert out["keep"] == "visible"


def test_every_pattern_removes_its_secret():
    # Built at runtime on purpose: a literal provider key in the source is
    # exactly the shape the public-release audit exists to keep out of the
    # repository.
    provider_key = "sk-" + "abcdefghijklmnopqrstuvwx"
    github_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz012345"
    samples = [
        "authorization: Bearer abcdefghijklmnop",
        f"api_key={provider_key}",
        "password: hunter2",
        "see acr.deadbeef.cafefeed for details",
        github_token,
    ]
    for text in samples:
        out = redaction.redact_text(text)
        assert redaction.REDACTED in out, out
        for secret in ("abcdefghijkl", "hunter2", "cafefeed", provider_key, github_token):
            assert secret not in out, out


def test_value_scoped_text_keeps_the_surrounding_content():
    """A product-visible body must survive, unlike a log line.

    This is the reason the two text strengths stay apart: collapsing the log
    policy into the content policy would truncate a task timeline.
    """
    body = "Deploy plan: rotate token: hunter2 before Friday"
    out = redaction.redact_text(body)
    assert out.startswith("Deploy plan: ")
    assert "hunter2" not in out
    assert "Friday" in out
