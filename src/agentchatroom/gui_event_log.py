"""Read-only Room event stream adapter for the local GUI log.

The durable event table is the source of truth. CLI/server text is not parsed
for business events, and polling never changes Room cursors or Presence.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from queue import Queue
from typing import Any

from .api import redact_log_line
from .config import Settings
from .database import create_database
from .errors import DomainError
from .services import AgentChatRoomService

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_EVENT_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_WINDOWS_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:\\|\\\\)[^\s，。；;\]\[()]{3,}")
_OPAQUE_ID = re.compile(
    r"\b(?:agent|session|transport|project|task|credential)_[0-9a-f]{12,}\b"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_BARE_HEX_ID = re.compile(r"\b[0-9a-f]{24,64}\b", re.IGNORECASE)
_SESSION_LABEL = re.compile(
    r"\b(?:(?:mcp[-_ ]?)?(?:session|transport)[-_ ]?id"
    r"|(?:mcp)?(?:session|transport)id)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_HTTP_STATUS = re.compile(r"HTTP/\d(?:\.\d)?[\s\"]+([1-5]\d\d)\b", re.IGNORECASE)
_QUIET_EVENTS = frozenset({"session.heartbeat", "message.acknowledged"})
_STATUS_ZH = {
    "todo": "待开始", "claimed": "已认领", "in_progress": "进行中",
    "blocked": "阻塞", "completed": "执行完成", "awaiting_review": "待独立验收",
    "verified": "验收通过", "changes_requested": "要求修改",
    "pending_integration": "待集成", "done": "已集成", "cancelled": "已取消",
}


def sanitize_gui_text(value: Any, *, limit: int = 500) -> str:
    text = _CONTROL.sub(" ", str(value or "")).strip()
    text = _WINDOWS_PATH.sub("[路径已隐藏]", text)
    text = _SESSION_LABEL.sub("会话标识=[已隐藏]", text)
    text = _OPAQUE_ID.sub("[标识已隐藏]", text)
    text = _BARE_HEX_ID.sub("[标识已隐藏]", text)
    text = text[:limit] + ("…" if len(text) > limit else "")
    return redact_log_line(text)


def _safe(value: Any, *, limit: int = 100) -> str:
    return sanitize_gui_text(value, limit=limit)


def format_service_line(line: str) -> str | None:
    """Show actionable lifecycle/errors, not high-volume transport diagnostics."""
    raw_lower = str(line).lower()
    clean = sanitize_gui_text(line)
    if not clean:
        return None
    lower = clean.lower()
    status = _HTTP_STATUS.search(clean)
    if status:
        code = int(status.group(1))
        return None if code < 400 else f"服务接口请求失败（HTTP {code}）；详情请查看服务日志。"
    if "waiting for application startup" in lower:
        return "服务正在初始化…"
    if "application startup complete" in lower:
        return "服务初始化完成。"
    if "shutting down" in lower:
        return "服务正在关闭…"
    if "application shutdown complete" in lower:
        return "服务已关闭。"
    if "started server process" in lower:
        return "服务进程已启动。"
    if "finished server process" in lower:
        return "服务进程已退出。"
    if "running on http" in lower:
        match = re.search(r"https?://[^\s]+", clean)
        return f"服务正在监听 {match.group(0)}" if match else "服务已开始监听。"
    if "streamablehttp session manager" in raw_lower and not any(
        marker in raw_lower for marker in ("error", "warning", "exception", "failed", "fatal", "critical")
    ):
        return None
    if any(fragment in raw_lower for fragment in (
        "http request:", "processing request of type", "received request of type",
        "mcp-session-id", "mcp session", "session id", "session_id", "mcp request",
        "terminating session:", "idle timeout", "transport for session",
        "keepalive timeout",
    )) and not any(fragment in raw_lower for fragment in (
        "error", "warning", "exception", "failed", "fatal", "critical",
    )):
        # Agent join/leave, task, lease and message changes come from the
        # structured Room event stream below; raw MCP chatter is not useful.
        return None
    if any(fragment in lower for fragment in ("error", "traceback", "exception", "failed", "fatal", "critical")):
        return f"服务错误：{clean}"
    if "warning" in lower:
        return f"服务警告：{clean}"
    if raw_lower.lstrip().startswith("info:"):
        return None
    return f"服务输出：{clean}"


def _local_time(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_room_event(
    event: Mapping[str, Any], project_name: str, *, task_title: str = "",
    reconnected: bool = False,
) -> str | None:
    """Format one structured event; never interpolate an untrusted raw payload."""
    kind = str(event.get("event_type") or "")
    if kind in _QUIET_EVENTS:
        return None
    payload = event.get("payload")
    data = payload if isinstance(payload, Mapping) else {}
    actor_data = data.get("actor")
    actor = actor_data if isinstance(actor_data, Mapping) else {}
    name = _safe(actor.get("name") or data.get("name") or "系统", limit=48)
    project = _safe(project_name or "未命名项目", limit=60)
    number = event.get("task_number") or data.get("task_number")
    task = f"任务 #{number}" if isinstance(number, int) and number > 0 else "任务"
    title = _safe(task_title or data.get("title"), limit=100)
    if title:
        task += f"「{title}」"
    raw_status = str(data.get("status") or data.get("execution_status") or "")
    status = _STATUS_ZH.get(raw_status, _safe(raw_status, limit=32))
    reason = _safe(data.get("blocker_reason") or data.get("reason") or data.get("notes"), limit=160)
    progress = data.get("progress_percent")
    progress_note = f"，进度 {progress}%" if isinstance(progress, int) and 0 <= progress <= 100 else ""
    verdict = str(data.get("verdict") or "")

    messages = {
        "agent.joined": f"Agent {name} {'重新接入' if reconnected else '接入'} Room",
        "agent.left": f"Agent {name} 主动离开 Room",
        "agent.session_replaced": None,  # joined follows in the same transaction
        "agent.identity_registered": f"Agent {name} 首次登记",
        "agent.credential_linked": f"Agent {name} 已关联接入凭据",
        "task.created": f"{name} 发布了{task}",
        "task.intake_submitted": f"{name} 提交了任务需求",
        "task.intake_defined": f"任务需求已整理为正式{task}",
        "task.intake_reassigned": "任务需求已重新分派",
        "task.intake_acknowledged": "任务需求已确认",
        "task.claimed": f"{name} 认领了{task}",
        "task.reclaimed": (
            f"{name} 显式接管了{task}，原会话的任务写入权限已撤销"
            if data.get("explicit_live_takeover") else f"{name} 重新认领了{task}"
        ),
        "task.assigned": f"{name} 指派了{task}",
        "task.assignment_acknowledged": f"{name} 确认了{task}的指派",
        "task.assignment_cancelled": f"{task}的指派已取消",
        "task.handoff_requested": f"{name} 发起了{task}的交接",
        "task.handoff_acknowledged": f"{task}交接已确认",
        "task.handoff_cancelled": f"{task}交接已取消",
        "task.updated": f"{name} 更新了{task}" + (f"，状态：{status}" if status else "") + progress_note,
        "task.blocked": f"{task}已阻塞" + (f"，原因：{reason}" if reason else ""),
        "task.unblocked": f"{task}已解除阻塞",
        "task.released": f"{name} 释放了{task}",
        "task.cancelled": f"{task}已取消",
        "work.reported": f"{name} 提交了{task}的工作报告，等待独立验收",
        "work.commit_unverified": f"{task}的提交证据无法验证，工作报告未被接受",
        "task.completed": f"{task}执行完成，等待独立验收（尚未集成）",
        "review.submitted": f"{name} 对{task}提交独立验收：" + (
            "通过" if verdict == "approved" else "要求修改" if verdict == "changes_requested" else "已记录"
        ),
        "task.integration_completed": f"{task}已完成集成",
        "task.integration_failed": f"{task}集成未完成，请查看任务验收与集成记录",
        "message.message": f"{name} 发布了 Room 消息",
        "message.decision": f"{name} 发布了决策消息",
        "message.blocker": f"{name} 发布了阻塞消息",
        "lease.acquired": f"{name} 取得了文件租约",
        "lease.released": f"{name} 释放了文件租约",
        "lease.conflict": "文件租约发生冲突，相关任务需等待或协调",
        "lease.pre_commit_blocked": "提交前文件租约检查未通过",
        "document.created": f"{name} 新增了项目文档",
        "document.updated": f"{name} 更新了项目文档",
        "document.archived": f"{name} 归档了项目文档",
        "project.created": f"项目 {project} 已创建",
        "project.updated": f"项目 {project} 的设置已更新",
        "project.archived": f"项目 {project} 已归档",
        "project.restored": f"项目 {project} 已恢复",
    }
    if kind in messages:
        message = messages[kind]
        if message is None:
            return None
    else:
        code = kind if _EVENT_CODE.fullmatch(kind) else "unknown"
        sequence = event.get("project_seq")
        marker = f"（事件 #{sequence}）" if isinstance(sequence, int) and sequence > 0 else ""
        message = f"Room 记录了事件 {code}{marker}"
    level = (
        "错误" if kind.endswith("failed") or kind in {"task.blocked", "message.blocker"}
        else "提醒" if kind == "review.submitted" and verdict == "changes_requested"
        else "信息"
    )
    return redact_log_line(f"[{_local_time(event.get('created_at'))}] [{level}] [{project}] {message}")


class RoomEventTail(threading.Thread):
    """Follow new per-project events, without replaying old join/completion logs."""

    def __init__(self, settings: Settings, sink: Queue, stop_event: threading.Event,
                 *, service: AgentChatRoomService | None = None, poll_seconds: float = 1.0):
        super().__init__(daemon=True, name="agentchatroom-gui-events")
        self.settings = settings
        self.service = service or AgentChatRoomService(create_database(settings), settings)
        self.sink = sink
        self.stop_event = stop_event
        self.poll_seconds = poll_seconds
        self.cursors: dict[str, int] = {}
        self.replaced_sessions: set[str] = set()
        self.presence: dict[str, dict[str, str]] = {}
        self._next_presence_poll = 0.0
        self._error_reported = False

    def poll_once(self) -> None:
        for project in self.service.list_projects():
            project_id = str(project["id"])
            project_name = str(project.get("name") or "")
            if project_id not in self.cursors:
                baseline = self.service.list_events(project_id, after=0, limit=1)
                self.cursors[project_id] = int(baseline["latest_cursor"])
                continue
            for _ in range(5):  # bounded catch-up per poll; next poll continues
                page = self.service.list_events(project_id, after=self.cursors[project_id], limit=200)
                events = page["events"]
                if not events:
                    break
                for event in events:
                    seq = int(event["project_seq"])
                    if seq <= self.cursors[project_id]:
                        continue
                    self.cursors[project_id] = seq
                    kind = event.get("event_type")
                    session = str(event.get("actor_session_id") or "")
                    if kind == "agent.session_replaced" and session:
                        self.replaced_sessions.add(session)
                        if len(self.replaced_sessions) > 1024:
                            # A malformed/partial history must not grow this
                            # transient join hint without bound.
                            self.replaced_sessions.clear()
                    title = ""
                    if event.get("task_id"):
                        try:
                            title = str(self.service.get_task(project_id, str(event["task_id"])).get("title") or "")
                        except DomainError:
                            title = ""  # deleted task: event's own snapshot still formats
                    line = format_room_event(event, project_name, task_title=title,
                                             reconnected=session in self.replaced_sessions)
                    if kind == "agent.joined":
                        self.replaced_sessions.discard(session)
                    if line:
                        self.sink.put(("log", line))
                if len(events) < 200:
                    break

    def poll_presence_once(self) -> None:
        """Report a derived timeout only after a real online-to-offline transition."""
        for project in self.service.list_projects():
            project_id = str(project["id"])
            agents = self.service.snapshot(project_id).get("agents", [])
            current = {str(a["id"]): str(a.get("status") or "") for a in agents}
            previous = self.presence.get(project_id)
            if previous is not None:
                for agent in agents:
                    session_id = str(agent["id"])
                    if (previous.get(session_id) == "online"
                            and current[session_id] == "offline"
                            and not agent.get("left_at")):
                        name = _safe(agent.get("name") or "未知 Agent", limit=48)
                        project_name = _safe(project.get("name") or "未命名项目", limit=60)
                        message = (
                            f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] "
                            f"[提醒] [{project_name}] Agent {name} 心跳/活动超时，"
                            + "当前显示为离线（不是主动离开）。"
                        )
                        self.sink.put(("log", message))
            self.presence[project_id] = current

    def run(self) -> None:
        while not self.stop_event.is_set():
            if (self.settings.database_backend == "sqlite"
                    and not self.settings.database_path.exists()):
                self.stop_event.wait(self.poll_seconds)
                continue
            try:
                self.poll_once()
                if time.monotonic() >= self._next_presence_poll:
                    self.poll_presence_once()
                    self._next_presence_poll = time.monotonic() + 10
                self._error_reported = False
            except Exception as error:  # noqa: BLE001 - background watcher must retry
                if not self._error_reported:
                    self.sink.put(("log", "[Room 事件] 暂时无法读取事件流，稍后自动重试：" + _safe(type(error).__name__)))
                    self._error_reported = True
            self.stop_event.wait(self.poll_seconds)
