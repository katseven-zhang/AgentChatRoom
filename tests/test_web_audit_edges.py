"""#317 clause 2 + #319 clauses 1-3: the audit view may only state edges the
server actually confirmed for the window it returned.

Two independent hard-codes were left in the pager:

* ``loadOlderAuditEvents`` asserted ``hasNewer = true`` and
  ``loadNewerAuditEvents`` asserted ``hasOlder = true``, so paging to an edge
  re-enabled the button that leads back into nothing;
* the live-tail merge forced ``hasNewer = false`` and ``auditPage = 1``, and the
  mid-history branch counted rows (``fresh.length > 0``) instead of reading the
  flag — so a burst wider than one page claimed the view was already at the end.

The harness runs the real ``loadOlderAuditEvents`` / ``loadNewerAuditEvents`` /
``refreshManagement`` from ``app.js`` against a configurable audit page, so the
assertions are about the flags the pager ends up showing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = """
const assert = require('node:assert/strict');

const state = {
  projectId: 'project-1',
  selectGeneration: 0,
  auditFilter: '',
  auditEvents: [],
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
function renderAudit() {}
function renderManagement() {}
function auditQueryUrl(projectId, options) {
  const parts = [];
  if (options.after) parts.push(`after=${options.after}`);
  if (options.before) parts.push(`before=${options.before}`);
  return `/api/v1/projects/${projectId}/audit?${parts.join('&')}`;
}
async function fetchAuditTail(projectId) {
  return {events: [], has_older: false, has_newer: false};
}
const AUDIT_PAGE_SIZE = 5;

let response = {};
async function api() {
  return {
    members: [], credentials: [], workspaces: [], backups: [], auto_backup: null,
    events: [], has_older: true, has_newer: true, ...response,
  };
}

function windowOf(ids) {
  return ids.map((id) => ({project_seq: id, id, event_type: 'message.message'}));
}
"""

BODY = r"""
// --- #317: the edge we came from is the server's answer, not an assumption --
response = {events: windowOf([10, 11]), has_older: true, has_newer: false};
state.auditEvents = windowOf([12, 13]);
state.auditHasOlder = true; // the button we are pressing
state.auditPage = 1;
await loadOlderAuditEvents();
assert.equal(state.auditHasOlder, true, 'older edge comes from the server');
assert.equal(
  state.auditHasNewer,
  false,
  'paging older from the newest page must not fabricate a newer side',
);

response = {events: windowOf([20, 21]), has_older: false, has_newer: true};
state.auditEvents = windowOf([18, 19]);
state.auditHasNewer = true; // the button we are pressing
state.auditPage = 3;
await loadNewerAuditEvents();
assert.equal(state.auditHasNewer, true, 'newer edge comes from the server');
assert.equal(
  state.auditHasOlder,
  false,
  'paging newer from the oldest page must not fabricate an older side',
);

// --- #319: a burst wider than one page must stay reachable ------------------
state.auditEvents = windowOf([40, 41, 42, 43, 44]);
state.auditFilter = '';
state.auditHasNewer = false; // we were on the live tail
state.auditHasOlder = false;
state.auditPage = 1;
response = {
  events: windowOf([45, 46, 47, 48, 49]),
  has_older: true,
  has_newer: true,
  latest_cursor: 60,
};
await refreshManagement();
assert.equal(
  state.auditHasNewer,
  true,
  'the server says newer events exist past this window, so 「下一页」 must stay enabled',
);
assert.equal(state.auditEvents[state.auditEvents.length - 1].project_seq, 49);

// criterion 3: those events are actually reachable from the enabled control.
response = {events: windowOf([50, 51, 52, 53, 54]), has_older: true, has_newer: false};
state.auditPage = 2;
await loadNewerAuditEvents();
assert.equal(state.auditEvents[state.auditEvents.length - 1].project_seq, 54);
assert.equal(state.auditHasNewer, false, 'the true tail is now in view');
assert.equal(state.auditPage, 1, 'and the page counter lands on the last page');

// --- #319: the mid-history branch reads the flag, not the row count ---------
state.auditEvents = windowOf([30, 31]);
state.auditHasNewer = true; // viewing mid-history
response = {events: [], has_older: true, has_newer: false};
await refreshManagement();
assert.equal(
  state.auditHasNewer,
  false,
  'an empty probe means the server found nothing newer; counting rows would keep claiming otherwise',
);

// --- and when the tail really is reached, the counter resets honestly ------
state.auditEvents = windowOf([50]);
state.auditHasNewer = false; // already looking at the live tail
state.auditPage = 4;
response = {events: windowOf([51]), has_older: true, has_newer: false};
await refreshManagement();
assert.equal(state.auditHasNewer, false);
assert.equal(state.auditPage, 1, 'only a confirmed tail may claim page 1');
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("async function loadOlderAuditEvents()")
    end = javascript.index("async function selectProject", start)
    refresh = javascript[
        javascript.index("function requestAnchor() {") : javascript.index(
            "function renderReportEvidence("
        )
    ]
    harness = tmp_path / "audit_edges.js"
    harness.write_text(
        PREAMBLE
        + "\n"
        + refresh
        + "\n"
        + javascript[start:end]
        + "\n(async () => {"
        + BODY
        + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_audit_pager_reports_only_server_confirmed_edges(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
