"""#265: full snapshot polling is only the fallback while SSE is down."""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = r"""
const assert = require('node:assert/strict');
let streamOnline = false;
const state = {
  projectId: 'project-1',
  presenceTimer: null,
  config: {},
  snapshot: { cursor: 10, project: { id: 'project-1' } },
  snapshotInFlight: null,
};
const document = { hidden: false };
const timers = [];
function setInterval(fn, ms) { timers.push(ms); return timers.length; }
function clearInterval() {}
async function refreshPresence() {}
"""

BODY = r"""
startPresenceRefresh();
assert.equal(timers.length, 1);
assert.ok(timers[0] >= 5000, 'the fallback poll is at least 5 seconds');
timers.length = 0;
streamOnline = true;
startPresenceRefresh();
assert.equal(timers.length, 0, 'a live event stream does not start snapshot polling');
document.hidden = true;
streamOnline = false;
startPresenceRefresh();
assert.equal(timers.length, 0, 'a hidden page does not poll');

let requested = null;
function api(path) { requested = path; return Promise.resolve({ cursor: 11, project: { id: 'project-1' } }); }
const direct = fetchSnapshotDirect.toString();
assert.equal(direct.includes('arguments['), false);
assert.equal(direct.includes('min_cursor=${'), false);
const skipped = refreshSnapshot('project-1', { minCursor: 10 });
assert.equal(requested, null);
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function fetchSnapshotDirect(")
    end = javascript.index("async function api(", start)
    refresh_start = javascript.index("function stopPresenceRefresh(")
    refresh_end = javascript.index("async function refreshPresence(", refresh_start)
    harness = tmp_path / "presence_fallback.js"
    harness.write_text(
        PREAMBLE
        + "\n"
        + javascript[start:end]
        + "\n"
        + javascript[refresh_start:refresh_end]
        + "\n"
        + BODY,
        encoding="utf-8",
    )
    return harness


def test_snapshot_polling_waits_for_the_event_stream_to_drop(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_late_intake_targets_do_not_cross_projects(tmp_path):
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    helpers = javascript[
        javascript.index("function requestAnchor() {")
        : javascript.index("function withinRequestScope(anchor) {")
        + len("function withinRequestScope(anchor) {\n  return (\n    anchor.projectId === state.projectId && anchor.generation === state.selectGeneration\n  );\n}\n")
    ]
    refresh = javascript[
        javascript.index("async function refreshTaskIntakeTargets(")
        : javascript.index("let taskIntakeRefreshSequence", javascript.index("async function refreshTaskIntakeTargets("))
    ]
    script = tmp_path / "targets.js"
    script.write_text(
        helpers
        + "\n"
        + refresh
        + r"""
const assert = require('node:assert/strict');
const state = { projectId: 'project-a', selectGeneration: 1, taskIntakeTargets: [{ member_id: 'kept' }] };
const elements = {
  'task-target-agent-input': { innerHTML: 'old', disabled: false },
  'task-target-agent-empty': { textContent: '', classList: { toggle() {} } },
  'task-intake-submit': { disabled: true },
};
function escapeHtml(value) { return String(value ?? ''); }
let release;
function api() { return new Promise((resolve) => { release = resolve; }); }
const pending = refreshTaskIntakeTargets();
state.projectId = 'project-b';
release({ targets: [{ member_id: 'other', name: 'Other', connection_status: 'connected', client: 'cli' }] });
pending.then((applied) => {
  assert.equal(applied, false);
  assert.deepEqual(state.taskIntakeTargets, [{ member_id: 'kept' }]);
  assert.equal(elements['task-target-agent-input'].innerHTML, 'old');
}).catch((error) => { console.error(error); process.exit(1); });
""",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_backup_keep_count_rejects_blank_and_negative_locally(tmp_path):
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function parseKeptCount(")
    end = javascript.index("let backupSettingsRequest", start)
    script = tmp_path / "kept.js"
    script.write_text(
        javascript[start:end]
        + "\n"
        + "const assert = require('node:assert/strict');\n"
        + "assert.equal(parseKeptCount(''), null);\n"
        + "assert.equal(parseKeptCount('-1'), null);\n"
        + "assert.equal(parseKeptCount('0'), null);\n"
        + "assert.equal(parseKeptCount('3'), 3);\n",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
