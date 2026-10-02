from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PREFIXES = (
    ".agentchatroom/",
    ".codex/",
    ".grok/",
    ".trae/",
    ".workbuddy/",
    "docs/",
    # Acceptance dumps are local proof, not product content: they carry the
    # developer's hostname, absolute checkout paths and real Room identifiers.
    "evidence/",
)
FORBIDDEN_FILENAMES = {
    ".env",
    "post_room_update.py",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pem",
    ".p12",
    ".pfx",
    ".bak",
}
SENSITIVE_SCREENSHOT = re.compile(
    r"(?i)^docs/assets/.*(?:dashboard|screenshot|room|session).*[.](?:png|jpe?g|webp)$"
)

# Acceptance dumps and screenshots are local proof, not product content: JUnit
# XML carries the runner's hostname, and screenshots show live Room data. They
# belong in the ignored .agentchatroom/ or evidence/ area, never in the index.
RUNTIME_DUMP_SUFFIXES = {
    ".dump",
    ".har",
    ".jpeg",
    ".jpg",
    ".pcap",
    ".png",
    ".webp",
    ".xml",
}

PLACEHOLDER_MARKERS = (
    "change",
    "demo",
    "dummy",
    "example",
    "local",
    "placeholder",
    "postgres",
    "replace",
    "sample",
    "secret",
    "test",
    "token",
    "your",
    "${",
    "{{",
    "<",
)

from agentchatroom.redaction import AGENTCHATROOM_CREDENTIAL_PATTERN

SECRET_PATTERNS = (    (
        "github_token",
        re.compile(r"(?i)(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9_]{30,})"),
    ),
    (
        "provider_api_key",
        re.compile(
            r"(?i)\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|"
            r"xai-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,})\b"
        ),
    ),
    (
        "agentchatroom_credential",
        AGENTCHATROOM_CREDENTIAL_PATTERN,
    ),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "authorization_literal",
        re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{20,}"),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
    ),
)

GENERIC_SECRET_LITERAL = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|session[_-]?token|"
    r"admin[_-]?token|management[_-]?token|client[_-]?secret|password|"
    r"passwd|secret|token)\b\s*[:=]\s*[\"']([^\"']{8,})[\"']"
)
CREDENTIAL_URL = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)"
    r"://[^\s/:@]+:([^\s@/]+)@"
)

PUBLIC_SURFACE_PATTERNS = (
    ("windows_absolute_path", re.compile(r"(?i)\b[A-Z]:\\(?!<)[^\s`\"']+")),
    (
        "windows_user_home",
        re.compile(r"(?i)\b[A-Z]:\\(?:Users|文档|Desktop|AppData)\\[^\\/\s`\"']+"),
    ),
    ("unix_user_home", re.compile(r"(?i)(?:/Users/|/home/)[^/\s`\"']+")),
    (
        # Machine names never belong in a published repository, and JUnit XML,
        # .dump files and HAR archives carry them as attributes. The rule is
        # deliberately generic: naming the operator's host inside the auditor
        # would leak the very value it looks for. Only serialized forms count, so
        # a Python statement like `hostname = parsed.hostname` is not a finding.
        "developer_hostname",
        re.compile(
            r"(\bhostname\s*=\s*\"[^\"<>\s]{2,}\""
            r"|\bhostname\s*[=:]\s*'[^'<>\s]{2,}'"
            r"|\b(?:COMPUTERNAME|HOSTNAME|MACHINE_NAME)\s*[=:]\s*"
            r"[A-Za-z0-9][A-Za-z0-9._-]{1,})"
        ),
    ),
    (
        "live_domain_id",
        re.compile(
            r"\b(?:project|task|agent|report|review|handoff|integration|member|"
            r"lease)_[0-9a-f]{12,}\b"
        ),
    ),
    ("live_project_key", re.compile(r"\bagentchatroom-workbuddy-local\b")),
    (
        "dated_acceptance_key",
        re.compile(r"\bmainline-live-acceptance-[0-9]{8}\b"),
    ),
    (
        "private_lan_address",
        re.compile(
            r"\b(?:10(?:\.\d{1,3}){3}|"
            r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3})\b"
        ),
    ),
    ("room_event_reference", re.compile(r"`#[0-9]{3,}`")),
)


