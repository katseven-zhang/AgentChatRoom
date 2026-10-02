"""#313: the Room feed must be able to prove it did not lose events in a blip.

Three separate mistakes made a reconnected stream silently lose history:

* the reconnect cursor came from ``state.snapshot.cursor`` - a snapshot
  high-water that keeps advancing from polling while the stream is down, so
  resuming "events newer than X" skipped everything between the last applied
  event and that mark;
* even with the right cursor, reconnecting only starts pushing *new* events:
  whatever landed during the break is still missing unless somebody asks for it;
* the feed de-duplicates on the physical ``event.id`` while ``/events?after=``
  and the SSE cursor are the Project's ``project_seq`` - two different number
  spaces (``api.py`` notes the stream cursor "uses project_seq (same space as
  ?after=)"), so a watermark built from ids resumes from a value that means
  something else entirely.

The harness slices the real ``mergeEvents`` / ``connectEvents`` out of
``app.js`` and drives them with a stubbed stream, so the assertions cover the
cursor that leaves the client *and* the catch-up that has to follow it.
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
  events: [],
  eventIds: new Set(),
  snapshot: {cursor: 1970},
  eventSource: null,
  streamHadError: false,
  streamCatchUpInFlight: false,
};

function row(seq, id) {
  return {id, project_seq: seq, event_type: 'message.message', payload: {body: `e${seq}`}};
}

const opened = [];
class FakeEventSource {
  constructor(url) {
    this.url = url;
    opened.push(this);
  }
  addEventListener() {}
  close() {
    this.closed = true;
  }
}
const EventSource = FakeEventSource;

const urls = [];
async function api(url) {
  urls.push(url);
  return {events: []};
}
function showToast() {}
function renderAll() {}
function renderForEvent() {}
function setConnection() {}
function scheduleStreamRetry() {}
const probed = [];
async function probeAuthAndReconnect(projectId, after) {
  probed.push(after);
}
let refreshSnapshotCalls = 0;
async function refreshSnapshot() {
  refreshSnapshotCalls += 1;
  return {cursor: 1970};
}
function applySnapshotIfCurrent() {
  return true;
}
"""

BODY = r"""
// --- clause 1: the resume cursor is the feed's own project_seq watermark ---
state.events = [row(140, 900), row(141, 901)];
state.eventIds = new Set([900, 901]);
state.snapshot = {cursor: 1970};
connectEvents(100);
opened[0].onerror();
assert.equal(
  probed[0],
  141,
  'reconnect must resume at the newest applied project_seq, not the snapshot high-water',
);
assert.notEqual(probed[0], 1970, 'the polled snapshot cursor must never be the resume point');
assert.notEqual(
  probed[0],
  901,
  'the physical event id is a different number space and must not be used either',
);

// --- clause 2: the reopened stream asks for what it missed -----------------
urls.length = 0;
state.events = [row(141, 901)];
state.eventIds = new Set([901]);
state.streamHadError = true;
connectEvents(141);
opened[opened.length - 1].onopen();
await new Promise((resolve) => setImmediate(resolve));
assert.ok(
  urls.some((url) => url.includes('/events?after=141')),
  `reconnect must backfill /events?after=<watermark>, saw ${JSON.stringify(urls)}`,
);

// --- clause 3: a hole on the right edge triggers the same backfill ---------
urls.length = 0;
state.events = [row(150, 910)];
state.eventIds = new Set([910]);
mergeEvents([row(153, 913)]); // 151 and 152 never arrived
await new Promise((resolve) => setImmediate(resolve));
assert.ok(
  urls.some((url) => url.includes('/events?after=150')),
  `a skipped project_seq must be pulled in, saw ${JSON.stringify(urls)}`,
);

// the feed keeps what it already has while catching up
state.events = [row(160, 920)];
state.eventIds = new Set([920]);
mergeEvents([row(161, 921)]);
await new Promise((resolve) => setImmediate(resolve));
assert.equal(state.events.length, 2, 'a contiguous arrival needs no backfill');
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function mergeEvents(")
    end = javascript.index("function projectGroupKey(", start)
    harness = tmp_path / "stream_resume.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n(async () => {" + BODY + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_room_feed_recovers_every_event_missed_during_a_blip(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_watermark_uses_the_streams_own_number_space():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    block = javascript[
        javascript.index("function streamWatermark() {")
        : javascript.index("function projectGroupKey(")
    ]
    assert "event.project_seq" in block
    assert "lastAppliedEventCursor" not in block
    assert "catchUpStream" in block
