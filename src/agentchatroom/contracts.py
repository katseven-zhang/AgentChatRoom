from __future__ import annotations

import re
from typing import Any
from typing_extensions import NotRequired, TypedDict


DOMAIN_SCHEMA_VERSION = 6
PROJECT_MEMBER_SCHEMA_VERSION = 1
KNOWLEDGE_SCHEMA_VERSION = 1
MODEL_DISPLAY_NAME_MAX_LENGTH = 160

PROJECT_MEMBER_STATUSES = {"invited", "active", "suspended", "revoked"}

LEGACY_TASK_STATUSES = {
    "todo",
    "claimed",
    "in_progress",
    "blocked",
    "awaiting_review",
    "verified",
    "done",
    "cancelled",
}

LEGACY_TASK_TRANSITIONS = {
    "todo": {"todo", "cancelled"},
    "claimed": {"claimed", "in_progress", "blocked", "todo", "cancelled"},
    "in_progress": {"in_progress", "blocked", "todo", "cancelled"},
    "blocked": {"blocked", "in_progress", "todo", "cancelled"},
    "awaiting_review": {"awaiting_review", "cancelled"},
    "verified": {"verified", "done", "cancelled"},
    "done": {"done"},
    "cancelled": {"cancelled", "todo"},
}

TASK_EXECUTION_STATUSES = {
    "todo",
    "claimed",
    "in_progress",
    "blocked",
    "completed",
    "cancelled",
}
TASK_VERIFICATION_STATUSES = {
    "not_required",
    "pending",
    "changes_requested",
    "approved",
}
TASK_INTEGRATION_STATUSES = {"pending", "done", "failed"}
TASK_PHASES = (
    "todo",
    "claimed",
    "in_progress",
    "blocked",
    "awaiting_review",
    "verified",
    "done",
    "cancelled",
)
TASK_PHASE_FILTERS = TASK_PHASES
TASK_PHASE_LABELS = {
    "todo": "待认领",
    "claimed": "已认领",
    "in_progress": "执行中",
    "blocked": "阻塞",
    "awaiting_review": "已提交",
    "verified": "待验收",
    "done": "已完成",
    "cancelled": "已取消",
}
TASK_PHASE_COMMANDS = {
    "todo": "create or define a task; it stays unclaimed until task_claim",
    "claimed": "task_claim",
    "in_progress": "task_update status=in_progress",
    "blocked": "task_update status=blocked",
    "awaiting_review": "work_report",
    "verified": "review_submit verdict=approved",
    "done": "integration_submit result=done",
    "cancelled": "task_update status=cancelled",
}
TASK_INTAKE_STATUSES = {
    "pending",
    "accepted",
    "defined",
    "declined",
    "blocked",
    "cancelled",
}
TASK_INTAKE_TRANSITIONS = {
    "pending": {"pending", "accepted", "declined", "blocked", "cancelled"},
    "accepted": {"accepted", "defined", "blocked", "cancelled"},
    "defined": {"defined"},
    "declined": {"declined", "pending", "cancelled"},
    "blocked": {"blocked", "pending", "cancelled"},
    "cancelled": {"cancelled"},
}

ASSIGNMENT_STATUSES = {"pending", "accepted", "declined", "blocked", "cancelled"}
ASSIGNMENT_RESPONSES = {"accepted", "declined", "blocked"}
HANDOFF_STATUSES = {"pending", "accepted", "declined", "blocked", "cancelled"}
HANDOFF_RESPONSES = {"accepted", "declined", "blocked"}
INTEGRATION_RESULTS = {"done", "failed"}

