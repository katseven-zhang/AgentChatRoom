from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import SCHEMA_VERSION


class BackupError(RuntimeError):
    """Raised when a database backup or restore cannot be completed safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _manifest_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.manifest.json")


def _write_manifest(target: Path, *, backend: str, details: dict[str, Any]) -> Path:
    manifest = {
        "backup_schema_version": 1,
        "database_schema_version": SCHEMA_VERSION,
        "backend": backend,
        "created_at": _utc_now(),
        **details,
    }
    path = _manifest_path(target)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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


def backup_sqlite(source: Path, target: Path) -> dict[str, Any]:
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
    manifest = _write_manifest(
        target,
        backend="sqlite",
        details={"schema_version": schema_version},
    )
    return {"backend": "sqlite", "output": str(target), "manifest": str(manifest)}


def restore_sqlite(source: Path, target: Path, *, overwrite: bool = False) -> dict[str, Any]:
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
    manifest = _write_manifest(
        target,
        backend="sqlite",
        details={"restored_from": str(source), "schema_version": schema_version},
    )
    return {"backend": "sqlite", "input": str(source), "output": str(target), "manifest": str(manifest)}


def _run_database_tool(command: list[str], *, action: str) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        raise BackupError(
            f"{action} requires the PostgreSQL client tools (pg_dump/pg_restore)"
        ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise BackupError(f"{action} failed: {detail[-500:]}")


def backup_postgresql(database_url: str, target: Path) -> dict[str, Any]:
    if not database_url.strip():
        raise BackupError("PostgreSQL database URL is required for backup")
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("pg_dump")
    if not executable:
        raise BackupError("PostgreSQL backup requires pg_dump on PATH")
    _run_database_tool(
        [executable, "--format=custom", "--file", str(target), "--dbname", database_url],
        action="PostgreSQL backup",
    )
    manifest = _write_manifest(target, backend="postgresql", details={"format": "custom"})
    return {"backend": "postgresql", "output": str(target), "manifest": str(manifest)}


def restore_postgresql(database_url: str, source: Path) -> dict[str, Any]:
    if not database_url.strip():
        raise BackupError("PostgreSQL database URL is required for restore")
    source = source.expanduser().resolve()
    if not source.is_file():
        raise BackupError(f"PostgreSQL backup does not exist: {source}")
    executable = shutil.which("pg_restore")
    if not executable:
        raise BackupError("PostgreSQL restore requires pg_restore on PATH")
    _run_database_tool(
        [
            executable,
            "--clean",
            "--if-exists",
            "--exit-on-error",
            "--dbname",
            database_url,
            str(source),
        ],
        action="PostgreSQL restore",
    )
    return {"backend": "postgresql", "input": str(source), "restored": True}
