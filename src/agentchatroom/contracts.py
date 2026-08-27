from __future__ import annotations

from typing import Any
from typing_extensions import NotRequired, TypedDict


DOMAIN_SCHEMA_VERSION = 5
PROJECT_MEMBER_SCHEMA_VERSION = 1
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

ASSIGNMENT_STATUSES = {"pending", "accepted", "declined", "blocked", "cancelled"}
ASSIGNMENT_RESPONSES = {"accepted", "declined", "blocked"}
HANDOFF_STATUSES = {"pending", "accepted", "declined", "blocked", "cancelled"}
HANDOFF_RESPONSES = {"accepted", "declined", "blocked"}
INTEGRATION_RESULTS = {"done", "failed"}


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
    return {
        "schema_version": DOMAIN_SCHEMA_VERSION,
        "execution_status": execution_status,
        "verification_status": verification_status,
        "integration_status": integration_status,
        "legacy_status": legacy_status,
        "completed": execution_status == "completed",
        "verified": execution_status == "completed"
        and verification_status in {"not_required", "approved"},
        "integrated": integration_status == "done",
    }
