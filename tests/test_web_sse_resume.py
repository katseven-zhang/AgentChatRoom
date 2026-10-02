"""#313: the event stream must resume from what the feed actually applied.

On `onerror` the browser-side stream reconnected with ``state.snapshot?.cursor``
— the snapshot endpoint's server-side high-water mark, which keeps advancing
from polling even while the stream is down. Resuming with "events newer than
this id" from that value silently skipped everything that landed between the
last applied event and the polled high-water, so a brief blip permanently lost
Room events from the feed.

The harness slices the real ``connectEvents`` out of ``app.js`` and stubs
``EventSource`` plus its collaborators, so the assertions are about the resume
cursor that actually leaves the client.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = """
const assert = require('node:assert/strict');

const state = {
  projectId: 'project-1',
  events: [],
  eventIds: new Set(),
  snapshot: {cursor: 1970},
  eventSource: null,
  streamHadError: false,
};

const opened = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    opened.push(this);
  }
  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }
  close() {
    this.closed = true;
  }
}

const EventSource = FakeEventSource;

let connectionStates = [];
function setConnection(kind, text) {
  connectionStates.push([kind, text]);
}
function scheduleStreamRetry(projectId, after) {
  retried.push(after);
}
const retried = [];
let probed = [];
async function probeAuthAndReconnect(projectId, after) {
  probed.push(after);
  if (probeShouldFail) {
    probeShouldFail = false;
    throw new Error('auth probe failed');
  }
}
let probeShouldFail = false;

async function refreshSnapshot(projectId, minCursor) {
  return {cursor: minCursor};
}
function applySnapshotIfCurrent() {
  return true;
}
function renderAll() {}
function renderForEvent() {}
function mergeEvents(events) {
  for (const event of events) {
    if (state.eventIds.has(event.id)) continue;
    state.eventIds.add(event.id);
    state.events.push(event);
  }
}
function showToast() {}
"""

BODY = r"""
// --- resume from the newest event the feed actually applied ---------------
connectEvents(100);
assert.ok(opened[0].url.includes('after=100'), 'the first subscription honours its cursor');

// Two events arrive on the stream, then the connection blips.
state.events.push({id: 140}, {id: 141});
state.snapshot = {cursor: 1970}; // the snapshot poll ran ahead of the feed
opened[0].onerror();
assert.equal(probed.length, 1, 'the blip triggers one reconnect probe');
assert.equal(
  probed[0],
  141,
  'the reconnect must resume at the last applied event, not the polled high-water',
);
assert.notEqual(
  probed[0],
  1970,
  'resuming at the snapshot cursor would skip events 142..1970 forever',
);

// --- the retry fallback uses the same resume point -------------------------
probed = [];
retried.length = 0;
probeShouldFail = true;
opened[0].onerror();
await new Promise((resolve) => setImmediate(resolve));
assert.equal(retried[0], 141, 'the retry path must not fall back to the snapshot cursor');

// --- with nothing applied yet, the original subscription cursor is kept ----
probed = [];
state.events = [];
state.eventIds = new Set();
connectEvents(200);
opened[opened.length - 1].onerror();
assert.equal(probed[0], 200, 'an empty feed resumes where it first subscribed');
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.rfind(
        "function lastAppliedEventCursor() {", 0, javascript.index("function connectEvents(")
    )
    if start < 0:  # a tree without the fix: run the original function as-is
        start = javascript.index("function connectEvents(")
    end = javascript.index("function projectGroupKey(", start)
    harness = tmp_path / "sse_resume.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n(async () => {" + BODY + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_event_stream_resumes_from_the_applied_feed(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_resume_cursor_does_not_read_the_polled_snapshot():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    body = javascript[
        javascript.index("function connectEvents(") : javascript.index(
            "function projectGroupKey("
        )
    ]
    assert "state.snapshot?.cursor || after" not in body
    assert "lastAppliedEventCursor()" in body