# Line-level, rule-specific opt-out for fixtures that must contain a
# Windows-shaped path or an id-shaped token to exercise normalization.
AUDIT_ALLOW_DIRECTIVE = re.compile(r"audit-allow:([A-Za-z0-9_,]+)")
# Same idea declared once at the top of a module, for test files whose purpose
# is to exercise path/host/id normalization with synthetic values.
AUDIT_ALLOW_FILE_DIRECTIVE = re.compile(
    r"audit-allow-file:([A-Za-z0-9_,]+)"
)

_MACHINE_VALUE_RULES: tuple[tuple[str, re.Pattern[str]], ...] | None = None


def machine_value_rules() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Values that identify the machine running the audit.

    They are derived at runtime instead of written into the repository, because
    hardcoding a hostname or a user home in the scanner would publish exactly
    what the scanner is looking for. Unlike the shape-based rules these apply
    everywhere, including test fixtures, and no directive can silence them.
    """
    global _MACHINE_VALUE_RULES
    if _MACHINE_VALUE_RULES is not None:
        return _MACHINE_VALUE_RULES
    candidates: list[tuple[str, str]] = [
        ("this_machine_hostname", socket.gethostname() or ""),
        ("this_user_home_path", str(Path.home())),
    ]
    rules: list[tuple[str, re.Pattern[str]]] = []
    for rule, value in candidates:
        cleaned = value.strip()
        if len(cleaned) < 3 or _is_placeholder(cleaned):
            continue
        rules.append((rule, re.compile(re.escape(cleaned), re.IGNORECASE)))
    _MACHINE_VALUE_RULES = tuple(rules)
    return _MACHINE_VALUE_RULES

# Containers that hide readable text between binary bytes. A NUL must not mean
# "skip this file": that is how a JUnit dump or a screenshot escaped the audit.
BINARY_SCAN_SUFFIXES = {
    ".dump",
    ".gif",
    ".har",
    ".jpeg",
    ".jpg",
    ".jsonl",
    ".pcap",
    ".png",
    ".webp",
    ".xml",
    ".zip",
}


@dataclass(frozen=True, slots=True)
class Finding:
    source: str
    path: str
    line: int | None
    rule: str

def _run_git(root: Path, *args: str, text: bool = False) -> bytes | str:
    options: dict[str, object] = {
        "cwd": root,
        "check": True,
        "capture_output": True,
    }
    if text:
        options.update(text=True, encoding="utf-8", errors="replace")
    result = subprocess.run(["git", *args], **options)
    return result.stdout


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _is_public_surface(path: str) -> bool:
    """Everything this repository publishes is a public surface.

    The previous allowlist made the scanner blind by default: evidence/,
    packaging/, tests/, root .cmd files and the package metadata were never
    content-checked, so acceptance dumps carrying the developer's hostname,
    absolute checkout paths and a real Room key sailed through the audit that
    README promises. Only the auditor itself stays exempt, because it holds the
    literal detector expressions.
    """
    normalized = path.replace("\\", "/")
    if normalized == "scripts/audit_public_release.py":
        return False
    return True


def _allowed_rules(line: str) -> set[str]:
    """Rules a line opts out of, e.g. ``# audit-allow:windows_absolute_path``.

    Fixtures that must contain a Windows-shaped path or an id-shaped token to
    exercise normalization use this directive, so an exemption is a reviewable
    per-rule decision instead of a blind spot for a whole directory.
    """
    match = AUDIT_ALLOW_DIRECTIVE.search(line)
    if match is None:
        return set()
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


def _file_allowed_rules(text: str) -> set[str]:
    head = "\n".join(text.splitlines()[:3])
    match = AUDIT_ALLOW_FILE_DIRECTIVE.search(head)
    if match is None:
        return set()
    return {item.strip() for item in match.group(1).split(",") if item.strip()}


def _path_findings(path: str, source: str) -> list[Finding]:
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    findings: list[Finding] = []
    if lowered.startswith(FORBIDDEN_PREFIXES):
        findings.append(Finding(source, normalized, None, "runtime_path_tracked"))
    if Path(lowered).name in FORBIDDEN_FILENAMES:
        findings.append(Finding(source, normalized, None, "sensitive_filename"))
    if Path(lowered).suffix in FORBIDDEN_SUFFIXES:
        findings.append(Finding(source, normalized, None, "runtime_artifact_tracked"))
    if Path(lowered).suffix in RUNTIME_DUMP_SUFFIXES:
        findings.append(Finding(source, normalized, None, "acceptance_dump_tracked"))
    if SENSITIVE_SCREENSHOT.search(normalized):
        findings.append(Finding(source, normalized, None, "runtime_screenshot_tracked"))
    return findings


def _is_test_fixture(path: str) -> bool:
    """True for repository test sources, which carry fake credentials by design."""
    return path.startswith("tests/") or "/tests/" in path


def scan_text(text: str, *, path: str, source: str) -> list[Finding]:
    findings: list[Finding] = []
    public_surface = _is_public_surface(path)
    file_allowed = _file_allowed_rules(text)
    machine_rules = machine_value_rules()
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in machine_rules:
            if pattern.search(line):
                findings.append(Finding(source, path, line_number, rule))
        high_confidence_secret = False
        for rule, pattern in SECRET_PATTERNS:
            if rule == "agentchatroom_credential" and _is_test_fixture(path):
                # Test suites carry fake acr.<id>.<secret> values on purpose; the
                # credential rule exists for shipped text (scripts, deploy files,
                # logs) and would otherwise be noise the moment a test is added.
                continue
            if pattern.search(line):
                findings.append(Finding(source, path, line_number, rule))
                high_confidence_secret = True

        if not high_confidence_secret:
            for match in GENERIC_SECRET_LITERAL.finditer(line):
                if not _is_placeholder(match.group(1)):
                    findings.append(
                        Finding(source, path, line_number, "generic_secret_literal")
                    )

        for match in CREDENTIAL_URL.finditer(line):
            if not _is_placeholder(match.group(1)):
                findings.append(Finding(source, path, line_number, "credential_url"))

        if public_surface:
            allowed = file_allowed | _allowed_rules(line)
            for rule, pattern in PUBLIC_SURFACE_PATTERNS:
                if rule in allowed:
                    continue
                match = pattern.search(line)
                if match is None:
                    continue
                # A documented placeholder value is not private data: the shape
                # rules exist for real machine paths, hosts and Room ids.
                if _is_placeholder(match.group(0)):
                    continue
                findings.append(Finding(source, path, line_number, rule))
    return findings


def _scan_bytes(data: bytes, *, path: str, source: str) -> list[Finding]:
    normalized = path.replace("\\", "/")
    if b"\x00" in data and Path(normalized).suffix.lower() not in BINARY_SCAN_SUFFIXES:
        return []
    probe = data.replace(b"\x00", b" ") if b"\x00" in data else data
    return scan_text(probe.decode("utf-8", errors="replace"), path=path, source=source)


def candidate_paths(root: Path) -> list[str]:
    raw = _run_git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    assert isinstance(raw, bytes)
    return sorted({item.decode("utf-8") for item in raw.split(b"\x00") if item})


def audit_working_tree(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in candidate_paths(root):
        target = root / path
        if not target.is_file():
            continue
        findings.extend(_path_findings(path, "working-tree"))
        findings.extend(
            _scan_bytes(target.read_bytes(), path=path, source="working-tree")
        )
    return sorted(set(findings), key=lambda item: (item.path, item.line or 0, item.rule))


def history_blobs(root: Path) -> Iterable[tuple[str, str, bytes]]:
    raw_objects = _run_git(root, "rev-list", "--objects", "--all", text=True)
    assert isinstance(raw_objects, str)
    seen: set[str] = set()
    for row in raw_objects.splitlines():
        object_id, separator, path = row.partition(" ")
        if not separator or object_id in seen:
            continue
        object_type = _run_git(root, "cat-file", "-t", object_id, text=True)
        assert isinstance(object_type, str)
        if object_type.strip() != "blob":
            continue
        seen.add(object_id)
        data = _run_git(root, "cat-file", "blob", object_id)
        assert isinstance(data, bytes)
        yield object_id, path, data


def audit_history(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for object_id, path, data in history_blobs(root):
        source = f"git:{object_id[:12]}"
        findings.extend(_path_findings(path, source))
        findings.extend(_scan_bytes(data, path=path, source=source))
    return sorted(set(findings), key=lambda item: (item.path, item.line or 0, item.rule))


def _print_findings(findings: list[Finding], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(item) for item in findings], ensure_ascii=False, indent=2))
        return
    if not findings:
        print("public-release audit passed")
        return
    for finding in findings:
        location = finding.path
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        print(f"{finding.source} {location} [{finding.rule}]")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect credentials and private runtime data before publication."
    )
    parser.add_argument("--history", action="store_true", help="scan all Git blobs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    findings = audit_history(ROOT) if args.history else audit_working_tree(ROOT)
    _print_findings(findings, as_json=args.json)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
