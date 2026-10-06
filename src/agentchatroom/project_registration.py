from __future__ import annotations

import json
import os
import time
import re
import subprocess
from contextlib import contextmanager
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from .errors import DomainError


PROJECT_REGISTRATION_SCHEMA_VERSION = 1
PROJECT_REGISTRATION_RELATIVE_PATH = Path(".agentchatroom") / "project.json"
PROJECT_INSTRUCTIONS_FILENAME = "AGENTS.md"
PROJECT_INSTRUCTIONS_BEGIN = "<!-- BEGIN AgentChatRoom managed coordination -->"
PROJECT_INSTRUCTIONS_END = "<!-- END AgentChatRoom managed coordination -->"


def normalize_logical_path(value: str) -> str:
    return value.strip().replace("\\", "/").strip("/")


def logical_paths_equal(left: str, right: str) -> bool:
    """Case-insensitive logical_path equality independent of host OS."""
    return normalize_logical_path(left).casefold() == normalize_logical_path(
        right
    ).casefold()


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
    # Preserve path case on every OS so Windows and Linux checkouts of the
    # same monorepo subproject store the same logical_path. Comparisons use
    # casefold via logical_paths_equal.
    return normalized.replace("\\", "/")


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
    if requested and not logical_paths_equal(requested, derived):
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


