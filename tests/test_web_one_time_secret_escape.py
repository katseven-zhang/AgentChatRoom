"""#321 (second clause): Escape must not destroy a one-time Agent Token.

The issued secret is only ever returned once, and ``token-secret-dialog``'s
``close`` handler wipes ``token-secret-value``. A native ``<dialog>`` fires
``cancel`` on Escape and then ``close``, so pressing Escape discarded a
credential the server will never show again — the user had to issue a second
token to get one they could configure. The cancel event now needs an explicit
preventDefault so 「我已保存」 stays the only way out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = """
const assert = require('node:assert/strict');

const state = {issuedHttpSetup: {token: 'issued'}};
const listeners = {};
function dialogElement() {
  const handlers = {};
  return {
    addEventListener: (name, handler) => {handlers[name] = handler;},
    close() {
      this.closed = true;
      if (handlers.close) handlers.close({});
    },
    fire(name, event) {
      if (handlers[name]) {
        handlers[name](event);
        return true;
      }
      return false;
    },
    has(name) {
      return Boolean(handlers[name]);
    },
  };
}

const secretDialog = dialogElement();
const elements = {
  'token-secret-dialog': secretDialog,
  'token-secret-close': {addEventListener: (name, handler) => {listeners.close = handler;}},
  'token-secret-value': {textContent: 'acr.credential_1234567890'},
  'token-secret-section': {hidden: false},
  'token-secret-copy': {hidden: false},
  'token-secret-context': {textContent: ''},
  'token-secret-title': {textContent: ''},
  'token-config-value': {textContent: 'x'},
  'token-config-path': {textContent: 'x'},
  'token-config-projects': {textContent: 'x'},
  'token-config-section': {hidden: false},
  'token-dialog': {addEventListener() {}},
};
let toasts = [];
function showToast(message) {
  toasts.push(message);
}
"""

BODY = r"""
// Escape fires cancel and then close on a native dialog.
assert.ok(
  secretDialog.has('cancel'),
  'the one-time secret dialog must listen for cancel so Escape cannot close it',
);
const event = {preventDefault() {this.defaultPrevented = true;}};
secretDialog.fire('cancel', event);
assert.equal(event.defaultPrevented, true, 'cancel must be prevented, otherwise the dialog closes');
assert.ok(toasts.length, 'the user is told why Escape did nothing');
assert.equal(
  elements['token-secret-value'].textContent,
  'acr.credential_1234567890',
  'the secret is still on screen after an Escape attempt',
);

// The explicit acknowledgement button still closes and wipes it.
listeners.close({});
assert.equal(secretDialog.closed, true, 'the close button still dismisses the dialog');
assert.equal(elements['token-secret-value'].textContent, '', 'and clears the secret deliberately');
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index('elements["token-secret-close"].addEventListener(')
    end = javascript.index('elements["token-dialog"].addEventListener(', start)
    harness = tmp_path / "secret_escape.js"
    harness.write_text(
        PREAMBLE + "\n" + javascript[start:end] + "\n(async () => {" + BODY + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_escape_cannot_discard_a_one_time_token_secret(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_secret_dialog_still_has_an_explicit_exit():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    block = javascript[
        javascript.index('elements["token-secret-close"].addEventListener(')
        : javascript.index('elements["token-dialog"].addEventListener(')
    ]
    assert 'addEventListener("cancel"' in block
    assert "event.preventDefault()" in block
    assert 'elements["token-secret-value"].textContent = "";' in block
