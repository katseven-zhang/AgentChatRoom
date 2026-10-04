"""#327: feed event ids and history project_seq must not share expand state."""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = r"""
const assert = require('node:assert/strict');
const state = { expandedEvents: new Set() };
"""

BODY = r"""
rememberExpandedEvent('feed', 5);
assert.equal(eventIsExpanded('feed', 5), true);
assert.equal(eventIsExpanded('seq', 5), false, 'a history row with the same number stays collapsed');
rememberExpandedEvent('seq', 5);
assert.equal(eventIsExpanded('seq', 5), true);
assert.equal(eventIsExpanded('feed', 5), true, 'expanding history does not collapse the feed');
for (let index = 0; index < 90; index += 1) rememberExpandedEvent('feed', 1000 + index);
const feedKeys = [...state.expandedEvents].filter((item) => String(item).startsWith('feed:'));
const seqKeys = [...state.expandedEvents].filter((item) => String(item).startsWith('seq:'));
assert.ok(feedKeys.length <= 80, 'the feed cap does not count history rows');
assert.deepEqual(seqKeys, ['seq:5']);
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function expansionKey(")
    end = javascript.index("function resetProjectFilters(", start)
    harness = tmp_path / "expanded_events.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n" + BODY,
        encoding="utf-8",
    )
    return harness


def test_feed_and_history_expansion_do_not_share_a_number(tmp_path):
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    feed = javascript[
        javascript.index("function renderMessageBody(")
        : javascript.index("function showToast(", javascript.index("function renderMessageBody("))
    ]
    history = javascript[
        javascript.index("function renderHistoryEvidence(")
        : javascript.index("function renderHistoryEvidence(") + 800
    ]
    assert 'eventIsExpanded("feed"' in feed
    assert 'eventIsExpanded("seq"' in history
    run = subprocess.run(
        ["node", str(_harness(tmp_path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
