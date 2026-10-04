"""Run the credential-free local release preflight.

This check is intentionally narrower than the MCP acceptance harnesses. It
does not create Projects, issue credentials, join Rooms, or write business
events. It verifies that a running center exposes its health and Web entry
points, and optionally runs source-level gates from the repository root.

The default run sends no credential anywhere. A management token is only
attached when the operator asks for it explicitly, and only over https - a
bearer token replayed in cleartext on a LAN is worse than not checking at all.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]

# The source tree is the authority on what "correct" means. A preflight that
# cannot import it would silently compare the server against nothing.
sys.path.insert(0, str(ROOT / "src"))

from agentchatroom import __version__ as SOURCE_VERSION  # noqa: E402
from agentchatroom.config import Settings  # noqa: E402

# The config schema this source tree understands. A server built from another
# tree may answer every probe correctly and still parse the file differently.
SOURCE_SCHEMA_VERSION = Settings(data_dir=ROOT).config_schema_version


def normalize_base_url(value: str) -> str:
    base = value.strip().rstrip("/")
    if base.endswith("/mcp"):
        base = base[:-4]
    return base.rstrip("/")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the credential-free AgentChatRoom release preflight."
    )
    parser.add_argument(
        "--url",
        default=os.getenv("AGENTCHATROOM_SERVER_URL", "http://127.0.0.1:8765"),
        help="Center base URL; /mcp is accepted and removed automatically",
    )
    parser.add_argument(
        "--project-id",
        default="",
        help="Optional Project ID for a read-only snapshot check",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--no-source-checks",
        action="store_true",
        help="Skip pytest, compileall, Node syntax, and git whitespace checks",
    )
    return parser.parse_args(argv)


def request_json(url: str, *, timeout: float, headers: dict[str, str]) -> tuple[int, Any]:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured URL
        raw = response.read()
        body = json.loads(raw.decode("utf-8")) if raw else None
        return int(response.status), body


def http_check(
    name: str,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str],
    predicate: Any,
    expected_field: str = "",
    expected_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_values = expected_values or {}
    try:
        status, body = request_json(url, timeout=timeout, headers=headers)
        passed = status == 200 and bool(predicate(body))
        result: dict[str, Any] = {"name": name, "passed": passed, "status": status}
        if isinstance(body, dict):
            result["status_value"] = body.get("status")
        # A reachable server is not the claim being made. A stale EXE still
        # answers 200 on every probe, so the reported version has to be part of
        # the verdict - otherwise "verify" certifies the wrong build.
        expected = expected_values.get(name)
        if passed and expected is not None:
            observed = body.get(expected_field) if isinstance(body, dict) else None
            result["expected_value"] = expected
            result["observed_value"] = observed
            if observed != expected:
                passed = False
                result["error"] = (
                    f"{expected_field} mismatch: server reports {observed!r}, "
                    f"this source tree is {expected!r}"
                )
        result["passed"] = passed
        if not passed and "error" not in result:
            result["error"] = "unexpected response"
        return result
    except HTTPError as error:
        return {"name": name, "passed": False, "status": error.code, "error": "http error"}
    except (OSError, URLError, ValueError, json.JSONDecodeError) as error:
        return {"name": name, "passed": False, "error": type(error).__name__}


def run_command(name: str, command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed command list from this module
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return {"name": name, "passed": False, "exit_code": None, "error": type(error).__name__}
    result: dict[str, Any] = {
        "name": name,
        "passed": completed.returncode == 0,
        "exit_code": completed.returncode,
    }
    if completed.returncode != 0:
        output = (completed.stdout + "\n" + completed.stderr).strip()
        result["output_tail"] = output[-1000:]
    return result


def static_check(
    name: str,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str],
) -> dict[str, Any]:
    try:
        request = Request(url, headers=headers, method="GET")
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            content = response.read()
            return {
                "name": name,
                "passed": response.status == 200 and bool(content),
                "status": int(response.status),
            }
    except HTTPError as error:
        return {"name": name, "passed": False, "status": error.code, "error": "http error"}
    except (OSError, URLError) as error:
        return {"name": name, "passed": False, "error": type(error).__name__}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be positive")

    base_url = normalize_base_url(args.url)
    if not base_url.startswith(("http://", "https://")):
        raise SystemExit("--url must start with http:// or https://")

    headers: dict[str, str] = {}
    admin_token = os.getenv("AGENTCHATROOM_ADMIN_TOKEN", "").strip()
    if admin_token:
        # A bearer token is replayable by anything on the path. Sending one
        # over cleartext to an http:// endpoint hands it to the network, which
        # this module exists to avoid - so refuse rather than quietly proceed.
        if not base_url.startswith("https://"):
            raise SystemExit(
                "refusing to send AGENTCHATROOM_ADMIN_TOKEN over http://; "
                "use an https:// --url or unset the variable"
            )
        headers["Authorization"] = f"Bearer {admin_token}"

    # The source tree is what a release is cut from, so its versions are the
    # expectation. Reaching a server proves nothing about which build it is.
    expected_versions = {
        "health_live": SOURCE_VERSION,
        "public_config": SOURCE_SCHEMA_VERSION,
    }

    checks = [
        http_check(
            "health_live",
            f"{base_url}/health/live",
            timeout=args.timeout,
            headers=headers,
            predicate=lambda body: isinstance(body, dict) and body.get("status") == "ok",
            expected_field="version",
            expected_values=expected_versions,
        ),
        http_check(
            "health_ready",
            f"{base_url}/health/ready",
            timeout=args.timeout,
            headers=headers,
            predicate=lambda body: isinstance(body, dict) and body.get("status") == "ready",
        ),
        http_check(
            "public_config",
            f"{base_url}/api/v1/config/public",
            timeout=args.timeout,
            headers=headers,
            predicate=lambda body: isinstance(body, dict) and "domain" in body,
            expected_field="config_schema_version",
            expected_values=expected_versions,
        ),
    ]
    checks.extend(
        [
            static_check(
                "web_index",
                f"{base_url}/",
                timeout=args.timeout,
                headers=headers,
            ),
            static_check(
                "favicon",
                f"{base_url}/assets/favicon.svg",
                timeout=args.timeout,
                headers=headers,
            ),
        ]
    )

    if args.project_id:
        checks.append(
            http_check(
                "project_snapshot",
                f"{base_url}/api/v1/projects/{args.project_id}/snapshot",
                timeout=args.timeout,
                headers=headers,
                predicate=lambda body: isinstance(body, dict) and "project" in body,
            )
        )

    if not args.no_source_checks:
        checks.extend(
            [
                run_command("pytest", [sys.executable, "-m", "pytest"]),
                run_command("compileall", [sys.executable, "-m", "compileall", "-q", "src"]),
                run_command("node_syntax", ["node", "--check", "src/agentchatroom/web/app.js"]),
                run_command("git_diff_check", ["git", "diff", "--check"]),
            ]
        )

    result = {
        "base_url": base_url,
        "credential_used": bool(admin_token),
        "checks": checks,
        "passed": all(bool(check.get("passed")) for check in checks),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
