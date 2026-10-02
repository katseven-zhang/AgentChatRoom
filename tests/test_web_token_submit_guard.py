"""#321 and #364: token submit is single-flight and will not drop a missing member."""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "agentchatroom" / "web"

PREAMBLE = r"""
const listeners = [];
const submitButton = { disabled: false };
const memberSelect = { innerHTML: '', value: '' };
const elements = {
  'token-form': { querySelector: () => submitButton, addEventListener: (type, fn) => listeners.push(fn) },
  'token-member': memberSelect,
  'token-permissions': { querySelectorAll: () => [{ value: 'room:read' }] },
  'token-days': { value: '30' },
  'token-dialog': { open: true, close() { this.open = false; } },
  'token-agent-name': { value: '' },
};
const state = {
  projectId: 'project-1',
  pendingHttpSetup: null,
  tokenSubmitInFlight: false,
  members: [],
  credentials: [],
};
const errors = [];
function setTokenFormError(message) { errors.push(message || ''); }
function deriveTokenCredentialName() { return 'Agent 凭据'; }
function escapeHtml(value) { return String(value ?? ''); }
let apiCalls = 0;
let releaseApi;
function api() {
  apiCalls += 1;
  return new Promise((resolve) => { releaseApi = resolve; });
}
function refreshManagement() { return Promise.resolve(); }
function showTokenSecret() {}
function showIssuedHttpResult() {}
function showToast() {}
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    options = javascript[
        javascript.index("function renderTokenMemberOptions(") : javascript.index(
            "function syncTokenAgentNameVisibility("
        )
    ]
    listener = javascript[
        javascript.index('elements["token-form"].addEventListener("submit"') : javascript.index(
            'elements["task-assign-form"].addEventListener'
        )
    ]
    body = r"""
renderTokenMemberOptions('member-missing');
if (memberSelect.value !== 'member-missing') {
  throw new Error('missing member selection was dropped: ' + memberSelect.value);
}
if (!memberSelect.innerHTML.includes('member-missing')) {
  throw new Error('missing member option was not kept');
}
const event = { preventDefault() {}, submitter: submitButton };
const first = listeners[0](event);
const second = listeners[0](event);
if (apiCalls !== 0) throw new Error('missing member must not issue a credential, calls=' + apiCalls);
if (!errors.some((item) => item.includes('不会签发不关联凭据'))) {
  throw new Error('missing member was not explained: ' + JSON.stringify(errors));
}
memberSelect.value = '';
errors.length = 0;
const issued = listeners[0](event);
const duplicate = listeners[0](event);
if (apiCalls !== 1) throw new Error('two submits issued ' + apiCalls + ' credentials');
releaseApi({ token: 'secret-once' });
await issued;
await duplicate;
await first;
await second;
"""
    harness = tmp_path / "token_submit_guard.js"
    harness.write_text(
        PREAMBLE + "\n" + options + "\n" + listener + "\n(async () => {\n" + body + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_token_submit_keeps_a_missing_member_and_issues_once(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
