from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from typing import Any


PROJECT_CREDENTIAL_BUNDLE_PREFIX = "acrb.v1."
MAX_PROJECT_CREDENTIALS = 32
MAX_BUNDLE_LENGTH = 16_384


class CredentialBundleError(ValueError):
    """Raised when an HTTP project-credential bundle is malformed."""


def is_project_credential_bundle(value: str) -> bool:
    return str(value or "").startswith(PROJECT_CREDENTIAL_BUNDLE_PREFIX)


def _normalized_entry(entry: Mapping[str, Any]) -> dict[str, str]:
    name = str(entry.get("name") or "").strip()
    token = str(entry.get("token") or "").strip()
    if not name or len(name) > 200:
        raise CredentialBundleError("Project credential name is required and must be at most 200 characters")
    if not token.startswith("acr.") or is_project_credential_bundle(token):
        raise CredentialBundleError("Project credential token is invalid")
    return {"name": name, "token": token}


def encode_project_credential_bundle(entries: Iterable[Mapping[str, Any]]) -> str:
    normalized: list[dict[str, str]] = []
    seen_names: set[str] = set()
    seen_tokens: set[str] = set()
    for raw in entries:
        entry = _normalized_entry(raw)
        folded = entry["name"].casefold()
        if folded in seen_names:
            raise CredentialBundleError("Project credential names must be unique")
        if entry["token"] in seen_tokens:
            raise CredentialBundleError("Project credential tokens must be unique")
        seen_names.add(folded)
        seen_tokens.add(entry["token"])
        normalized.append(entry)
    if not normalized:
        raise CredentialBundleError("At least one Project credential is required")
    if len(normalized) > MAX_PROJECT_CREDENTIALS:
        raise CredentialBundleError(
            f"At most {MAX_PROJECT_CREDENTIALS} Project credentials are supported"
        )
    payload = json.dumps(
        {"projects": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    result = f"{PROJECT_CREDENTIAL_BUNDLE_PREFIX}{encoded}"
    if len(result) > MAX_BUNDLE_LENGTH:
        raise CredentialBundleError("Project credential bundle is too large")
    return result


def decode_project_credential_bundle(value: str) -> list[dict[str, str]]:
    bundle = str(value or "").strip()
    if not is_project_credential_bundle(bundle) or len(bundle) > MAX_BUNDLE_LENGTH:
        raise CredentialBundleError("Project credential bundle is invalid")
    encoded = bundle[len(PROJECT_CREDENTIAL_BUNDLE_PREFIX) :]
    if not encoded:
        raise CredentialBundleError("Project credential bundle is empty")
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        parsed = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialBundleError("Project credential bundle is invalid") from error
    if not isinstance(parsed, dict) or set(parsed) != {"projects"}:
        raise CredentialBundleError("Project credential bundle has an unsupported shape")
    projects = parsed.get("projects")
    if not isinstance(projects, list):
        raise CredentialBundleError("Project credential bundle projects must be a list")
    # Re-encode to apply the same count, duplicate, name, and token validation.
    normalized = [_normalized_entry(item) for item in projects if isinstance(item, dict)]
    if len(normalized) != len(projects):
        raise CredentialBundleError("Project credential bundle contains an invalid entry")
    canonical = encode_project_credential_bundle(normalized)
    if canonical != bundle:
        raise CredentialBundleError("Project credential bundle is not canonical")
    return normalized
