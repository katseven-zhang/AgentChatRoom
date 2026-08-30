from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from .errors import DomainError


PROJECT_REGISTRATION_SCHEMA_VERSION = 1
PROJECT_REGISTRATION_RELATIVE_PATH = Path(".agentchatroom") / "project.json"


def normalize_logical_path(value: str) -> str:
    return value.strip().replace("\\", "/").strip("/")


def validate_logical_path(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw.replace("\\", "/"))
    if (
        windows_path.drive
        or windows_path.is_absolute()
        or posix_path.is_absolute()
        or ".." in posix_path.parts
    ):
        raise DomainError(
            "invalid_logical_path",
            "logical_path must be a repository-relative path",
            details={"logical_path": raw},
        )
    normalized = posix_path.as_posix().strip("/")
    if normalized == ".":
        return ""
    return os.path.normcase(normalized).replace("\\", "/")


def derive_logical_path(
    root: Path,
    git_root: Path,
    requested_logical_path: str = "",
) -> str:
    resolved_root = root.resolve()
    resolved_git_root = git_root.resolve()
    try:
        relative = resolved_root.relative_to(resolved_git_root)
    except ValueError as error:
        raise DomainError(
            "project_scope_conflict",
            "Project path must be inside its detected repository root",
            details={
                "root_path": str(resolved_root),
                "repository_root": str(resolved_git_root),
            },
        ) from error
    derived = validate_logical_path(relative.as_posix())
    requested = validate_logical_path(requested_logical_path)
    if requested and requested != derived:
        raise DomainError(
            "invalid_logical_path",
            "logical_path must match the path derived from project_path",
            details={
                "logical_path": requested,
                "derived_logical_path": derived,
                "root_path": str(resolved_root),
            },
        )
    return derived


def normalize_remote(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    normalized = re.sub(r"^git@([^:]+):", r"https://\1/", normalized)
    normalized = re.sub(r"\.git$", "", normalized)
    return normalized.rstrip("/").lower()


def _git_info(root: Path) -> tuple[str, Path]:
    try:
        remote = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            stdin=subprocess.DEVNULL,
        ).stdout.strip()
        return remote, Path(top).resolve() if top else root
    except (OSError, subprocess.SubprocessError):
        return "", root


def project_registration_path(root_path: str | Path) -> Path:
    return Path(root_path).expanduser().resolve() / PROJECT_REGISTRATION_RELATIVE_PATH


def checkout_scope(
    root_path: str | Path,
    *,
    logical_path: str = "",
) -> dict[str, str]:
    root = Path(root_path).expanduser().resolve()
    if not root.is_dir():
        raise DomainError(
            "project_path_not_found",
            "Project root must be an existing directory",
            details={"root_path": str(root)},
        )
    remote, git_root = _git_info(root)
    logical = derive_logical_path(root, git_root, logical_path)
    if remote:
        return {
            "kind": "git",
            "identity": normalize_remote(remote),
            "logical_path": logical,
        }
    return {
        "kind": "path",
        "identity": os.path.normcase(str(git_root.resolve())),
        "logical_path": logical,
    }


def stored_project_scope(project: Mapping[str, Any]) -> dict[str, str]:
    logical = normalize_logical_path(str(project.get("logical_path", "") or ""))
    remote = str(project.get("git_remote", "") or "").strip()
    if remote:
        return {
            "kind": "git",
            "identity": normalize_remote(remote),
            "logical_path": logical,
        }
    root = Path(str(project.get("root_path", ""))).expanduser().resolve()
    return {
        "kind": "path",
        "identity": os.path.normcase(str(root)),
        "logical_path": logical,
    }


def _invalid_registration(path: Path, message: str) -> DomainError:
    return DomainError(
        "project_registration_invalid",
        message,
        status_code=409,
        details={"path": str(path)},
    )


def _load_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": PROJECT_REGISTRATION_SCHEMA_VERSION,
            "registrations": [],
        }
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _invalid_registration(path, "Checkout Project registration is unreadable") from error
    if not isinstance(document, dict):
        raise _invalid_registration(path, "Checkout Project registration must be an object")
    if document.get("schema_version") != PROJECT_REGISTRATION_SCHEMA_VERSION:
        raise _invalid_registration(path, "Unsupported checkout Project registration schema")
    registrations = document.get("registrations")
    if not isinstance(registrations, list):
        raise _invalid_registration(path, "Checkout Project registrations must be a list")

    seen: set[str] = set()
    for registration in registrations:
        if not isinstance(registration, dict):
            raise _invalid_registration(path, "Checkout Project registration entry must be an object")
        logical = normalize_logical_path(str(registration.get("logical_path", "") or ""))
        project_key = registration.get("project_key")
        scope = registration.get("scope")
        if logical in seen:
            raise _invalid_registration(path, "Checkout Project registration contains duplicate logical paths")
        if not isinstance(project_key, str) or not project_key.strip():
            raise _invalid_registration(path, "Checkout Project registration has no project_key")
        if not isinstance(scope, dict):
            raise _invalid_registration(path, "Checkout Project registration has no scope")
        if scope.get("kind") not in {"git", "path"}:
            raise _invalid_registration(path, "Checkout Project registration has an invalid scope kind")
        if not isinstance(scope.get("identity"), str) or not scope["identity"].strip():
            raise _invalid_registration(path, "Checkout Project registration has an invalid scope identity")
        if normalize_logical_path(str(scope.get("logical_path", "") or "")) != logical:
            raise _invalid_registration(path, "Checkout Project registration scope does not match its logical path")
        seen.add(logical)
    return document


