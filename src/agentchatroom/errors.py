from __future__ import annotations

from typing import Any

# What a caller should do next, by failure code.
#
# The card counted 197 DomainError codes with roughly twenty carrying a
# required_action, so the majority answered "no" with nothing else. A machine
# caller cannot act on that: it has the code, and a fixed remedy per code is
# what turns the answer into a next step. Keys are the codes worth promising a
# remedy for; a code that is absent gets no field rather than a wrong one.
REQUIRED_ACTIONS: dict[str, str] = {
    # Ownership
    "not_task_owner": "release_or_reclaim_the_task_before_changing_it",
    "task_owner_session_connected": "task_claim_takeover_by_owner_identity",
    "task_already_claimed": "release_your_claim_or_wait_for_the_owner",
    "task_reclaim_forbidden": "ask_the_owner_to_release_the_task",
    "task_already_integrated": "integration_is_done_no_further_action",
    # State machine
    "invalid_transition": "reload_the_task_to_see_its_current_phase",
    "task_not_assignable": "pick_a_task_that_is_still_open",
    "task_not_ready_for_integration": "submit_and_pass_review_before_integrating",
    # Evidence
    "insufficient_work_evidence": "add_test_evidence_or_a_no_code_change_reason",
    "acceptance_criteria_not_satisfied": "mark_every_criterion_passed_to_approve",
    "invalid_review_criteria": "each_criterion_needs_a_verdict_and_evidence",
    "integration_tests_failed": "fix_the_failing_tests_then_resubmit",
    "insufficient_integration_evidence": "add_integration_test_evidence",
    "no_code_change_reason": "say_why_nothing_changed_or_add_file_evidence",
    # Dependencies
    "task_dependencies_incomplete": "finish_the_listed_dependencies_first",
    "task_dependency_cycle": "drop_one_of_the_dependencies_in_the_cycle",
    "invalid_dependency_status": "pick_a_dependency_that_can_still_complete",
    # Repository ownership
    "repository_owned_by_another_room": "use_that_repository_room",
    # Leases
    "lease_conflict": "wait_for_the_lease_ttl_or_ask_the_holder_to_release",
    "lease_not_owner": "release_only_leases_your_session_holds",
    # Authentication
    "invalid_session_token": "rejoin_the_room_for_a_fresh_session",
    "session_closed": "join_again_to_open_a_new_session",
    "session_token_expired": "join_again_to_open_a_new_session",
    "agent_token_permission_forbidden": "issue_a_token_with_the_required_permission",
    "agent_token_project_forbidden": "use_a_token_issued_for_this_project",
    "management_auth_required": "authenticate_management_or_use_owner_credentials",
}


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        # An explicit required_action always wins: a call site that knows more
        # about the situation than the table does must not be overruled.
        self.details = dict(details or {})
        if "required_action" not in self.details and code in REQUIRED_ACTIONS:
            self.details["required_action"] = REQUIRED_ACTIONS[code]

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }
