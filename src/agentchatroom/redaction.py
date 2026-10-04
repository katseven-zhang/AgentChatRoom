"""The single redaction policy for every surface that can print a credential.

Six call sites used to redact independently: the API log filter, the GUI log
tail, task-history projections, runtime-binding dumps, the request-field guard
and the PostgreSQL tool wrapper. They disagreed on which key names counted
(9 / 10 / 7 entries, no set being a superset) and on the replacement marker, so
the same secret came out as ``[redacted]`` from one surface and ``[REDACTED]``
from another and could not be asserted or filtered uniformly.

This module is the source of truth for *what counts as sensitive* and *how a
value is walked*. It deliberately keeps the two text strengths apart:

- :func:`redact_text` replaces only the credential *value*, so free-form
  product content (message bodies, notes, evidence) keeps its surrounding
  text intact.
- The log-line policy in :mod:`agentchatroom.api` additionally swallows the
  rest of a log line, because a log line is not user-visible content.

Collapsing those two into one would silently truncate a task timeline the
moment a body mentioned ``token: ...``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# One marker for every surface. The upper-case form was already dominant in
# the log path and in the test suite; a secret must redact to one string so a
# filter or assertion can rely on it.
REDACTED = "[REDACTED]"

# The union of the three key sets that previously drifted apart. A key in this
# set has its value replaced wholesale and is dropped from mapping output.
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "admin_token",
        "agent_token",
        "api_key",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "management_token",
        "password",
        "passwd",
        "secret",
        "session_token",
        "token",
    }
)

# A long opaque value under a token-ish key is a credential even when the key
# itself is not one of the exact names above (e.g. "agent_token_hint").
TOKEN_KEY_MIN_LENGTH = 24
_TOKENISH = "token"

_CREDENTIAL_VALUE = r"[^\s,;}\]&\"']+"

# AgentChatRoom's own credential shapes. Both the bare token
# (acr.<id>.<secret>) and the credential bundle the MCP connector carries
# (acrb.v1.<base64>) are live values: the bundle is what the client sends on
# every call, so a pattern that stops at "acr." lets the whole bundle through.
# The release audit reports the same shapes, so a live credential can neither
# survive a log nor stay invisible to the publish check.
AGENTCHATROOM_CREDENTIAL_PATTERN = re.compile(
    r"\bacrb?\.v?\d*\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)*\b"
    r"|\bacr\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)

# Value-pattern redaction: credentials embedded in free-form text must not leak
# even under a benign key such as "body" or "summary" (review #44, criterion 12).
_REDACT_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?" + _CREDENTIAL_VALUE),
    re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(token|api[_-]?key|secret|password|passwd|session[_-]?token"
        r"|access[_-]?token)\b(\s*[=:]\s*)(\"[^\"]+\"|'[^']+'|[^\s,;\"']+)"
    ),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    AGENTCHATROOM_CREDENTIAL_PATTERN,
)


def redact_text(text: str) -> str:
    """Replace credential-looking substrings inside free-form text.

    Only the value is replaced; the surrounding text survives so this is safe
    for message bodies, notes and evidence.
    """
    redacted = str(text)
    for pattern in _REDACT_TEXT_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def is_sensitive_key(key: str) -> bool:
    return str(key).strip().lower() in SENSITIVE_KEYS


def redact_value(value: Any, *, key: str = "") -> Any:
    """Walk a runtime structure, replacing every sensitive value it can reach.

    One walker for every call site: a mapping loses its sensitive entries
    entirely, a sequence is walked element by element, and a bare string is
    put through :func:`redact_text`.
    """
    name = str(key).strip().lower()
    if name in SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key).strip().lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        if len(value) >= TOKEN_KEY_MIN_LENGTH and _TOKENISH in name:
            return REDACTED
        return redact_text(value)
    return value
