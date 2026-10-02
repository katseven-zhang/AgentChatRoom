"""#266 (frontend half): the task-history pager must trust the server's edges.

``loadTaskHistory`` merged the two page-boundary flags by hard-coding the
*opposite* direction to ``true`` ("if we just paged older, newer must exist").
That holds for the first hop but not at an edge: once the server reports
``has_more_before = false`` for the window it returned, a forced ``true`` keeps
「上一页/下一页」 enabled forever, the next click fetches an empty page, and the
walk-to-focus loop never terminates. The REST history response already answers
both flags for the returned window (``services.py`` ``_has_further``), so the
client has no reason to invent either one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = """
const assert = require('node:assert/strict');

const state = {projectId: 'project-1', taskHistory: null};
const elements = {'task-history-filter': {value: ''}};
let lastUrl = '';
let pageFor = () => ({});
async function api(url) {
  lastUrl = url;
  return pageFor(url);
}
"""

BODY = r"""
// --- paging towards newer items from the oldest edge ----------------------
state.taskHistory = {
  taskId: 'task-1',
  items: [{event_id: 1}],
  total: 3,
  next_after: 1,
  next_before: 1,
  has_more_after: true,
  has_more_before: false,
};
pageFor = () => ({
  items: [{event_id: 2}],
  total: 3,
  has_more_after: true,
  has_more_before: false,
});
await loadTaskHistory('task-1', {direction: 'later'});
assert.ok(lastUrl.includes('after=1'), 'paging later sends the after cursor');
assert.equal(
  state.taskHistory.has_more_before,
  false,
  'the oldest edge must stay closed after paging later',
);

// --- paging towards older items from the newest edge ----------------------
state.taskHistory = {
  taskId: 'task-1',
  items: [{event_id: 9}],
  total: 3,
  next_after: 9,
  next_before: 9,
  has_more_after: false,
  has_more_before: true,
};
pageFor = () => ({
  items: [{event_id: 8}],
  total: 3,
  has_more_after: false,
  has_more_before: true,
});
await loadTaskHistory('task-1', {direction: 'earlier'});
assert.ok(lastUrl.includes('before=9'), 'paging earlier sends the before cursor');
assert.equal(
  state.taskHistory.has_more_after,
  false,
  'the newest edge must stay closed after paging earlier',
);

// --- the first load trusts both answers as they are -----------------------
state.taskHistory = null;
pageFor = () => ({items: [], total: 0, has_more_after: false, has_more_before: false});
await loadTaskHistory('task-1');
assert.equal(state.taskHistory.has_more_after, false);
assert.equal(state.taskHistory.has_more_before, false);

// --- an interior window still reports both directions open ----------------
state.taskHistory = null;
pageFor = () => ({items: [{event_id: 5}], total: 9, has_more_after: true, has_more_before: true});
await loadTaskHistory('task-1');
assert.equal(state.taskHistory.has_more_after, true);
assert.equal(state.taskHistory.has_more_before, true);
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("async function loadTaskHistory(")
    end = javascript.index("function formatSpecReceipt(")
    harness = tmp_path / "task_history_pager.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n(async () => {" + BODY + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_task_history_pager_uses_the_servers_page_boundaries(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_task_history_pager_no_longer_invents_an_edge():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    body = javascript[
        javascript.index("async function loadTaskHistory(") : javascript.index(
            "function formatSpecReceipt("
        )
    ]
    assert "? true : Boolean(page.has_more" not in body
    assert "has_more_after: Boolean(page.has_more_after)" in body
    assert "has_more_before: Boolean(page.has_more_before)" in body
    # The buttons are still driven by those two flags.
    assert 'elements["task-history-load-earlier"].disabled = !history?.has_more_before' in javascript
    assert 'elements["task-history-load-later"].disabled = !history?.has_more_after' in javascript
