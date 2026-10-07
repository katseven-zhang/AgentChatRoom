"""Validate repository review provenance inside the domain write transaction."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from .errors import DomainError


def repository_identity(value: str) -> tuple[str, str]:
    value = str(value or "").strip()
    match = re.fullmatch(r"[^@\s]+@([^:\s]+):([^\s]+)", value)
    if match:
        host, path = match.groups()
    else:
        url = urlparse(value)
        host, path = url.hostname or "", url.path.lstrip("/")
    path = path.rstrip("/").removesuffix(".git")
    if not host or not re.fullmatch(r"[\w.-]+/[\w.-]+", path):
        return "", ""
    return host.lower(), path.lower()


def validate_repository_review(
    project: dict[str, Any], report: dict[str, Any] | None,
    evidence: dict[str, Any], criteria: list[dict[str, Any]], limit: int,
    *, verdict: str,
) -> dict[str, Any]:
    def fail(code: str, message: str) -> None:
        raise DomainError(code, message, status_code=409, details={
            "required_action": "read_the_latest_work_report_and_review_its_exact_repository_commit",
        })

    if not report or evidence.get("work_report_id") != report["id"]:
        fail("review_report_changed", "The review must reference the latest WorkReport")
    sha = str(evidence.get("commit_hash") or "").lower()
    report_sha = str(report.get("commit_hash") or "").lower()
    returning = verdict == "changes_requested"
    # Returning deficient evidence must not require that evidence to be complete.
    # A supplied inspected commit still needs a full SHA and matching repository
    # link; only approval asserts that it is the exact reported implementation.
    if (not returning or sha) and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        fail("review_commit_required", "Repository review requires a full commit SHA")
    if not returning and sha != report_sha:
        fail("review_commit_mismatch", "The SHA does not match the latest WorkReport")
    repository_url = str(evidence.get("repository_url") or "")
    url = urlparse(repository_url)
    expected = repository_identity(str(project.get("git_remote") or ""))
    if (
        url.scheme != "https" or url.username or url.password or url.query or url.fragment
        or expected == ("", "") or repository_identity(repository_url) != expected
    ):
        fail("review_repository_mismatch", "Use the HTTPS repository registered to this Project")
    base = f"https://{url.netloc}/{url.path.strip('/').removesuffix('.git')}"
    links = evidence.get("evidence_urls", [])
    minimum_links = 0 if returning and not sha else 1
    if not isinstance(links, list) or not minimum_links <= len(links) <= limit:
        fail("review_evidence_required", "Provide bounded repository evidence URLs")
    normalized = []
    for value in links:
        link = urlparse(str(value))
        if (
            link.scheme != "https" or link.username or link.password or link.query
            or "\\" in unquote(link.path)
            or any(part in {".", ".."} for part in unquote(link.path).split("/"))
            or (link.hostname or "").lower() != expected[0]
            or not link.path.lower().startswith("/" + expected[1] + "/")
            or len(str(value)) > 2048
        ):
            fail("review_evidence_repository_mismatch", "Evidence URLs must belong to the registered repository")
        normalized.append(str(value))
    if sha and not any(urlparse(value).path.lower() == f"/{expected[1]}/commit/{sha}" for value in normalized):
        fail("review_commit_evidence_required", "Include the exact repository /commit/<SHA> URL")
    if any(not str(item.get("evidence") or "").strip() for item in criteria):
        fail("review_criterion_evidence_required", "Each assessed criterion requires concrete evidence")
    return {
        "work_report_id": report["id"], "commit_hash": sha,
        "repository_url": base, "evidence_urls": list(dict.fromkeys(normalized)),
        "report_commit_hash": report_sha, "commit_matches_report": bool(sha) and sha == report_sha,
    }