def load_checkout_registration(
    root_path: str | Path,
    *,
    logical_path: str = "",
) -> dict[str, Any] | None:
    path = project_registration_path(root_path)
    document = _load_document(path)
    logical = normalize_logical_path(logical_path)
    matches = [
        registration
        for registration in document["registrations"]
        if normalize_logical_path(str(registration.get("logical_path", "") or ""))
        == logical
    ]
    return dict(matches[0]) if matches else None


def resolve_checkout_project_key(
    root_path: str | Path,
    *,
    logical_path: str = "",
) -> tuple[str | None, bool]:
    expected_scope = checkout_scope(root_path, logical_path=logical_path)
    expected_logical_path = expected_scope["logical_path"]
    registration = load_checkout_registration(
        root_path,
        logical_path=expected_logical_path,
    )
    if registration is None:
        return None, False

    if registration["scope"] != expected_scope:
        raise DomainError(
            "project_registration_scope_conflict",
            "Checkout Project registration belongs to another repository scope",
            status_code=409,
            details={
                "path": str(project_registration_path(root_path)),
                "registered_scope": registration["scope"],
                "requested_scope": expected_scope,
            },
        )
    registered_key = str(registration["project_key"]).strip()
    return registered_key, True


def validate_project_scope(
    root_path: str | Path,
    project: Mapping[str, Any],
    *,
    logical_path: str = "",
) -> None:
    requested_scope = checkout_scope(root_path, logical_path=logical_path)
    actual_scope = stored_project_scope(project)
    if requested_scope != actual_scope:
        raise DomainError(
            "project_scope_conflict",
            "Resolved Project belongs to another repository scope",
            status_code=409,
            details={
                "project_key": str(project.get("project_key", "")),
                "project_scope": actual_scope,
                "requested_scope": requested_scope,
            },
        )


def _write_document(path: Path, document: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as error:
        raise DomainError(
            "project_registration_write_failed",
            "Checkout Project registration could not be written",
            status_code=500,
            details={"path": str(path)},
        ) from error


def register_checkout_project(
    root_path: str | Path,
    project: Mapping[str, Any],
    *,
    replace_existing: bool = False,
) -> Path:
    logical = normalize_logical_path(str(project.get("logical_path", "") or ""))
    validate_project_scope(root_path, project, logical_path=logical)
    path = project_registration_path(root_path)
    document = _load_document(path)
    project_key = str(project.get("project_key", "")).strip()
    if not project_key:
        raise DomainError("invalid_project", "Project has no stable project_key")

    registrations = list(document["registrations"])
    existing = next(
        (
            registration
            for registration in registrations
            if normalize_logical_path(str(registration.get("logical_path", "") or ""))
            == logical
        ),
        None,
    )
    if (
        existing is not None
        and str(existing["project_key"]).strip() != project_key
        and not replace_existing
    ):
        raise DomainError(
            "project_registration_conflict",
            "Checkout is already registered to another Project key",
            status_code=409,
            details={
                "path": str(path),
                "registered_project_key": str(existing["project_key"]),
            },
        )

    entry = {
        "logical_path": logical,
        "project_key": project_key,
        "scope": checkout_scope(root_path, logical_path=logical),
    }
    if existing is None:
        registrations.append(entry)
    else:
        registrations[registrations.index(existing)] = entry
    document["registrations"] = sorted(
        registrations,
        key=lambda registration: str(registration.get("logical_path", "")),
    )
    _write_document(path, document)
    return path


def remove_checkout_project_registration(
    root_path: str | Path,
    *,
    project_key: str,
    logical_path: str = "",
) -> bool:
    path = project_registration_path(root_path)
    if not path.is_file():
        return False
    document = _load_document(path)
    logical = normalize_logical_path(logical_path)
    retained = [
        registration
        for registration in document["registrations"]
        if not (
            normalize_logical_path(str(registration.get("logical_path", "") or ""))
            == logical
            and str(registration.get("project_key", "")).strip() == project_key.strip()
        )
    ]
    if len(retained) == len(document["registrations"]):
        return False
    if retained:
        document["registrations"] = retained
        _write_document(path, document)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise DomainError(
                "project_registration_write_failed",
                "Checkout Project registration could not be removed",
                status_code=500,
                details={"path": str(path)},
            ) from error
    return True
