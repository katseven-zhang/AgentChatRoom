"""Configuration-driven AgentChatRoom server deployment helper.

This module is deliberately outside the application domain. It only composes
container and release commands, validates the deployment boundary, and never
prints values loaded from the deployment environment file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse


REQUIRED_ENV_KEYS = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "AGENTCHATROOM_DATABASE_URL",
    "AGENTCHATROOM_ADMIN_TOKEN",
    "AGENTCHATROOM_EXTERNAL_BASE_URL",
)
PLACEHOLDER_MARKERS = ("replace-with", "change-me", "room.example.com")


class DeploymentError(RuntimeError):
    """Raised for a deployment preflight or command construction failure."""


@dataclass(frozen=True, slots=True)
class DeploymentPaths:
    repo_root: Path
    env_file: Path
    compose_file: Path
    config_file: Path
    proxy_file: Path
    example_env_file: Path
    example_config_file: Path
    example_proxy_file: Path


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int


def _default_paths(repo_root: Path) -> DeploymentPaths:
    deploy_dir = repo_root / "deploy"
    return DeploymentPaths(
        repo_root=repo_root,
        env_file=deploy_dir / ".env",
        compose_file=deploy_dir / "compose.yaml",
        config_file=deploy_dir / "config.server.toml",
        proxy_file=deploy_dir / "Caddyfile",
        example_env_file=deploy_dir / ".env.example",
        example_config_file=deploy_dir / "config.server.example.toml",
        example_proxy_file=deploy_dir / "Caddyfile.example",
    )


def resolve_paths(
    *,
    repo_root: str | Path | None = None,
    env_file: str | Path | None = None,
    compose_file: str | Path | None = None,
    config_file: str | Path | None = None,
    proxy_file: str | Path | None = None,
) -> DeploymentPaths:
    root = Path(repo_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    defaults = _default_paths(root)

    def resolve(value: str | Path | None, default: Path) -> Path:
        candidate = Path(value).expanduser() if value is not None else default
        return candidate if candidate.is_absolute() else (root / candidate).resolve()

    return DeploymentPaths(
        repo_root=root,
        env_file=resolve(env_file, defaults.env_file),
        compose_file=resolve(compose_file, defaults.compose_file),
        config_file=resolve(config_file, defaults.config_file),
        proxy_file=resolve(proxy_file, defaults.proxy_file),
        example_env_file=defaults.example_env_file,
        example_config_file=defaults.example_config_file,
        example_proxy_file=defaults.example_proxy_file,
    )


def parse_env_file(path: Path) -> dict[str, str]:
    """Read the small dotenv subset used by Compose without expanding secrets."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        normalized = value.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "\"'":
            normalized = normalized[1:-1]
        values[key.strip()] = normalized
    return values


