from __future__ import annotations

import re
from typing import Any, Mapping

_TOKEN_KEYS = {
    "token",
    "agent_token",
    "admin_token",
    "session_token",
    "access_token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "api_key",
}

# Value-pattern redaction: credentials embedded inside free-form text
# (message bodies, notes, evidence) must not leak even when the *key* is
# benign such as "body" or "summary" (review #44, criterion 12).
_REDACT_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;\"']+",),
    re.compile(r"(?i)\b(bearer)\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(token|api[_-]?key|secret|password|passwd|session[_-]?token|access[_-]?token)\b(\s*[=:]\s*)(\"[^\"]+\"|'[^']+'|[^\s,;\"']+)"),
    re.compile(r"(?i)\b(sk-[a-z0-9]{16,}|ghp_[a-z0-9]{20,}|gho_[a-z0-9]{20,}|xox[baprs]-[a-z0-9-]{10,})"),
)
_REDACTED = "[redacted]"


def redact_text(text: str) -> str:
    """Redact credential-looking substrings inside free-form text."""
    redacted = str(text)
    for pattern in _REDACT_TEXT_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def redact_runtime_value(value: Any, *, key: str = "") -> Any:
    if key.lower() in _TOKEN_KEYS:
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_runtime_value(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key).lower() not in _TOKEN_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [redact_runtime_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


TASK_HISTORY_SCHEMA_VERSION = 1
TASK_HISTORY_LIMIT_MAX = 200
TASK_HISTORY_DETAIL_KINDS = (
    "message",
    "review",
    "work_report",
    "integration",
    "task_state",
    "assignment",
    "handoff",
    "lease",
    "acknowledgement",
    "unknown",
)

_STATE_PAIRS = (
    ("status", "from_status"),
    ("execution_status", "from_execution_status"),
    ("verification_status", "from_verification_status"),
    ("integration_status", "from_integration_status"),
)


def history_detail_kind(event_type: str) -> str:
    if event_type.startswith("message.") and event_type != "message.acknowledged":
        return "message"
    if event_type.startswith("review."):
        return "review"
    if event_type in {"work.reported", "task.completed"}:
        return "work_report"
    if event_type.startswith("task.integration_"):
        return "integration"
    if event_type in {"task.assigned", "task.assignment_acknowledged"}:
        return "assignment"
    if "handoff" in event_type:
        return "handoff"
    if event_type.startswith("lease."):
        return "lease"
    if event_type == "message.acknowledged":
        return "acknowledgement"
    if event_type.startswith("task."):
        return "task_state"
    return "unknown"


def actor_snapshot(
    session: Mapping[str, Any] | None,
    member: Mapping[str, Any] | None,
) -> dict[str, Any]:
    session = session or {}
    member = member or {}
    metadata = member.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return redact_runtime_value(
        {
            "session_id": str(session.get("id") or ""),
            "member_id": str(member.get("id") or session.get("member_id") or ""),
            "name": str(member.get("name") or session.get("name") or "unknown"),
            "client": str(
                metadata.get("client") or session.get("client") or "unknown"
            ),
            "role": str(session.get("role") or "unknown"),
            "software_key": str(metadata.get("software_key") or ""),
            "member_status": str(member.get("status") or ""),
        }
    )


def state_changes(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for after_key, before_key in _STATE_PAIRS:
        before = payload.get(before_key)
        after = payload.get(after_key)
        if before is None or after is None or before == after:
            continue
        changes.append({"field": after_key, "before": before, "after": after})
    return changes


def related_entity_ids(payload: Mapping[str, Any]) -> dict[str, str]:
    keys = (
        "report_id",
        "review_id",
        "integration_id",
        "assignment_id",
        "handoff_id",
        "lease_id",
        "intake_id",
        "acknowledged_event_id",
    )
    related: dict[str, str] = {}
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            related[key] = value
    if payload.get("event_id") and "acknowledged_event_id" not in related:
        related["event_id"] = str(payload.get("event_id"))
    return related


def message_model_display_name(payload: Mapping[str, Any]) -> str:
    value = str(payload.get("model_display_name") or "").strip()
    return value or "unknown"


def _section(kind: str, title: str, items: list[Any] | None) -> dict[str, Any] | None:
    if not items:
        return None
    return {"kind": kind, "title": title, "items": items}


def evidence_sections(
    *,
    detail_kind: str,
    payload: Mapping[str, Any],
    related: Mapping[str, Any],
    acknowledgements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if detail_kind == "message":
        body = str(payload.get("body") or "")
        if body:
            sections.append(
                {
                    "kind": "message",
                    "title": "正文",
                    "items": [body],
                    "preview": body.splitlines()[:8],
                    "expandable": body.count("\n") >= 8 or len(body) > 400,
                }
            )
        meta = []
        if payload.get("mentions"):
            meta.append({"label": "mentions", "value": payload.get("mentions")})
        if payload.get("files"):
            meta.append({"label": "files", "value": payload.get("files")})
        if payload.get("requires_ack"):
            meta.append(
                {
                    "label": "requires_ack",
                    "value": True,
                    "acknowledgements": acknowledgements,
                }
            )
        section = _section("meta", "消息属性", meta)
        if section:
            sections.append(section)
    if detail_kind == "review":
        review = related.get("review") or payload
        criteria = review.get("criteria") or payload.get("criteria") or []
        if criteria:
            sections.append(
                {"kind": "criteria", "title": "验收记录", "items": criteria}
            )
        notes = str(review.get("notes") or payload.get("notes") or "").strip()
        if notes:
            sections.append({"kind": "notes", "title": "审查说明", "items": [notes]})
    if detail_kind == "work_report":
        report = related.get("report") or payload
        files = report.get("files") or payload.get("files") or []
        tests = report.get("tests") or payload.get("tests") or []
        git = report.get("system_evidence") or payload.get("system_evidence") or {}
        if files:
            sections.append({"kind": "files", "title": "变更文件", "items": files})
        reason = str(
            report.get("no_code_change_reason")
            or payload.get("no_code_change_reason")
            or ""
        ).strip()
        if reason:
            sections.append(
                {"kind": "notes", "title": "无代码变更", "items": [reason]}
            )
        if tests:
            sections.append({"kind": "tests", "title": "测试证据", "items": tests})
        if git:
            sections.append({"kind": "git", "title": "Git 证据", "items": [git]})
    if detail_kind == "integration":
        integration = related.get("integration") or payload
        tests = integration.get("tests") or payload.get("tests") or []
        files = integration.get("files") or payload.get("files") or []
        if files:
            sections.append({"kind": "files", "title": "集成文件", "items": files})
        if tests:
            sections.append({"kind": "tests", "title": "集成测试", "items": tests})
        commit_hash = str(
            integration.get("commit_hash") or payload.get("commit_hash") or ""
        ).strip()
        sections.append(
            {
                "kind": "git",
                "title": "Git 证据",
                "items": [
                    {
                        "branch": "",
                        "head": commit_hash,
                        "captured": bool(commit_hash),
                        "reason": "" if commit_hash else "未采集",
                    }
                ],
            }
        )
    changes = state_changes(payload)
    if changes:
        sections.append(
            {"kind": "state", "title": "状态迁移", "items": changes}
        )
    return sections


def history_summary(event_type: str, payload: Mapping[str, Any]) -> str:
    if event_type == "review.submitted":
        return f"review {payload.get('verdict') or 'submitted'}"
    if event_type == "work.reported":
        return str(payload.get("summary") or "work reported")
    if event_type == "task.released":
        reason_code = str(payload.get("reason_code") or "other")
        reason = str(payload.get("reason") or "").strip()
        summary = f"released ({reason_code})"
        if reason:
            summary = f"{summary}: {reason}"
        return summary
    if event_type.startswith("message."):
        body = str(payload.get("body") or "").strip().splitlines()
        return body[0][:180] if body else event_type
    if event_type.startswith("task.integration_"):
        return str(payload.get("summary") or payload.get("result") or event_type)
    if payload.get("note"):
        return str(payload.get("note"))
    if payload.get("summary"):
        return str(payload.get("summary"))
    return event_type


def project_history_item(
    event: Mapping[str, Any],
    *,
    actor: Mapping[str, Any],
    related: Mapping[str, Any],
    acknowledgements: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        payload = {}
    event_type = str(event.get("event_type") or "unknown")
    kind = history_detail_kind(event_type)
    item = {
        "schema_version": TASK_HISTORY_SCHEMA_VERSION,
        "event_id": int(event.get("id") or 0),
        "event_type": event_type,
        "occurred_at": event.get("created_at"),
        "actor": actor,
        "task_id": event.get("task_id") or "",
        "task_number": event.get("task_number"),
        "channel": event.get("channel") or payload.get("channel") or "",
        "summary": history_summary(event_type, payload),
        "detail_kind": kind,
        "related_ids": related_entity_ids(payload),
        "state_changes": state_changes(payload),
        "evidence_sections": evidence_sections(
            detail_kind=kind,
            payload=payload,
            related=related,
            acknowledgements=acknowledgements,
        ),
        "acknowledgements": acknowledgements,
        "priority": payload.get("priority"),
        "requires_ack": bool(payload.get("requires_ack")),
        "model_display_name": (
            message_model_display_name(payload) if kind == "message" else ""
        ),
        "verdict": payload.get("verdict") or (related.get("review") or {}).get("verdict"),
        "result": payload.get("result") or (related.get("integration") or {}).get("result"),
        "payload": redact_runtime_value(dict(payload)),
    }
    return redact_runtime_value(item)
