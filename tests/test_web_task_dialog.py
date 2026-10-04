"""#316: reopening the task dialog must not throw or wipe the paged history."""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = r"""
const assert = require('node:assert/strict');
const calls = [];
const dialog = {
  open: false,
  showModal() {
    if (this.open) {
      const error = new Error('InvalidStateError');
      error.name = 'InvalidStateError';
      throw error;
    }
    this.open = true;
    calls.push('showModal');
  },
};
const elements = {
  'task-edit-dialog': dialog,
  'task-history-filter': { value: '' },
  'task-assign-button': { disabled: false },
  'task-release-button': { classList: { toggle() {} } },
  'task-cancel-button': { classList: { toggle() {} } },
};
const state = {
  projectId: 'project-1',
  editingTaskId: 'task-1',
  focusEventId: 0,
  snapshot: {
    tasks: [
      { id: 'task-1', task_number: 1 },
      { id: 'task-2', task_number: 2 },
    ],
  },
  taskHistory: {
    taskId: 'task-1',
    items: [{ event_id: 7, project_seq: 7 }, { event_id: 8, project_seq: 8 }],
    eventType: '',
    has_more_after: true,
    has_more_before: true,
  },
};
function taskView() { return { phase: 'claimed' }; }
function taskReleaseVisible() { return false; }
function taskCancelVisible() { return false; }
function renderTaskContract() {}
function renderTaskAssignments() {}
function renderTaskTimeline() { calls.push('timeline:' + state.taskHistory.items.length); }
function showToast() { calls.push('toast'); }
async function loadTaskHistory() { calls.push('load'); }
const hashchanges = [];
const location = {
  href: 'http://127.0.0.1:8765/',
  get hash() { return this._hash || ''; },
  set hash(value) { this._hash = value; hashchanges.push(value); },
  _hash: '',
};
const history = { state: null, replaceState(_state, _title, url) { calls.push('replace:' + String(url)); } };
const navigator = { clipboard: { async writeText() { calls.push('copy'); } } };
function URL(value) {
  return { hash: '', toString() { return 'http://127.0.0.1:8765/#' + this.hash.replace(/^#/, ''); } };
}
"""

BODY = r"""
dialog.open = true;
const kept = state.taskHistory.items.slice();
await openTaskDetails('task-1', { focusEventId: 8 });
assert.deepEqual(state.taskHistory.items, kept, 'the open task keeps its paged history');
assert.deepEqual(calls.filter((item) => item === 'showModal'), [], 'showModal is not called on an open dialog');
assert.equal(calls.includes('load'), false, 'the same open task is not reloaded');

calls.length = 0;
await openTaskDetails('task-1');
assert.equal(state.taskHistory.items.length, 2);

calls.length = 0;
await copyEventReference(8, 1, 8);
assert.equal(hashchanges.length, 0, 'copying a reference must not assign location.hash');
assert.ok(calls.some((item) => item.startsWith('replace:')), 'the shareable hash is written with replaceState');

calls.length = 0;
dialog.open = true;
state.editingTaskId = 'task-1';
state.taskHistory.items = kept;
await openTaskDetails('task-2');
assert.deepEqual(calls.filter((item) => item === 'showModal'), [], 'a related task uses the dialog that is already open');
assert.ok(calls.includes('load'), 'a different task still loads its own history');
assert.equal(state.taskHistory.taskId, 'task-2');
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("async function openTaskDetails(")
    end = javascript.index("function eventFromCaches(", start)
    harness = tmp_path / "task_dialog.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n(async () => {" + BODY + "})().catch((error) => { console.error(error); process.exit(1); });\n",
        encoding="utf-8",
    )
    return harness


def test_open_task_dialog_keeps_history_and_does_not_renavigate(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
