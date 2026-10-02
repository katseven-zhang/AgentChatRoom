"""#317: management-page and audit-pager responses must not cross projects.

``refreshManagement`` fires six requests, awaits them together and then writes
``state.members`` / ``credentials`` / ``workspaces`` / the audit window;
``loadOlderAuditEvents`` and ``loadNewerAuditEvents`` write the audit window
after a single await. None of them checked whether the user had switched
projects while the request was in flight, so a slow response from project A
landed on project B's management page (and only ``selectProject`` had a
generation guard).

The tests slice the real functions out of ``app.js`` and run them under node
with stubbed ``api`` / DOM, so the assertions cover the late-response behaviour
rather than the presence of a magic string.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = """
const assert = require('node:assert/strict');

const state = {
  projectId: 'project-a',
  selectGeneration: 0,
  auditFilter: '',
  auditEvents: [{project_seq: 10}, {project_seq: 11}],
  auditHasOlder: true,
  auditHasNewer: true,
  auditPage: 1,
  members: [],
  credentials: [],
  workspaces: [],
  runtime: null,
  managedBackups: [],
  autoBackupInfo: null,
};

const elements = {'audit-event-filter': {value: ''}};
let renderManagementCalls = 0;
let renderAuditCalls = 0;
function renderManagement() {
  renderManagementCalls += 1;
}
function renderAudit() {
  renderAuditCalls += 1;
}
function auditQueryUrl(projectId, options) {
  return `/api/v1/projects/${projectId}/audit?before=${options.before || ''}&after=${options.after || ''}&event_type=${options.eventType || ''}`;
}
async function fetchAuditTail(projectId) {
  return {events: [], has_older: false, has_newer: false};
}
const AUDIT_PAGE_SIZE = 50;

let waiters = [];
// Every request is deferred so the test decides when the in-flight responses of
// the OLD project finally land.
async function api(url) {
  return new Promise((resolve) => {
    waiters.push(() =>
      resolve({
        members: [],
        credentials: [],
        workspaces: [],
        events: [{project_seq: 5}],
        has_older: true,
        has_newer: true,
        total: 1,
        page_info: {},
        runtime: {},
        backups: [],
        auto_backup: null,
      })
    );
  });
}

function releaseAll() {
  const waiting = waiters;
  waiters = [];
  waiting.forEach((release) => release());
}
"""

BODY = r"""
// --- refreshManagement: a late answer must not paint the new project -------
const inFlight = refreshManagement();
assert.equal(waiters.length, 6, 'all six management requests went out');
state.projectId = 'project-b';
state.selectGeneration += 1;
state.members = [{id: 'member-of-b'}];
releaseAll();
await inFlight;
assert.deepEqual(
  state.members,
  [{id: 'member-of-b'}],
  'project A members must not overwrite project B',
);
assert.equal(renderManagementCalls, 0, 'a stale management refresh does not re-render');

// --- the same refresh still works when the project did not change ---------
state.projectId = 'project-a';
state.selectGeneration += 1;
const current = refreshManagement();
releaseAll();
await current;
assert.equal(state.members.length, 0, 'the fresh response is applied');
assert.equal(renderManagementCalls, 1);

// --- audit paging in both directions --------------------------------------
async function pageStale(loader) {
  state.auditEvents = [{project_seq: 10}];
  state.auditHasOlder = true;
  state.auditHasNewer = true;
  state.auditPage = 2;
  const run = loader();
  await Promise.resolve();
  state.projectId = 'project-c';
  state.selectGeneration += 1;
  const windowBefore = state.auditEvents;
  releaseAll();
  await run;
  assert.equal(state.auditEvents, windowBefore, 'the audit window is untouched');
  assert.equal(state.auditPage, 2, 'the page counter is not advanced by a stale page');
}

await pageStale(loadOlderAuditEvents);
await pageStale(loadNewerAuditEvents);

