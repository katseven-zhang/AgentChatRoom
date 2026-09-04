from __future__ import annotations

import re
from typing import Any
from typing_extensions import NotRequired, TypedDict


DOMAIN_SCHEMA_VERSION = 7
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

# ---------------------------------------------------------------------------
# Versioned task view projection (Plan D, event #2788 + #2795 corrections).
#
# L2 projection layer: a pure, deterministic function of the three canonical
# state faces (execution x verification x integration). It emits stable
# semantic codes only — no localized text, colors, icons, or themes live in
# the domain layer. Presentation metadata is served as versioned config by
# the REST adapter and consumed by REST / MCP / CLI / Web alike.
# ---------------------------------------------------------------------------
TASK_VIEW_SCHEMA_VERSION = 2

# The 11 valid phases of the Plan D phase table. P7 (completed, approved,
# pending) and P11 (completed, not_required, pending) share the
# "pending_integration" phase code and the integration navigation group.
TASK_VIEW_PHASES = (
    "todo",
    "claimed",
    "in_progress",
    "blocked",
    "awaiting_review",
    "changes_requested",
    "pending_integration",
    "integration_failed",
    "done",
    "cancelled",
)
TASK_VIEW_UNCLASSIFIED_PHASE = "unclassified"
TASK_VIEW_ALL_PHASES = (*TASK_VIEW_PHASES, TASK_VIEW_UNCLASSIFIED_PHASE)

# Navigation groups (vertical slices); attention is a query, never a state.
TASK_VIEW_GROUPS = {
    "todo": "claimable",
    "claimed": "active",
    "in_progress": "active",
    "blocked": "active",
    "changes_requested": "active",
    "awaiting_review": "review",
    "pending_integration": "integration",
    "integration_failed": "integration",
    "done": "done",
    "cancelled": "cancelled",
    "unclassified": "unclassified",
}
TASK_VIEW_GROUP_CODES = (
    "claimable",
    "active",
    "review",
    "integration",
    "done",
    "cancelled",
    "unclassified",
)

# Inbox semantics: exactly the three anomaly phases ("something is wrong").
# todo stays out because 待认领 already has its own navigation entry.
TASK_VIEW_ATTENTION_PHASES = {"changes_requested", "blocked", "integration_failed"}

# Terminal-state guard: a cancelled task must never be masked by stale
# verification/integration residue carried over from earlier phases.
TASK_VIEW_CANCELLED_VERIFICATION_STATUSES = set(TASK_VERIFICATION_STATUSES)

# "修改中" also covers released-after-review tasks returning to the pool as
# (todo, changes_requested, pending) — contract of task #43.
_TASK_VIEW_REOPEN_EXECUTION_STATUSES = {"todo", "claimed", "in_progress", "blocked"}
_TASK_VIEW_CLOSED_VERIFICATION = {"approved", "not_required"}


def task_view(
    *,
    execution_status: str,
    verification_status: str,
    integration_status: str,
) -> dict[str, Any]:
    """Project the three state faces onto stable view codes.

    Deterministic pure function: input is exactly the (E, V, I) triple, output
    carries phase / group / needs_attention / badge codes only. Unknown or
    illegal combinations fall through to the explicit "unclassified" phase so
    that new states or legacy residue are surfaced instead of being silently
    disguised as a normal phase.
    """
    execution = str(execution_status)
    verification = str(verification_status)
    integration = str(integration_status)
    if execution == "cancelled" and integration == "pending":
        phase = "cancelled"
    elif (
        execution == "completed"
        and verification in _TASK_VIEW_CLOSED_VERIFICATION
        and integration == "done"
    ):
        phase = "done"
    elif (
        execution == "completed"
        and verification in _TASK_VIEW_CLOSED_VERIFICATION
        and integration == "failed"
    ):
        phase = "integration_failed"
    elif (
        verification == "changes_requested"
        and execution in _TASK_VIEW_REOPEN_EXECUTION_STATUSES
        and integration == "pending"
    ):
        phase = "changes_requested"
    elif (
        execution == "completed"
        and verification == "pending"
        and integration == "pending"
    ):
        phase = "awaiting_review"
    elif (
        execution == "completed"
        and verification in _TASK_VIEW_CLOSED_VERIFICATION
        and integration == "pending"
    ):
        phase = "pending_integration"
    elif (
        execution in _TASK_VIEW_REOPEN_EXECUTION_STATUSES
        and verification == "not_required"
        and integration == "pending"
    ):
        phase = execution
    else:
        phase = TASK_VIEW_UNCLASSIFIED_PHASE
    auxiliary = []
    if phase == "changes_requested" and execution == "blocked":
        auxiliary.append("blocked")
    return {
        "schema_version": TASK_VIEW_SCHEMA_VERSION,
        "phase": phase,
        "group": TASK_VIEW_GROUPS[phase],
        "needs_attention": phase in TASK_VIEW_ATTENTION_PHASES,
        "primary_badge": phase,
        "auxiliary_badges": auxiliary,
        "execution_status": execution,
        "verification_status": verification,
        "integration_status": integration,
    }


def task_view_contract(
    *,
    execution_status: str,
    verification_status: str,
    integration_status: str,
) -> dict[str, Any]:
    """Alias kept for the contract name used in the Plan D spec (event #2788)."""
    return task_view(
        execution_status=execution_status,
        verification_status=verification_status,
        integration_status=integration_status,
    )

TASK_PHASES = TASK_VIEW_ALL_PHASES
TASK_PHASE_FILTERS = TASK_PHASES
TASK_PHASE_COMMANDS = {
    "todo": "create or define a task; it stays unclaimed until task_claim; task_release returns an owned task here",
    "claimed": "task_claim",
    "in_progress": "task_update status=in_progress",
    "blocked": "task_update status=blocked",
    "awaiting_review": "work_report",
    "changes_requested": "review_submit verdict=changes_requested",
    "pending_integration": "review_submit verdict=approved",
    "integration_failed": "integration_submit result=failed",
    "done": "integration_submit result=done",
    "cancelled": "task_update status=cancelled",
    "unclassified": "not reachable through state machine commands; surfaced for legacy or invalid data",
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

TASK_RELEASE_REASON_CODES = {
    "quota_exhausted",
    "agent_unavailable",
    "user_requested",
    "reassignment_needed",
    "other",
}
TASK_RELEASE_EXECUTION_STATUSES = {"claimed", "in_progress", "blocked"}
TASK_RELEASE_COMPAT_REASON_CODE = "other"

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
    """Legacy phase accessor, kept as a read-only compatible output.

    Delegates to the versioned task_view projection so every consumer sees
    the same phase code; the previous hand-rolled mapping (which masked
    pending_integration and integration_failed as verified/awaiting_review)
    is retired by this delegation.
    """
    del status  # legacy_status must not drive the projection
    return task_view(
        execution_status=execution_status,
        verification_status=verification_status,
        integration_status=integration_status,
    )["phase"]


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
