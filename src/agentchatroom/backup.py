from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .database import SCHEMA_VERSION

# A restore swaps only the main database file. Sidecars belong to the previous
# database and are replayed on the next open, so they must go with the swap.
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# Never let a real secret reach argv, stderr or an exception message: argv is
# world-readable through the process list on a shared machine.
REDACTED = "***"


class BackupError(RuntimeError):
    """Raised when a database backup or restore cannot be completed safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _manifest_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.manifest.json")


def _write_manifest(
    target: Path,
    *,
    backend: str,
    details: dict[str, Any],
    latest_event_id: int | None = None,
) -> Path:
    # A manifest without an event cursor cannot be proven current, and the
    # in-app restore then refuses it as backup_cursor_unknown (#295). Callers
    # that can read the cursor must pass it; only a caller that genuinely has
    # no access to the event log leaves it out.
    if latest_event_id is not None:
        details = {**details, "latest_event_id": int(latest_event_id)}
    manifest = {
        "backup_schema_version": 1,
        "database_schema_version": SCHEMA_VERSION,
        "backend": backend,
        "created_at": _utc_now(),
        **details,
    }
    path = _manifest_path(target)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _validate_sqlite_backup(source: Path) -> int:
    if not source.is_file():
        raise BackupError(f"SQLite backup does not exist: {source}")
    connection = None
    try:
        connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise BackupError(f"SQLite integrity check failed: {integrity}")
            row = connection.execute(
                "SELECT version FROM schema_meta LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupError(f"Cannot inspect SQLite backup: {error}") from error
    if row is None:
        raise BackupError("SQLite backup is missing schema_meta")
    version = int(row[0])
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"SQLite backup schema {version} is newer than supported schema {SCHEMA_VERSION}"
        )
    return version


def backup_sqlite(
    source: Path,
    target: Path,
    *,
    latest_event_id: int | None = None,
    source_label: str | None = None,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if source == target:
        raise BackupError("Backup target must differ from the live SQLite database")
    if not source.is_file():
        raise BackupError(f"SQLite database does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        schema_version = _validate_sqlite_backup(temporary)
        os.replace(temporary, target)
    except sqlite3.Error as error:
        temporary.unlink(missing_ok=True)
        raise BackupError(f"SQLite backup failed: {error}") from error
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    details: dict[str, Any] = {"schema_version": schema_version}
    if source_label:
        details["source"] = source_label
    manifest = _write_manifest(
        target,
        backend="sqlite",
        details=details,
        latest_event_id=latest_event_id,
    )
    return {"backend": "sqlite", "output": str(target), "manifest": str(manifest)}


def clear_sqlite_sidecars(target: Path) -> list[str]:
    """Remove the WAL/SHM/journal files sitting beside a replaced database.

    ``Connection.backup`` copies the source header, so a restored file comes
    back in WAL mode. Any leftover ``-wal`` from the previous database is then
    replayed on the next open and resurrects pre-restore rows — exactly when a
    restore is used, after an unclean shutdown. Removing them in the same step
    as the swap keeps the restore atomic.
    """
    removed: list[str] = []
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = target.with_name(f"{target.name}{suffix}")
        try:
            sidecar.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise BackupError(
                f"Cannot remove SQLite sidecar {sidecar.name}: {error}"
            ) from error
        removed.append(sidecar.name)
    return removed


def restore_sqlite(
    source: Path,
    target: Path,
    *,
    overwrite: bool = False,
    write_manifest: bool = True,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if source == target:
        raise BackupError("Restore source and target must differ")
    schema_version = _validate_sqlite_backup(source)
    if target.exists() and not overwrite:
        raise BackupError(
            f"Restore target already exists: {target}; pass overwrite confirmation explicitly"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.restore.tmp")
    temporary.unlink(missing_ok=True)
    try:
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        _validate_sqlite_backup(temporary)
        os.replace(temporary, target)
    except sqlite3.Error as error:
        temporary.unlink(missing_ok=True)
        raise BackupError(f"SQLite restore failed: {error}") from error
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    removed_sidecars = clear_sqlite_sidecars(target)
    manifest_path = ""
    if write_manifest:
        manifest_path = str(
            _write_manifest(
                target,
                backend="sqlite",
                details={
                    "restored_from": str(source),
                    "schema_version": schema_version,
                    "removed_sidecars": removed_sidecars,
                },
            )
        )
    return {
        "backend": "sqlite",
        "input": str(source),
        "output": str(target),
        "manifest": manifest_path,
        "removed_sidecars": removed_sidecars,
    }


def _redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def split_conninfo_password(database_url: str) -> tuple[str, str]:
    """Split a conninfo into a password-free URL and its password.

    The password is stripped from the value handed to argv, because argv is
    readable by every local user through the process list. It travels to the
    tool through a 0600 PGPASSFILE instead.
    """
    parsed = urlsplit(database_url)
    if not parsed.password:
        return database_url, ""
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    if parsed.username:
        hostname = f"{parsed.username}@{hostname}"
    sanitized = urlunsplit(
        (parsed.scheme, hostname, parsed.path, parsed.query, parsed.fragment)
    )
    return sanitized, parsed.password


def _write_pgpass(database_url: str, password: str, directory: Path) -> Path | None:
    if not password:
        return None
    parsed = urlsplit(database_url)
    host = parsed.hostname or "localhost"
    port = str(parsed.port or 5432)
    user = parsed.username or "postgres"
    database = parsed.path.lstrip("/") or user
    path = directory / f".pgpass.{os.getpid()}.tmp"
    path.write_text(f"{host}:{port}:{database}:{user}:{password}\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows has no POSIX mode; the file still never leaves this
        # directory and is removed below.
        pass
    return path


def _run_database_tool(
    command: list[str],
    *,
    action: str,
    env: dict[str, str] | None = None,
    secrets: tuple[str, ...] = (),
) -> None:
    child_env = None
    if env:
        child_env = {**os.environ, **env}
    try:
        completed = subprocess.run(
            command,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=child_env,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise BackupError(
            f"{action} requires the PostgreSQL client tools (pg_dump/pg_restore)"
        ) from error
    if completed.returncode != 0:
        # Decode as UTF-8 rather than the process locale: pg_* writes UTF-8,
        # and a locale decode both mangles the message and can raise
        # UnicodeDecodeError, which would replace a clean failure with a 500.
        detail = (
            completed.stderr.decode("utf-8", "replace").strip()
            or completed.stdout.decode("utf-8", "replace").strip()
            or "command failed"
        )
        # Tool output can echo the conninfo back; never let it reach the caller.
        raise BackupError(f"{action} failed: {_redact(detail[-500:], *secrets)}")


def backup_postgresql(
    database_url: str,
    target: Path,
    *,
    latest_event_id: int | None = None,
    source_label: str | None = None,
) -> dict[str, Any]:
    if not database_url.strip():
        raise BackupError("PostgreSQL database URL is required for backup")
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("pg_dump")
    if not executable:
        raise BackupError("PostgreSQL backup requires pg_dump on PATH")
    sanitized, password = split_conninfo_password(database_url)
    # Dump into a sibling temporary file so an interrupted run cannot truncate
    # or destroy the previous good backup.
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    passfile = _write_pgpass(database_url, password, target.parent)
    try:
        _run_database_tool(
            [
                executable,
                "--format=custom",
                "--file",
                str(temporary),
                "--dbname",
                sanitized,
            ],
            action="PostgreSQL backup",
            env={"PGPASSFILE": str(passfile)} if passfile else None,
            secrets=(password, database_url),
        )
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if passfile is not None:
            passfile.unlink(missing_ok=True)
    details: dict[str, Any] = {"format": "custom"}
    if source_label:
        details["source"] = source_label
    manifest = _write_manifest(
        target,
        backend="postgresql",
        details=details,
        latest_event_id=latest_event_id,
    )
    return {"backend": "postgresql", "output": str(target), "manifest": str(manifest)}


def read_manifest(target: Path) -> dict[str, Any] | None:
    """Return a backup's manifest, or None when it is absent or unreadable.

    A corrupt manifest must not take the whole backup subsystem down with it.
    """
    path = _manifest_path(target)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def restore_postgresql(database_url: str, source: Path) -> dict[str, Any]:
    if not database_url.strip():
        raise BackupError("PostgreSQL database URL is required for restore")
    source = source.expanduser().resolve()
    if not source.is_file():
        raise BackupError(f"PostgreSQL backup does not exist: {source}")
    manifest = read_manifest(source)
    if manifest is not None:
        schema_version = manifest.get("database_schema_version")
        if isinstance(schema_version, int) and schema_version > SCHEMA_VERSION:
            raise BackupError(
                f"PostgreSQL backup schema {schema_version} is newer than "
                f"supported schema {SCHEMA_VERSION}"
            )
    executable = shutil.which("pg_restore")
    if not executable:
        raise BackupError("PostgreSQL restore requires pg_restore on PATH")
    sanitized, password = split_conninfo_password(database_url)
    passfile = _write_pgpass(database_url, password, source.parent)
    try:
        _run_database_tool(
            [
                executable,
                "--clean",
                "--if-exists",
                "--exit-on-error",
                "--dbname",
                sanitized,
                str(source),
            ],
            action="PostgreSQL restore",
            env={"PGPASSFILE": str(passfile)} if passfile else None,
            secrets=(password, database_url),
        )
    finally:
        if passfile is not None:
            passfile.unlink(missing_ok=True)
    return {
        "backend": "postgresql",
        "input": str(source),
        "restored": True,
        "manifest_validated": manifest is not None,
    }
