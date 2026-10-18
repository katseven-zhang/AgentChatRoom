"""Validate repository review provenance inside the domain write transaction.

A review is about **the repository the latest WorkReport actually proved**, not
about the Project's primary repository (the local default). The remote reviewer
may only see this report's verified HTTPS repository and exact commit, so the
two paths cannot disagree anymore.
"""
from __future__ import annotations

import json
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
    if not host or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)+", path):
        return "", ""
    return host.lower(), path.lower()


def repository_https_url(value: str) -> str:
    """Canonical HTTPS repository URL for a Git remote in any usual spelling."""
    host, path = repository_identity(value)
    return f"https://{host}/{path}" if host and path else ""


def _report_system_evidence(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    evidence = report.get("system_evidence")
    if isinstance(evidence, dict):
        return evidence
    raw = evidence if isinstance(evidence, str) else report.get("system_evidence_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def report_repository(
    report: dict[str, Any] | None, project: dict[str, Any] | None
) -> tuple[str, str, str]:
    """Return ``(https_url, source, warning)`` for the latest report.

    ``report_verified`` is the server-verified remote stored with the report.
    ``project_fallback`` keeps a legacy report (or one whose evidence predates
    repository provenance) reviewable against the Project primary instead of
    stranding it. ``report_invalid`` means the report carries a remote we cannot
    turn into a safe HTTPS identity; approval stays refused, while a return may
    still name a repository explicitly.
    """
    evidence = _report_system_evidence(report)
    remote = str(evidence.get("repository_remote") or "").strip()
    if remote:
        url = repository_https_url(remote)
        if url:
            return url, "report_verified", ""
        return "", "report_invalid", "report_repository_invalid"
    fallback = repository_https_url(str((project or {}).get("git_remote") or ""))
    if fallback:
        return fallback, "project_fallback", (
            "report_repository_missing_using_project_primary"
        )
    return "", "none", "repository_not_recorded"


def review_repository_hint(
    result: dict[str, Any] | None, project: dict[str, Any] | None
) -> dict[str, Any]:
    """Attach the safe review-repository projection to a remote task read.

    Only the report id, commit, HTTPS URL and provenance/warning are exposed;
    worktree, repository_root, repository_path, system_evidence and every other
    local path stay on the server.
    """
    data = dict(result or {})
    report = data.get("latest_work_report") or None
    url, source, warning = report_repository(report, project)
    data["review_repository"] = {
        "work_report_id": report.get("id") if report else None,
        "commit_hash": str(report.get("commit_hash") or "") if report else "",
        "repository_url": url,
        "source": source,
        "warning": warning,
    }
    return data


def validate_repository_review(
    project: dict[str, Any], report: dict[str, Any] | None,
    evidence: dict[str, Any], criteria: list[dict[str, Any]], limit: int,
    *, verdict: str,
) -> dict[str, Any]:
    report_repo, repo_source, repo_warning = report_repository(report, project)
    report_repo_id = repository_identity(report_repo)
    report_sha = str(report.get("commit_hash") or "").lower() if report else ""
    sha = str(evidence.get("commit_hash") or "").lower()
    provided_url = str(evidence.get("repository_url") or "").strip()
    provided_id = repository_identity(provided_url)
    if provided_url and (
        urlparse(provided_url).scheme != "https"
        or urlparse(provided_url).username
        or urlparse(provided_url).password
        or urlparse(provided_url).query
        or urlparse(provided_url).fragment
        or provided_id == ("", "")
    ):
        provided_id = ("", "")

    def fail(
        code: str, message: str, *, action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {
            "required_action": action,
            "work_report_id": str((report or {}).get("id") or ""),
            "report_repository": report_repo,
            "provided_repository": provided_url,
            "reported_commit_hash": report_sha,
        }
        merged.update(details or {})
        raise DomainError(code, message, status_code=409, details=merged)

    if not report or evidence.get("work_report_id") != report["id"]:
        fail(
            "review_report_changed",
            "The review must reference the latest WorkReport",
            action="read_the_latest_work_report_and_review_its_exact_repository_commit",
        )
    returning = verdict == "changes_requested"
    # `repository_identity` returns ("", "") for unusable input; that tuple is
    # truthy, so every "do we know a repository?" decision asks the components.
    def has_identity(identity: tuple[str, str]) -> bool:
        return bool(identity[0] and identity[1])
    # Returning deficient evidence must not require that evidence to be complete.
    # A supplied inspected commit still needs a full SHA and matching repository
    # link; only approval asserts that it is the exact reported implementation.
    if (not returning or sha) and not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha
    ):
        fail(
            "review_commit_required",
            "Repository review requires a full commit SHA",
            action="provide_the_reports_exact_commit",
        )
    if not returning and sha != report_sha:
        fail(
            "review_commit_mismatch",
            "The SHA does not match the latest WorkReport",
            action="use_the_reports_exact_commit",
        )

    if returning:
        if has_identity(report_repo_id):
            # The report's verified repository is authoritative; an omitted URL
            # still returns the report, an explicitly different one is a
            # misattribution and stays refused.
            if provided_url and provided_id != report_repo_id:
                fail(
                    "review_repository_mismatch",
                    "Use the repository verified on the latest WorkReport",
                    action="use_the_reports_verified_repository",
                )
            effective_url, effective_id = report_repo, report_repo_id
        elif provided_url:
            if not has_identity(provided_id):
                fail(
                    "review_repository_mismatch",
                    "Use an HTTPS repository URL",
                    action="use_the_reports_verified_repository",
                )
            # The report has no usable repository evidence. A return may still
            # name the repository the reviewer inspected, but it is recorded as
            # caller-asserted and can never approve work.
            effective_url, effective_id = (
                repository_https_url(provided_url), provided_id,
            )
            repo_source = "reviewer_asserted"
            repo_warning = "report_repository_missing_reviewer_asserted"
        else:
            effective_url, effective_id = "", ("", "")
            repo_source = "none"
            repo_warning = "report_repository_missing"
    else:
        if not has_identity(report_repo_id):
            fail(
                "review_repository_unknown",
                "The latest WorkReport has no verified repository; resubmit it with repository evidence",
                action="resubmit_report_with_repository_evidence",
            )
        if provided_id != report_repo_id:
            fail(
                "review_repository_mismatch",
                "Use the repository verified on the latest WorkReport",
                action="use_the_reports_verified_repository",
                details={"report_repository": report_repo},
            )
        effective_url, effective_id = report_repo, report_repo_id

    base = effective_url.rstrip("/") if effective_url else ""
    if sha and not has_identity(effective_id):
        fail(
            "review_repository_unknown",
            "A commit review needs the report's verified repository or an explicitly named one",
            action="provide_the_reports_verified_repository_or_omit_the_commit",
        )
    links = evidence.get("evidence_urls", [])
    minimum_links = 0 if returning and not sha else 1
    if not isinstance(links, list) or not minimum_links <= len(links) <= limit:
        fail(
            "review_evidence_required",
            "Provide bounded repository evidence URLs",
            action="include_the_reports_repository_evidence_urls",
        )
    normalized = []
    for value in links:
        link = urlparse(str(value))
        if (
            link.scheme != "https" or link.username or link.password or link.query
            or "\\" in unquote(link.path)
            or any(part in {".", ".."} for part in unquote(link.path).split("/"))
            or not has_identity(effective_id)
            or (link.hostname or "").lower() != effective_id[0]
            or not link.path.lower().startswith("/" + effective_id[1] + "/")
            or len(str(value)) > 2048
        ):
            fail(
                "review_evidence_repository_mismatch",
                "Evidence URLs must belong to the WorkReport's verified repository",
                action="use_the_reports_verified_repository",
            )
        normalized.append(str(value))
    if sha and not any(
        urlparse(value).path.lower() == f"/{effective_id[1]}/commit/{sha}"
        for value in normalized
    ):
        fail(
            "review_commit_evidence_required",
            "Include the exact repository /commit/<SHA> URL",
            action="include_the_reports_exact_commit_url",
        )
    if any(not str(item.get("evidence") or "").strip() for item in criteria):
        fail(
            "review_criterion_evidence_required",
            "Each assessed criterion requires concrete evidence",
            action="add_criterion_level_evidence",
        )
    return {
        "work_report_id": report["id"], "commit_hash": sha,
        "repository_url": base, "evidence_urls": list(dict.fromkeys(normalized)),
        "report_commit_hash": report_sha,
        "commit_matches_report": bool(sha) and sha == report_sha,
        "repository_evidence_source": repo_source,
        "repository_warning": repo_warning,
    }