// --- #319: a burst larger than one page must not claim the tail -----------
state.projectId = 'project-a';
state.selectGeneration += 1;
state.auditFilter = '';
state.auditEvents = [{project_seq: 20}, {project_seq: 21}];
state.auditHasNewer = false; // we were looking at the live tail
state.auditHasOlder = false;
const burst = refreshManagement();
releaseAll(); // the stub answers has_newer: true — more events past this window
await burst;
assert.equal(
  state.auditHasNewer,
  true,
  'the server said newer events exist past this window, so paging must stay open',
);
assert.ok(
  state.auditEvents.some((event) => event.project_seq === 5),
  'the fresh events are still merged into the window',
);
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    refresh_start = javascript.index("async function refreshManagement() {")
    # The scope helpers come from the fix; on a tree without them the harness
    # still runs the original functions, so the probe fails on behaviour (a
    # stale response painting the new project) rather than on a missing symbol.
    helpers_start = javascript.rfind("function requestAnchor() {", 0, refresh_start)
    helpers_start = refresh_start if helpers_start < 0 else helpers_start
    refresh_end = javascript.index("function renderReportEvidence(")
    loaders_start = javascript.index("async function loadOlderAuditEvents()")
    loaders_end = javascript.index("async function selectProject", loaders_start)
    # The helpers live next to refreshManagement while the two audit loaders
    # sit earlier in the file, so the harness stitches both regions together.
    harness = tmp_path / "management_scope.js"
    harness.write_text(
        PREAMBLE
        + "\n"
        + javascript[helpers_start:refresh_end]
        + "\n"
        + javascript[loaders_start:loaders_end]
        + "\n(async () => {"
        + BODY
        + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_management_and_audit_responses_do_not_cross_projects(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_every_late_writing_loader_is_scoped():
    """Anchored on the functions that exist in the base too, so the proof is
    about behaviour rather than the presence of the helper."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for name in (
        "async function refreshManagement()",
        "async function loadOlderAuditEvents()",
        "async function loadNewerAuditEvents()",
    ):
        body = javascript[javascript.index(name) : javascript.index("\n}\n", javascript.index(name)) + 3]
        assert "requestAnchor()" in body or "auditAnchor()" in body, name
        assert "withinRequestScope(anchor)" in body, name
        assert "const projectId = anchor.projectId;" in body or "anchor.eventType" in body, name


INTAKE_PREAMBLE = """
const assert = require('node:assert/strict');

const state = {
  projectId: 'project-a',
  selectGeneration: 0,
  taskIntakeTargets: [{member_id: 'previous', name: 'previous'}],
};

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

const elements = {
  'task-target-agent-input': {innerHTML: 'KEEP', disabled: true},
  'task-target-agent-empty': {textContent: 'KEEP', classList: {toggle() {}}},
  'intake-target-select': {innerHTML: 'KEEP'},
  'task-intake-submit': {disabled: true},
};

let waiter = null;
async function api(url) {
  return new Promise((resolve) => {
    waiter = () =>
      resolve({
        targets: [
          {member_id: 'member-x', name: 'Agent X', client: 'client', software_key: 'sk', connection_status: 'connected'},
        ],
      });
  });
}
"""

INTAKE_BODY = r"""
const applied = refreshTaskIntakeTargets();
await Promise.resolve();
state.projectId = 'project-b';
state.selectGeneration += 1;
const targetsBefore = state.taskIntakeTargets;
waiter();
const ok = await applied;
assert.equal(ok, false, 'a stale target list reports no usable targets');
assert.equal(state.taskIntakeTargets, targetsBefore, "another project's targets are not stored");
assert.equal(elements['task-target-agent-input'].innerHTML, 'KEEP', 'the select is not repainted');
assert.equal(elements['task-intake-submit'].disabled, true, 'submit stays disabled');

const current = refreshTaskIntakeTargets();
await Promise.resolve();
waiter();
assert.equal(await current, true, 'a current response is still applied');
assert.equal(state.taskIntakeTargets.length, 1);
assert.equal(elements['task-intake-submit'].disabled, false);
"""


def _intake_harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    helpers_start = javascript.index("function requestAnchor() {")
    start = javascript.index("async function refreshTaskIntakeTargets() {")
    end = javascript.index("async function refreshTaskIntakeData(", start)
    anchor = javascript[
        helpers_start : javascript.index("async function refreshManagement()")
    ]
    harness = tmp_path / "intake_targets.js"
    harness.write_text(
        INTAKE_PREAMBLE
        + "\n"
        + anchor
        + "\n"
        + javascript[start:end]
        + "\n(async () => {"
        + INTAKE_BODY
        + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_intake_target_refresh_does_not_cross_projects(tmp_path):
    harness = _intake_harness(tmp_path)
    if "function requestAnchor() {" not in harness.read_text(encoding="utf-8"):
        pytest.fail("no scope helper in this tree")
    run = subprocess.run(["node", str(harness)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