def detect_git_info(root: Path, *, timeout_seconds: float = 3) -> tuple[str, Path, bool]:
    """Return (origin_remote, git_toplevel, is_git_work_tree).

    Work-tree membership comes from ``rev-parse --show-toplevel`` succeeding —
    not from whether ``remote.origin.url`` exists — so a plain ``git init``
    is already a Git checkout for source/scope decisions.
    """

    def git_output(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Git writes path output as UTF-8 regardless of the Windows console
        # code page; decoding with the process locale instead turns non-ASCII
        # repository paths into mojibake that no longer matches root_path.
        return completed.stdout.decode("utf-8", "replace").strip()

    try:
        top = git_output("-C", str(root), "rev-parse", "--show-toplevel")
        if not top:
            return "", root, False
        remote = git_output("-C", str(root), "config", "--get", "remote.origin.url")
        return remote, Path(top).resolve(), True
    except (OSError, subprocess.SubprocessError):
        return "", root, False


_git_info = detect_git_info


def repository_directory(root_path: str | Path, repository_path: str = "") -> Path:
    """Resolve an explicitly selected repository within the workspace boundary."""
    root = Path(root_path).expanduser().resolve()
    raw = repository_path.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (PureWindowsPath(raw).drive or path.is_absolute() or ".." in path.parts
            or any(ord(char) < 32 for char in raw)):
        raise DomainError(
            "invalid_repository_path", "Repository selection must be workspace-relative",
            status_code=422,
            details={"required_action": "select_repository_inside_workspace"},
        )
    selected = (root / raw).resolve()
    try:
        selected.relative_to(root)
    except ValueError as error:
        raise DomainError(
            "untrusted_repository", "Repository selection escapes the workspace",
            status_code=403,
            details={"required_action": "select_repository_inside_workspace"},
        ) from error
    if not selected.is_dir():
        raise DomainError("project_path_not_found", "Selected repository directory does not exist")
    return selected


def project_registration_path(root_path: str | Path) -> Path:
    return Path(root_path).expanduser().resolve() / PROJECT_REGISTRATION_RELATIVE_PATH


def project_instructions_path(root_path: str | Path) -> Path:
    return Path(root_path).expanduser().resolve() / PROJECT_INSTRUCTIONS_FILENAME


def _project_instructions_error(path: Path, message: str) -> DomainError:
    return DomainError(
        "project_instructions_invalid",
        message,
        status_code=409,
        details={"path": str(path)},
    )


def _read_raw(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _read_project_instructions(path: Path) -> str:
    if not path.exists():
        return ""
    if not path.is_file():
        raise _project_instructions_error(
            path,
            "Project AGENTS.md path exists but is not a regular file",
        )
    try:
        # Path.read_text(newline=) exists only on Python 3.13+. The project
        # supports 3.11, and newline="" must keep the original line endings.
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeError) as error:
        raise _project_instructions_error(
            path,
            "Project AGENTS.md is not readable UTF-8 text",
        ) from error


def _managed_instructions_range(path: Path, text: str) -> tuple[int, int] | None:
    # Blanks around a marker are a formatting difference, not a different block.
    # Requiring the marker at column zero meant an editor that trimmed the line
    # or applied indentation stopped it being recognised, so the writer found no
    # range, appended a second block, and the file grew a copy per bootstrap.
    begin_matches = list(
        re.finditer(
            rf"(?m)^[ \t]*{re.escape(PROJECT_INSTRUCTIONS_BEGIN)}[ \t]*\r?$", text
        )
    )
    end_matches = list(
        re.finditer(
            rf"(?m)^[ \t]*{re.escape(PROJECT_INSTRUCTIONS_END)}[ \t]*\r?$", text
        )
    )
    if not begin_matches and not end_matches:
        return None
    if len(begin_matches) != 1 or len(end_matches) != 1:
        raise _project_instructions_error(
            path,
            "Project AGENTS.md has incomplete or duplicate AgentChatRoom managed markers",
        )
    start = begin_matches[0].start()
    end = end_matches[0].end()
    if end_matches[0].start() <= begin_matches[0].end():
        raise _project_instructions_error(
            path,
            "Project AGENTS.md AgentChatRoom managed markers are out of order",
        )
    return start, end


def validate_project_coordination_instructions(root_path: str | Path) -> Path:
    """Validate the managed marker boundary without changing the project file."""
    root = Path(root_path).expanduser().resolve()
    if not root.is_dir():
        raise DomainError(
            "project_path_not_found",
            "Project root must be an existing directory",
            details={"root_path": str(root)},
        )
    path = project_instructions_path(root)
    _managed_instructions_range(path, _read_project_instructions(path))
    return path


def _write_project_instructions(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8", newline="")
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DomainError(
            "project_instructions_write_failed",
            "Project AGENTS.md managed coordination rules could not be written",
            status_code=500,
            details={"path": str(path)},
        ) from error


def write_project_coordination_instructions(
    root_path: str | Path,
    project: Mapping[str, Any],
) -> dict[str, Any]:
    """Create or update only AgentChatRoom's marked block in project AGENTS.md."""
    root = Path(root_path).expanduser().resolve()
    if not root.is_dir():
        raise DomainError(
            "project_path_not_found",
            "Project root must be an existing directory",
            details={"root_path": str(root)},
        )
    project_name = str(project.get("name", "")).strip()
    if not project_name:
        raise DomainError("invalid_project", "Project has no name for AGENTS.md")

    # Imported lazily so the workspace registration module stays independent of
    # client profile construction during module initialization.
    from .integrations import build_project_coordination_instructions

    path = project_instructions_path(root)
    existing = _read_project_instructions(path)
    newline = "\r\n" if "\r\n" in existing else "\n"
    body = build_project_coordination_instructions(
        {**dict(project), "name": project_name}
    ).strip()
    managed = newline.join(
        (
            PROJECT_INSTRUCTIONS_BEGIN,
            body.replace("\r\n", "\n").replace("\n", newline),
            PROJECT_INSTRUCTIONS_END,
        )
    )
    managed_range = _managed_instructions_range(path, existing)
    if managed_range is None:
        prefix = existing.rstrip("\r\n")
        updated = f"{prefix}{newline * 2 if prefix else ''}{managed}{newline}"
        action = "created" if not existing else "appended"
    else:
        start, end = managed_range
        updated = f"{existing[:start]}{managed}{existing[end:]}"
        if not updated.endswith(("\n", "\r")):
            updated += newline
        action = "updated"
    # The END-marker range stops before the line's terminator, so re-rendering an
    # already-managed file can differ from it by that one separator alone. Compare
    # line-ending-insensitively: rewriting in that case would touch a user file
    # whose managed block has not actually changed.
    if updated.replace("\r\n", "\n") == existing.replace("\r\n", "\n"):
        action = "unchanged"
    else:
        _write_project_instructions(path, updated)
    return {
        "path": str(path),
        "action": action,
        "project_name": project_name,
    }


def remove_project_coordination_instructions(root_path: str | Path) -> bool:
    """Remove only AgentChatRoom's marked block and preserve user instructions."""
    path = project_instructions_path(root_path)
    if not path.exists():
        return False
    existing = _read_project_instructions(path)
    managed_range = _managed_instructions_range(path, existing)
    if managed_range is None:
        return False
    start, end = managed_range
    before = existing[:start].rstrip("\r\n")
    after = existing[end:].lstrip("\r\n")
    newline = "\r\n" if "\r\n" in existing else "\n"
    if before and after:
        updated = f"{before}{newline * 2}{after}"
    elif before:
        updated = f"{before}{newline}"
    elif after:
        updated = after
        if not updated.endswith(("\n", "\r")):
            updated += newline
    else:
        updated = ""
    if updated:
        _write_project_instructions(path, updated)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise DomainError(
                "project_instructions_write_failed",
                "Generated project AGENTS.md could not be removed",
                status_code=500,
                details={
                    "path": str(path),
                    "required_action": "retry_checkout_cleanup_or_remove_the_generated_instructions",
                },
            ) from error
    return True


def checkout_scope(
    root_path: str | Path,
    *,
    logical_path: str = "",
    repository_path: str | None = None,
) -> dict[str, str]:
    root = Path(root_path).expanduser().resolve()
    if not root.is_dir():
        raise DomainError(
            "project_path_not_found",
            "Project root must be an existing directory",
            details={"root_path": str(root)},
        )
    if repository_path is None:
        registration = load_checkout_registration(root, logical_path=logical_path)
        repository_path = str((registration or {}).get("repository_path") or "")
    selected = repository_directory(root, repository_path)
    remote, git_root, _is_work_tree = _git_info(selected)
    if repository_path and not _is_work_tree:
        raise DomainError("repository_not_found", "Selected repository is no longer a Git worktree",
                          status_code=409, details={"required_action": "recheck_repository_selection"})
    logical = derive_logical_path(selected, git_root, logical_path)
    if remote:
        return {
            "kind": "git",
            "identity": normalize_remote(remote),
            "logical_path": logical,
        }
    # Local work-tree without origin keeps path-scope identity (monorepo
    # uniqueness stays path+logical); display/source still reports Git via
    # project_source, and origin discovery later upgrades via #166 heal.
    return {
        "kind": "path",
        "identity": os.path.normcase(str(git_root.resolve())),
        "logical_path": logical,
    }


def repository_root_from_logical(root: Path, logical: str) -> Path:
    """Undo ``repository root / logical_path`` for a row that kept only the join.

    A path-scoped Project is identified by its repository root plus the
    repository-relative logical path, because that is what the candidate side can
    compute from a checkout. The stored ``root_path`` is the directory the operator
    selected, so recovering the root is the only way the two sides can meet -
    without it a monorepo subproject without an origin never recognised its own
    Room and every bootstrap created another Project.
    """
    parts = [
        part
        for part in Path(str(logical).replace("\\", "/")).parts
        if part not in ("", ".")
    ]
    if not parts:
        return root
    repo_root = root
    for _ in parts:
        repo_root = repo_root.parent
    return repo_root


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
    root = root / str(project.get("repository_path") or "")
    # Local monorepo identity is the repository root, not its scoped
    # subdirectory. repository_root_from_logical already walks up by the logical
    # parts; walking here as well would overshoot by that many levels again.
    return {
        "kind": "path",
        "identity": os.path.normcase(
            str(repository_root_from_logical(root, logical))
        ),
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
        if logical.casefold() in seen:
            raise _invalid_registration(path, "Checkout Project registration contains duplicate logical paths")
        if not isinstance(project_key, str) or not project_key.strip():
            raise _invalid_registration(path, "Checkout Project registration has no project_key")
        if not isinstance(scope, dict):
            raise _invalid_registration(path, "Checkout Project registration has no scope")
        if scope.get("kind") not in {"git", "path"}:
            raise _invalid_registration(path, "Checkout Project registration has an invalid scope kind")
        if not isinstance(scope.get("identity"), str) or not scope["identity"].strip():
            raise _invalid_registration(path, "Checkout Project registration has an invalid scope identity")
        if not logical_paths_equal(
            normalize_logical_path(str(scope.get("logical_path", "") or "")),
            logical,
        ):
            raise _invalid_registration(path, "Checkout Project registration scope does not match its logical path")
        seen.add(logical.casefold())
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
        if logical_paths_equal(
            normalize_logical_path(str(registration.get("logical_path", "") or "")),
            logical,
        )
    ]
    return dict(matches[0]) if matches else None


def _is_path_to_git_upgrade(
    root_path: str | Path,
    registered_scope: Mapping[str, Any],
    requested_scope: Mapping[str, Any],
) -> bool:
    if (
        isinstance(registered_scope, Mapping)
        and isinstance(requested_scope, Mapping)
        and registered_scope.get("kind") == "path"
        and requested_scope.get("kind") == "git"
    ):
        try:
            registered_identity = os.path.normcase(
                str(Path(str(registered_scope.get("identity", ""))).expanduser().resolve())
            )
            # A path-scope identity is the repository root, not the selected
            # subdirectory, so the upgrade check has to walk the same number of
            # logical segments back from the checkout.
            current_identity = os.path.normcase(
                str(
                    repository_root_from_logical(
                        Path(root_path).expanduser().resolve(),
                        str(requested_scope.get("logical_path", "") or ""),
                    )
                )
            )
            return registered_identity == current_identity
        except (OSError, ValueError):
            return False
    return False


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
    if registration is None and expected_logical_path:
        registration = load_checkout_registration(root_path, logical_path="")
    if registration is None:
        return None, False

    if registration["scope"] != expected_scope:
        selected = repository_directory(root_path, str(registration.get("repository_path") or ""))
        if not _is_path_to_git_upgrade(selected, registration["scope"], expected_scope):
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
    requested_scope = checkout_scope(
        root_path, logical_path=logical_path,
        repository_path=str(project.get("repository_path") or ""),
    )
    actual_scope = stored_project_scope(project)
    if requested_scope != actual_scope:
        if not _is_path_to_git_upgrade(root_path, actual_scope, requested_scope):
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


# Read-modify-write on the registration file is not atomic, and bootstrap runs
# from more than one Agent. A lock file in the same directory serialises the
# writers; O_CREAT|O_EXCL is the portable form and needs no dependency.
_REGISTRATION_LOCK_STALE_SECONDS = 30
_REGISTRATION_LOCK_WAIT_SECONDS = 5


def _lock_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _acquire_registration_lock(path: Path) -> Any:
    """Take the per-registration lock, or wait briefly for whoever holds it."""
    lock = _lock_path_for(path)
    deadline = time.monotonic() + _REGISTRATION_LOCK_WAIT_SECONDS
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode("ascii"))
            os.close(handle)
            return lock
        except FileExistsError:
            # A writer that crashed leaves the lock behind. Break it once it is
            # clearly older than any legitimate write, or the file stays locked
            # for good after one kill.
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0
            if age > _REGISTRATION_LOCK_STALE_SECONDS:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise DomainError(
                    "project_registration_locked",
                    "Another Agent is writing this checkout registration; "
                    "retry once it finishes",
                    status_code=409,
                    details={
                        "required_action": "retry_the_bootstrap_after_the_other_write"
                    },
                )
            time.sleep(0.05)


def _release_registration_lock(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def _write_document(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _acquire_registration_lock(path)
    temporary: Path | None = None
    try:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
    except OSError as error:
        raise DomainError(
            "project_registration_write_failed",
            "Checkout Project registration could not be written",
            status_code=500,
            details={"path": str(path)},
        ) from error
    finally:
        # A temporary file left behind is litter inside the user's checkout, and
        # it is the evidence that something went wrong.
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        _release_registration_lock(lock)


def register_checkout_project(
    root_path: str | Path,
    project: Mapping[str, Any],
    *,
    replace_existing: bool = False,
) -> Path:
    logical = normalize_logical_path(str(project.get("logical_path", "") or ""))
    validate_project_scope(root_path, project, logical_path=logical)
    validate_project_coordination_instructions(root_path)
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
            if logical_paths_equal(
                normalize_logical_path(str(registration.get("logical_path", "") or "")),
                logical,
            )
        ),
        None,
    )
    if existing is None and registrations:
        if len(registrations) == 1 and str(registrations[0].get("project_key", "")).strip() == project_key:
            existing = registrations[0]
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
        "scope": checkout_scope(root_path, logical_path=logical,
                                repository_path=str(project.get("repository_path") or "")),
    }
    if project.get("repository_path"):
        entry["repository_path"] = str(project["repository_path"])
    if existing is None:
        registrations.append(entry)
    else:
        # Merge over the stored entry rather than replacing it. A newer build
        # writing a subset of the keys used to delete everything the other one
        # had recorded, and the file is shared by every checkout in the repo.
        position = registrations.index(existing)
        merged = dict(existing)
        merged.update(entry)
        if "repository_path" in project and not project["repository_path"]:
            # An explicit return to the workspace must not retain the old
            # child selection through the forward-compatible merge above.
            merged.pop("repository_path", None)
        registrations[position] = merged
    document["registrations"] = sorted(
        registrations,
        key=lambda registration: str(registration.get("logical_path", "")),
    )
    _write_document(path, document)
    write_project_coordination_instructions(root_path, project)
    return path


@contextmanager
def checkout_registration_change(root: Path, project: Mapping[str, Any]):
    """Restore the two derived checkout files if registration or DB association fails."""
    paths = (project_registration_path(root), project_instructions_path(root))
    before = [(path, path.read_bytes() if path.is_file() else None) for path in paths]
    try:
        register_checkout_project(root, project)
        yield
    except Exception:
        for path, content in before:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(content)
                temporary.replace(path)
        raise


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
            logical_paths_equal(
                normalize_logical_path(
                    str(registration.get("logical_path", "") or "")
                ),
                logical,
            )
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
                details={
                    "path": str(path),
                    "required_action": "retry_checkout_cleanup_or_remove_the_registration_file",
                },
            ) from error
    return True
