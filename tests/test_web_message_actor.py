"""Message authors must survive a bounded Session snapshot and later renames."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"


def _function(javascript: str, name: str) -> str:
    start = javascript.index(f"function {name}(")
    end = javascript.index("\n}\n", start) + 3
    return javascript[start:end]


@pytest.mark.parametrize(
    ("session_id", "payload", "event_type", "expected"),
    [
        ("session-old", {"body": "hello", "actor": {"name": "Remote A"}}, "message.message", "Remote A"),
        ("session-current", {"body": "hello", "actor": {"name": "Original name"}}, "message.message", "Original name"),
        ("session-current", {"body": "legacy message"}, "message.message", "Renamed agent"),
        ("session-current", {"body": "legacy message", "actor": {"name": " "}}, "message.message", "Renamed agent"),
        ("session-old", {"body": "legacy message"}, "message.message", "未知 Agent"),
        ("session-old", {"body": "hello", "actor": {"name": 42}}, "message.message", "未知 Agent"),
        (None, {"body": "operator message"}, "message.message", "用户"),
        (None, {"reason": "scheduled maintenance"}, "backup.created", "系统"),
        ("session-old", {"reason": "joined", "actor": {"name": "Remote A"}}, "agent.joined", "Remote A"),
        ("session-old", {"reason": "legacy activity"}, "agent.joined", "未知 Agent"),
        ("session-old", {"body": "hello", "actor": {"name": "<img src=x onerror=alert(1)>"}}, "message.message", "&lt;img src=x onerror=alert(1)&gt;"),
    ],
    ids=["truncated-session", "historical-name", "legacy-session", "blank-snapshot-name", "unknown-agent", "invalid-snapshot-name", "operator", "system", "activity-snapshot", "unknown-activity", "escaped-author"],
)
def test_message_and_recent_activity_authors(
    tmp_path: Path, session_id: str | None, payload: dict, event_type: str, expected: str
) -> None:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    actor_start = javascript.index("function snapshotAgentName(")
    actor_end = javascript.index("function renderMetrics(", actor_start)
    event = {
        "id": 11,
        "project_seq": 11,
        "actor_session_id": session_id,
        "event_type": event_type,
        "payload": payload,
        "created_at": "2026-01-01T00:00:00Z",
    }
    preamble = r"""
const assert = require('node:assert/strict');
const agents = [{id: 'session-current', name: 'Renamed agent'},
  ...Array.from({length: 199}, (_, index) => ({id: `new-session-${index}`, name: `Agent ${index}`}))];
const state = {snapshot: {project: {name: 'Example'}, agents, tasks: [], acknowledgements: []},
  events: [], lastRenderedEventId: 0};
const elements = {
  'chat-stream': {offsetParent: {}, clientHeight: 400, scrollHeight: 400, scrollTop: 0, innerHTML: ''},
  'new-message-notice': {classList: {add() {}, remove() {}}},
  'recent-activity-project': {textContent: ''}, 'recent-event-list': {innerHTML: ''}
};
const visibleFeedEvents = events => events;
const avatarColorClass = () => '';
const messageModelBadge = () => '';
const messageKind = kind => kind;
const eventIdBadge = seq => `#${seq}`;
const formatTime = time => time;
const renderMessageBodySafely = () => '';
const messageChannel = channel => channel;
const eventLabel = type => type;
const RECENT_ACTIVITY_PAGE_SIZE = 5;
let recentActivityPage = 1;
const gridPageSlice = rows => ({slice: rows, current: 1, totalPages: 1});
const gridPagerHtml = () => '';
"""
    assertions = "\nstate.events = [" + json.dumps(event, ensure_ascii=False) + "];\n"
    assertions += "renderEvents(agents, []); renderRecentActivity();\n"
    assertions += "const expected = " + json.dumps(expected, ensure_ascii=False) + ";\n"
    assertions += r"""
for (const id of ['chat-stream', 'recent-event-list']) {
  const html = elements[id].innerHTML;
  const author = html.match(/<strong>(.*?)<\/strong>/)[1];
  assert.equal(author, expected, `${id} must resolve the immutable actor before the Session lookup`);
  assert.ok(!html.includes('<img src=x onerror=alert(1)>'), `${id} must escape actor names`);
}
"""
    harness = tmp_path / "message-actor.cjs"
    harness.write_text(
        preamble
        + _function(javascript, "escapeHtml")
        + _function(javascript, "initials")
        + javascript[actor_start:actor_end]
        + _function(javascript, "renderEvents")
        + assertions,
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(harness)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, run.stderr
