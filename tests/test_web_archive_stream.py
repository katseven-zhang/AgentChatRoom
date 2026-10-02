"""#328 (first clause): a failed archive/delete must not silence the page.

The archive handler ran ``closeEventSource()`` *before* issuing ``DELETE``, so
when the request failed the project was still selected and still rendered, but
its live event stream had already been torn down — the panel kept looking alive
while never updating again until the user switched projects by hand.

The harness extracts the real ``submit`` listener from ``app.js`` and drives it
with a stubbed ``api``, so the assertion is about whether a failed destructive
call leaves the stream connected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = """
const assert = require('node:assert/strict');

const state = {projectId: 'project-1', eventSource: {closed: false}};
const captured = {};
const elements = {
  'archive-form': {addEventListener: (name, handler) => {captured.submit = handler;}},
  'permanent-delete-input': {checked: false},
  'archive-dialog': {close() {this.closed = true;}},
};
let closeCalls = 0;
function closeEventSource() {
  closeCalls += 1;
  state.eventSource = null;
}
let loadProjectsCalls = 0;
async function loadProjects() {
  loadProjectsCalls += 1;
}
function showToast() {}
let handled = [];
function handleError(error) {
  handled.push(error.message);
}
const localStorage = {removeItem() {}};
let apiShouldFail = false;
async function api() {
  if (apiShouldFail) throw new Error('archive refused: task still in progress');
}
"""

BODY = r"""
// --- the delete fails: the stream must stay connected ----------------------
apiShouldFail = true;
closeCalls = 0;
handled = [];
await captured.submit({preventDefault() {}});
assert.equal(closeCalls, 0, 'a failed archive must not tear the event stream down');
assert.equal(state.projectId, 'project-1', 'the project stays selected');
assert.equal(handled.length, 1, 'the failure is still reported to the user');
assert.notEqual(elements['archive-dialog'].closed, true, 'the dialog stays open');

// --- the delete succeeds: only then does the stream close -------------------
apiShouldFail = false;
closeCalls = 0;
elements['permanent-delete-input'].checked = true;
await captured.submit({preventDefault() {}});
assert.equal(closeCalls, 1, 'a completed archive closes the stream');
assert.equal(state.projectId, null, 'the archived project is deselected');
assert.equal(loadProjectsCalls, 1);
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index('elements["archive-form"].addEventListener(')
    end = javascript.index('elements["permanent-delete-input"].addEventListener(', start)
    harness = tmp_path / "archive_stream.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n(async () => {" + BODY + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_failed_archive_keeps_the_event_stream(tmp_path):
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    block = javascript[
        javascript.index('elements["archive-form"].addEventListener(')
        : javascript.index('elements["permanent-delete-input"].addEventListener(')
    ]
    assert "closeEventSource();" in block, "the harness must be exercising a real stream close"
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_stream_close_follows_the_destructive_request():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    block = javascript[
        javascript.index('elements["archive-form"].addEventListener(')
        : javascript.index('elements["permanent-delete-input"].addEventListener(')
    ]
    assert block.index('method: "DELETE"') < block.index("closeEventSource();"), (
        "the stream may only be closed after the archive/delete actually succeeded"
    )