KNOWLEDGE_ASSET_STATUSES = {
    "candidate",
    "approved",
    "rejected",
    "superseded",
    "archived",
}
KNOWLEDGE_ASSET_TRANSITIONS = {
    "candidate": {"approved", "rejected", "archived"},
    "approved": {"superseded", "archived"},
    "rejected": {"candidate", "archived"},
    "superseded": {"candidate", "archived"},
    "archived": set(),
}
KNOWLEDGE_DEFAULT_KINDS = (
    "decision",
    "procedure",
    "pitfall",
    "verification",
    "preference",
    "reference",
)
KNOWLEDGE_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
KNOWLEDGE_OWNER_KINDS = {"agent_key", "member"}
KNOWLEDGE_SOURCE_TYPES = {"manual", "task_result", "import", "extractor"}
KNOWLEDGE_REVIEW_VERDICTS = {"approved", "changes_requested"}


class TestEvidence(TypedDict, total=False):
    """Structured command evidence shared by work reports and integrations."""

    command: str
    exit_code: int
    notes: NotRequired[str]


class ReviewCriterion(TypedDict, total=False):
    """One acceptance criterion assessed by an independent reviewer."""

    criterion: str
    status: str
    evidence: NotRequired[str]


class KnowledgeProvenance(TypedDict, total=False):
    """Where a Knowledge Asset version came from, kept append-only."""

    source_type: str
    source_task_id: str
    source_report_id: str
    source_review_id: str
    source_integration_id: str
    source_event_ids: list[int]
    created_by_session_id: str
    created_by_agent_key: str


class KnowledgeVersionSummary(TypedDict, total=False):
    """The current projection of one Knowledge Asset version."""

    version_id: str
    version: int
    title: str
    summary: str
    tags: list[str]
    content_hash: str
    supersedes_version_id: str


def knowledge_contract(
    *,
    kinds: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Project the versioned Knowledge contract for adapters and clients."""
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "asset_statuses": sorted(KNOWLEDGE_ASSET_STATUSES),
        "asset_transitions": {
            status: sorted(next_states)
            for status, next_states in KNOWLEDGE_ASSET_TRANSITIONS.items()
        },
        "kinds": list(kinds),
        "owner_kinds": sorted(KNOWLEDGE_OWNER_KINDS),
        "source_types": sorted(KNOWLEDGE_SOURCE_TYPES),
        "review_verdicts": sorted(KNOWLEDGE_REVIEW_VERDICTS),
    }


def task_phase(
    *,
    execution_status: str,
    verification_status: str,
    integration_status: str,
    status: str = "",
) -> str:
    """Derive the shared user-facing phase from the three task state faces."""
    if execution_status == "cancelled" or status == "cancelled":
        return "cancelled"
    if (
        execution_status == "completed"
        and verification_status == "approved"
        and integration_status == "done"
    ):
        return "done"
    if verification_status == "approved" and integration_status != "done":
        return "verified"
    if execution_status == "completed":
        return "awaiting_review"
    if execution_status == "blocked":
        return "blocked"
    if execution_status == "claimed":
        return "claimed"
    if execution_status == "in_progress":
        return "in_progress"
    return "todo"


def task_state_for_legacy_status(
    status: str,
    *,
    previous_verification: str = "not_required",
) -> tuple[str, str, str]:
    if status == "awaiting_review":
        return "completed", "pending", "pending"
    if status == "verified":
        return "completed", "approved", "pending"
    if status == "done":
        return "completed", "approved", "done"
    if status == "cancelled":
        return "cancelled", "not_required", "pending"
    verification = (
        previous_verification
        if previous_verification in {"changes_requested", "approved"}
        else "not_required"
    )
    return status, verification, "pending"


def task_contract(
    *,
    execution_status: str,
    verification_status: str,
    integration_status: str,
    legacy_status: str,
) -> dict[str, Any]:
    phase = task_phase(
        execution_status=execution_status,
        verification_status=verification_status,
        integration_status=integration_status,
        status=legacy_status,
    )
    return {
        "schema_version": DOMAIN_SCHEMA_VERSION,
        "execution_status": execution_status,
        "verification_status": verification_status,
        "integration_status": integration_status,
        "legacy_status": legacy_status,
        "phase": phase,
        "completed": execution_status == "completed",
        "verified": execution_status == "completed"
        and verification_status in {"not_required", "approved"},
        "integrated": integration_status == "done",
    }
