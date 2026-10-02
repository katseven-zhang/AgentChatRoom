"""#320: a busy-room refresh must not rebuild the task-number box while typing."""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = r"""
const assert = require('node:assert/strict');
const rebuilt = [];
const input = {
  dataset: { expert: 'number' },
  value: '',
  selectionStart: 0,
  selectionEnd: 0,
  focus() { document.activeElement = input; },
  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  },
};
const container = {
  dataset: {},
  querySelector() { return input; },
  set innerHTML(value) { rebuilt.push(String(value)); },
  get innerHTML() { return rebuilt[rebuilt.length - 1] || ''; },
};
const document = {
  activeElement: input,
  getElementById(id) { return id === 'task-expert-filters' ? container : null; },
};
globalThis.document = document;
function escapeHtml(value) { return String(value ?? ''); }
function taskView(task) {
  return {
    phase: task.phase || 'claimed',
    execution_status: 'claimed',
    verification_status: 'pending',
    integration_status: 'pending',
  };
}
function viewLabel(code) { return code; }
function viewFaceLabel(_kind, code) { return code; }
function taskViewConfig() { return { phases: ['claimed', 'todo'] }; }
function shortId(id) { return String(id).slice(0, 6); }
const state = {
  snapshot: { agents: [{ id: 'agent-1', name: 'Ada' }] },
  taskExpert: {
    execution: '', verification: '', integration: '', priority: '', owner: '', number: '12', phase: '',
  },
};
const tasks = [
  { id: 't1', owner_session_id: 'agent-1', phase: 'claimed', priority: 2 },
];
"""

BODY = r"""
input.value = '12';
input.selectionStart = 2;
input.selectionEnd = 2;
renderTaskExpertFilters(tasks);
assert.equal(rebuilt.length, 1);
const box = container.querySelector('[data-expert="number"]');
assert.equal(box, input, 'the first paint binds the live input');

input.value = '123';
state.taskExpert.number = '123';
input.selectionStart = 3;
renderTaskExpertFilters(tasks);
assert.equal(rebuilt.length, 1, 'typing does not rebuild the filter bar');
assert.equal(input.value, '123', 'the characters the user typed stay in the same input');
assert.equal(document.activeElement, input);

tasks.push({ id: 't2', owner_session_id: 'agent-2', phase: 'todo', priority: 1 });
state.snapshot.agents.push({ id: 'agent-2', name: 'Bea' });
renderTaskExpertFilters(tasks);
assert.equal(rebuilt.length, 2, 'a real change in the option set still rebuilds');
assert.equal(document.activeElement, input, 'focus returns to the task-number box');
assert.equal(input.selectionStart, 3, 'the caret is restored');
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function taskExpertFilterSignature(")
    end = javascript.index("function renderTaskSortControls(", start)
    harness = tmp_path / "task_filter.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n" + BODY,
        encoding="utf-8",
    )
    return harness


def test_task_number_filter_survives_a_busy_room_refresh(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