def is_placeholder(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return not normalized or any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def _safe_status(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    return "placeholder" if is_placeholder(value) else "configured"


def deployment_preflight(paths: DeploymentPaths) -> dict[str, Any]:
    """Return a redacted, side-effect-free deployment preflight result."""
    required_files = {
        "env_file": paths.env_file,
        "compose_file": paths.compose_file,
        "config_file": paths.config_file,
    }
    optional_files = {"proxy_file": paths.proxy_file}
    missing_files = [name for name, path in required_files.items() if not path.is_file()]
    env_values = parse_env_file(paths.env_file)
    env_status = {key: _safe_status(env_values, key) for key in REQUIRED_ENV_KEYS}
    missing_or_placeholder = [
        key for key, status in env_status.items() if status != "configured"
    ]
    passed = not missing_files and not missing_or_placeholder
    return {
        "valid": passed,
        "files": {
            name: {"path": str(path), "present": path.is_file()}
            for name, path in required_files.items()
        }
        | {
            name: {
                "path": str(path),
                "present": path.is_file(),
                "required": False,
            }
            for name, path in optional_files.items()
        },
        "environment": env_status,
        "missing_or_placeholder": missing_or_placeholder,
        "next": (
            "Run `serverctl.py up` after the container engine is available."
            if passed
            else "Run `serverctl.py init`, edit deploy/.env and deploy/config.server.toml, then check again."
        ),
    }


def _find_engine(requested: str | None) -> str:
    candidate = (requested or os.getenv("AGENTCHATROOM_CONTAINER_ENGINE", "")).strip()
    if candidate:
        if shutil.which(candidate) is None:
            raise DeploymentError(f"Container engine is not available: {candidate}")
        return candidate
    for name in ("docker", "podman"):
        if shutil.which(name):
            return name
    raise DeploymentError("No Docker or Podman executable was found")


def compose_prefix(paths: DeploymentPaths, engine: str) -> list[str]:
    return [
        engine,
        "compose",
        "--env-file",
        str(paths.env_file),
        "-f",
        str(paths.compose_file),
    ]


def _run(command: Sequence[str], *, cwd: Path, capture: bool = False) -> CommandResult:
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            check=False,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as error:
        raise DeploymentError(f"Executable is not available: {command[0]}") from error
    if completed.returncode:
        raise DeploymentError(
            f"Deployment command failed with exit code {completed.returncode}: {command[0]} compose"
        )
    return CommandResult(tuple(command), completed.returncode)


def _require_preflight(paths: DeploymentPaths) -> dict[str, Any]:
    result = deployment_preflight(paths)
    if not result["valid"]:
        raise DeploymentError(json.dumps(result, ensure_ascii=False, indent=2))
    _validate_application_config(paths, parse_env_file(paths.env_file))
    _validate_proxy_config(paths, parse_env_file(paths.env_file))
    return result


def _validate_application_config(
    paths: DeploymentPaths, env_values: dict[str, str]
) -> dict[str, Any]:
    """Run the product's own config validator without exposing env values."""
    environment = os.environ.copy()
    environment.update(env_values)
    command = [
        sys.executable,
        "-m",
        "agentchatroom",
        "--json",
        "config-check",
        "--config",
        str(paths.config_file),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(paths.repo_root),
            check=False,
            text=True,
            capture_output=True,
            env=environment,
        )
    except (FileNotFoundError, OSError) as error:
        raise DeploymentError("Unable to run AgentChatRoom config-check") from error
    if completed.returncode:
        raise DeploymentError(
            "AgentChatRoom config-check failed; inspect the configuration and required environment variables"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DeploymentError("AgentChatRoom config-check returned invalid JSON") from error
    if payload.get("valid") is not True:
        raise DeploymentError("AgentChatRoom config-check did not return valid=true")
    return payload


def _validate_proxy_config(
    paths: DeploymentPaths, env_values: dict[str, str]
) -> dict[str, Any]:
    """Validate the optional Caddy boundary without making it core logic."""
    if not paths.proxy_file.is_file():
        return {"configured": False, "reason": "no Caddyfile supplied"}
    content = paths.proxy_file.read_text(encoding="utf-8")
    problems: list[str] = []
    if "reverse_proxy" not in content:
        problems.append("Caddyfile must contain reverse_proxy")
    if "flush_interval -1" not in content:
        problems.append("Caddyfile must disable response buffering with flush_interval -1")
    public_url = env_values.get("AGENTCHATROOM_EXTERNAL_BASE_URL", "")
    public_host = urlparse(public_url).hostname
    if public_host and public_host not in content and "{$AGENTCHATROOM_PUBLIC_HOST}" not in content:
        problems.append("Caddyfile host must match AGENTCHATROOM_EXTERNAL_BASE_URL")
    if problems:
        raise DeploymentError("Caddyfile preflight failed: " + "; ".join(problems))
    return {
        "configured": True,
        "path": str(paths.proxy_file),
        "long_connection": True,
        "host": public_host,
    }


def init_deployment(paths: DeploymentPaths) -> dict[str, Any]:
    created: list[str] = []
    existing: list[str] = []
    for source, target in (
        (paths.example_env_file, paths.env_file),
        (paths.example_config_file, paths.config_file),
        (paths.example_proxy_file, paths.proxy_file),
    ):
        if target.exists():
            existing.append(str(target))
            continue
        if not source.is_file():
            raise DeploymentError(f"Deployment example is missing: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        created.append(str(target))
    return {"ok": True, "created": created, "existing": existing}


def run_action(
    action: str,
    paths: DeploymentPaths,
    *,
    engine: str | None = None,
    service: str = "room",
    follow: bool = False,
    tail: str = "200",
    volumes: bool = False,
    confirm: bool = False,
    output: str | None = None,
    input_path: str | None = None,
) -> dict[str, Any]:
    if action == "init":
        return init_deployment(paths)
    if action == "check":
        result = deployment_preflight(paths)
        if not result["valid"]:
            raise DeploymentError(json.dumps(result, ensure_ascii=False, indent=2))
        result["application_config"] = _validate_application_config(
            paths, parse_env_file(paths.env_file)
        )
        result["proxy_config"] = _validate_proxy_config(
            paths, parse_env_file(paths.env_file)
        )
        return result

    _require_preflight(paths)
    if action == "down" and volumes and not confirm:
        raise DeploymentError("Removing volumes requires --confirm")
    selected_engine = _find_engine(engine)
    prefix = compose_prefix(paths, selected_engine)

    if action == "up":
        _run([*prefix, "config", "--quiet"], cwd=paths.repo_root)
        _run([*prefix, "up", "-d", "--build"], cwd=paths.repo_root)
        return {"ok": True, "action": action, "engine": selected_engine}
    if action == "down":
        command = [*prefix, "down"]
        if volumes:
            command.append("--volumes")
        _run(command, cwd=paths.repo_root)
        return {"ok": True, "action": action, "engine": selected_engine, "volumes_removed": volumes}
    if action == "restart":
        _run([*prefix, "restart"], cwd=paths.repo_root)
        return {"ok": True, "action": action, "engine": selected_engine}
    if action == "status":
        _run([*prefix, "ps"], cwd=paths.repo_root)
        return {"ok": True, "action": action, "engine": selected_engine}
    if action == "logs":
        command = [*prefix, "logs"]
        if follow:
            command.append("--follow")
        command.extend(["--tail", tail, service])
        _run(command, cwd=paths.repo_root)
        return {"ok": True, "action": action, "engine": selected_engine, "service": service}
    if action in {"backup", "restore"}:
        if action == "backup" and not output:
            raise DeploymentError("backup requires --output inside the room container")
        if action == "restore" and (not input_path or not confirm):
            raise DeploymentError("restore requires --input and --confirm")
        if action == "backup":
            command = [
                *prefix,
                "exec",
                "-T",
                service,
                "python",
                "-m",
                "agentchatroom",
                "backup",
                "--config",
                "/app/config.server.toml",
                "--output",
                output or "",
            ]
            _run(command, cwd=paths.repo_root)
        else:
            # Restore must not race the foreground application. Keep the
            # database service running, use a disposable room container, and
            # always attempt to bring the application container back.
            _run([*prefix, "stop", service], cwd=paths.repo_root)
            restore_error: DeploymentError | None = None
            try:
                command = [
                    *prefix,
                    "run",
                    "--rm",
                    "--no-deps",
                    service,
                    "python",
                    "-m",
                    "agentchatroom",
                    "restore",
                    "--config",
                    "/app/config.server.toml",
                    "--input",
                    input_path or "",
                    "--confirm",
                ]
                _run(command, cwd=paths.repo_root)
            except DeploymentError as error:
                restore_error = error
            finally:
                _run([*prefix, "start", service], cwd=paths.repo_root)
            if restore_error is not None:
                raise restore_error
        return {"ok": True, "action": action, "engine": selected_engine, "service": service}
    raise DeploymentError(f"Unknown deployment action: {action}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serverctl.py")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--compose-file", default=None)
    parser.add_argument("--config-file", default=None)
    parser.add_argument("--proxy-file", default=None)
    parser.add_argument("--engine", choices=["docker", "podman"], default=None)
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("check")
    for name in ("up", "restart", "status"):
        subparsers.add_parser(name)
    down = subparsers.add_parser("down")
    down.add_argument("--volumes", action="store_true")
    down.add_argument("--confirm", action="store_true")
    logs = subparsers.add_parser("logs")
    logs.add_argument("--service", default="room")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--tail", default="200")
    backup = subparsers.add_parser("backup")
    backup.add_argument("--service", default="room")
    backup.add_argument("--output", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--service", default="room")
    restore.add_argument("--input", required=True)
    restore.add_argument("--confirm", action="store_true")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--url", default=None)
    verify.add_argument("--project-id", default=None)
    verify.add_argument("--python", dest="python_executable", default=sys.executable)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    paths = resolve_paths(
        repo_root=args.repo_root,
        env_file=args.env_file,
        compose_file=args.compose_file,
        config_file=args.config_file,
        proxy_file=args.proxy_file,
    )
    try:
        if args.action == "verify":
            url = (args.url or os.getenv("AGENTCHATROOM_EXTERNAL_BASE_URL", "")).strip()
            if not url or is_placeholder(url):
                raise DeploymentError("verify requires a real --url or AGENTCHATROOM_EXTERNAL_BASE_URL")
            command = [args.python_executable, "scripts/verify_release.py", "--url", url, "--no-source-checks"]
            if args.project_id:
                command.extend(["--project-id", args.project_id])
            _run(command, cwd=paths.repo_root)
            result: dict[str, Any] = {"ok": True, "action": "verify", "url": url}
        else:
            result = run_action(
                args.action,
                paths,
                engine=args.engine,
                service=getattr(args, "service", "room"),
                follow=getattr(args, "follow", False),
                tail=getattr(args, "tail", "200"),
                volumes=getattr(args, "volumes", False),
                confirm=getattr(args, "confirm", False),
                output=getattr(args, "output", None),
                input_path=getattr(args, "input", None),
            )
    except DeploymentError as error:
        payload = {"ok": False, "error": str(error)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"serverctl: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
