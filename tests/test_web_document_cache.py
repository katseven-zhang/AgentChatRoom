"""#318: the document body cache is per (project, doc_key, version).

The web document viewer kept body text in one global map keyed only by
``doc_key`` and never cleared it on project switch, so project B could render
project A's document; a failed auto-load was swallowed by ``.catch(() => {})``
and left the panel in "正在加载文档…" forever while every re-render fired the
same doomed request again.

These tests slice the real document functions out of ``app.js`` and run them
under node with a minimal DOM/`api` stub, so the assertions are about behaviour
(cache keys, late responses, failure state) rather than about source strings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "agentchatroom" / "web"

PREAMBLE = """
const assert = require('node:assert/strict');

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
function formatTime(value) {
  return String(value || '');
}

const state = {projectId: 'project-a', snapshot: null, documentContentCache: {}};

class FakeNode {
  constructor(tag, docContent) {
    this.tagName = tag;
    this.className = '';
    this.textContent = '';
    this.innerHTML = '';
    this.dataset = docContent === undefined ? {} : {docContent};
    this.replacedBy = null;
    this.classList = {contains: (name) => this.className.split(/\\s+/).includes(name)};
  }
  replaceWith(next) {
    this.replacedBy = next;
    const index = documentList.nodes.indexOf(this);
    if (index >= 0) documentList.nodes[index] = next;
  }
  closest() {
    return null;
  }
}

const documentList = {
  innerHTML: '',
  nodes: [],
  querySelector(selector) {
    const wanted = /\\[data-doc-content="([^"]*)"\\]/.exec(selector);
    if (!wanted) return null;
    return this.nodes.find((node) => node.dataset.docContent === wanted[1] && !node.replacedBy) || null;
  },
};

const elements = {'document-list': documentList};
const document = {
  createElement: (tag) => new FakeNode(tag),
};

let apiCalls = [];
let apiBehaviour = () => Promise.resolve({document: {}});
function api(path) {
  apiCalls.push(path);
  return apiBehaviour(path);
}

function resetDom() {
  documentList.innerHTML = '';
  documentList.nodes = [];
}

function seedLoadingNode(docKey) {
  const node = new FakeNode('div', docKey);
  node.className = 'loading-state';
  documentList.nodes.push(node);
  return node;
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function cacheKeys() {
  return Object.keys(state.documentContentCache).sort();
}

const doc = {doc_key: 'SPEC', title: 'Spec', kind: 'reference', version: 1, size: 4};
"""

BODY = r"""
// --- A -> B must never render A's body -----------------------------------
resetDom();
state.projectId = 'project-a';
state.snapshot = {documents: [doc]};
state.documentContentCache = {};
apiCalls = [];
apiBehaviour = (path) =>
  Promise.resolve({document: {doc_key: 'SPEC', version: 1, content: 'A-body', history: []}});
renderDocuments();
seedLoadingNode('SPEC');
await flush();
assert.equal(state.documentContentCache[Object.keys(state.documentContentCache)[0]].content, 'A-body');
assert.ok(documentBodyHtml(doc).includes('A-body'), 'project A shows its own body');

resetDom();
state.projectId = 'project-b';
apiCalls = [];
apiBehaviour = (path) =>
  Promise.resolve({document: {doc_key: 'SPEC', version: 1, content: 'B-body', history: []}});
const beforeSwitch = documentBodyHtml(doc);
assert.ok(!beforeSwitch.includes('A-body'), 'project B never shows project A body');
assert.ok(beforeSwitch.includes('正在加载文档'), 'project B starts from the loading state');
renderDocuments();
assert.equal(apiCalls.length, 1, 'project B re-fetches the same doc_key');
assert.ok(apiCalls[0].includes('project-b'), 'the re-fetch is scoped to the current project');

// --- a late response must not leak into the project the user left --------
resetDom();
state.projectId = 'project-a';
state.documentContentCache = {};
apiCalls = [];
let release;
apiBehaviour = () => new Promise((resolve) => { release = resolve; });
renderDocuments();
const pending = refreshDocumentBody(doc);
state.projectId = 'project-c';
release({document: {doc_key: 'SPEC', version: 1, content: 'A-late', history: []}});
await pending;
assert.equal(cacheKeys().length, 0, 'a response that arrives after the switch is dropped');
assert.ok(!documentBodyHtml({doc_key: 'SPEC', version: 1}).includes('A-late'));

// --- a failed auto-load is an error state, not permanent loading ---------
resetDom();
state.projectId = 'project-a';
state.documentContentCache = {};
apiCalls = [];
apiBehaviour = () => Promise.reject(new Error('document gone'));
renderDocuments();
const loadingNode = seedLoadingNode('SPEC');
await flush();
assert.ok(loadingNode.replacedBy, 'failure replaces the loading placeholder');
assert.equal(loadingNode.replacedBy.className, 'state-box error-state');
assert.ok(loadingNode.replacedBy.innerHTML.includes('data-doc-retry="SPEC"'), 'retry stays available');
assert.ok(documentBodyHtml(doc).includes('文档加载失败'), 're-render shows the error state');
assert.ok(!documentBodyHtml(doc).includes('正在加载文档'), 'never a permanent spinner');
renderDocuments();
await flush();
assert.equal(apiCalls.length, 1, 'the cached failure stops the request loop');

// --- the failure record is keyed by version ------------------------------
resetDom();
state.snapshot = {documents: [{...doc, version: 2}]};
apiCalls = [];
apiBehaviour = () =>
  Promise.resolve({document: {doc_key: 'SPEC', version: 2, content: 'A-v2', history: []}});
renderDocuments();
await flush();
assert.ok(documentBodyHtml({...doc, version: 2}).includes('A-v2'), 'a new version is fetched');
"""


def _harness(tmp_path: Path) -> Path:
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    anchors = [
        index
        for index in (
            javascript.find("function documentCacheKey("),
            javascript.find("function documentCacheEntry("),
        )
        if index >= 0
    ]
    start = min(anchors)
    end = javascript.index("function wireDocumentList(")
    harness = tmp_path / "document_cache.js"
    harness.write_text(
        PREAMBLE
        + "\n"
        + javascript[start:end]
        + "\n(async () => {"
        + BODY
        + "})();\n",
        encoding="utf-8",
    )
    return harness


def test_document_body_cache_is_scoped_to_project_and_version(tmp_path):
    run = subprocess.run(
        ["node", str(_harness(tmp_path))], capture_output=True, text=True, encoding="utf-8"
    )
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"


def test_project_switch_and_empty_room_drop_document_bodies():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    select_project = javascript[
        javascript.index("async function selectProject(") : javascript.index(
            "function renderProjects("
        )
    ]
    empty_room = javascript[
        javascript.index("function renderEmptyRoom()") : javascript.index("\n}\n", javascript.index("function renderEmptyRoom()"))
        + 3
    ]
    assert "state.documentContentCache = {};" in select_project
    assert "state.documentContentCache = {};" in empty_room


def test_document_body_cache_key_carries_the_project_id():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "function documentCacheKey(projectId, docKey)" in javascript
    assert "${projectId}|${docKey}" in javascript
    assert "state.documentContentCache[doc.doc_key] = loaded;" not in javascript
    assert "refreshDocumentBody(doc).catch(() => {});" not in javascript
    entry = javascript[
        javascript.index("function documentCacheEntry(") : javascript.index("\n}\n", javascript.index("function documentCacheEntry("))
        + 3
    ]
    assert "documentCacheEntry(projectId, docKey, version)" in entry
    assert entry.count("documentCacheKey(projectId, docKey)") == 1
