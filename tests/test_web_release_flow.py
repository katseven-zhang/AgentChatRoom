from __future__ import annotations

import json
import subprocess
from pathlib import Path


WEB_DIR = Path(__file__).parents[1] / "src" / "agentchatroom" / "web"


def test_web_release_flow_payload_visibility_and_stage_gating(tmp_path):
    """Regression for the #43 second review round: the Web release flow
    posts a structured reason to the release endpoint, the button is
    stage-gated by the extracted taskReleaseVisible rule, and a missing
    reason is rejected before any request — the real listener code runs
    against a stubbed DOM/api."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    start = javascript.index("function taskReleaseVisible(task)")
    end = javascript.index("function clearDialogDrafts(dialog)", start)
    visible_fn = javascript[start:end]

    listener_start = javascript.index(
        'elements["task-release-button"].addEventListener'
    )
    listener_end = javascript.index(
        'elements["task-release-form"].addEventListener("submit"'
    )
    form_end = javascript.index("\n});", listener_end) + len("\n});")
    wiring = javascript[listener_start:form_end]

    prelude = (
        "const state = { editingTaskId: 'task_x', projectId: 'project_x',"
        " snapshot: { tasks: [{ id: 'task_x', task_number: 43,"
        " execution_status: 'in_progress' }] } };\n"
        "const calls = [];\n"
        "function api(path, options) { calls.push({ path, options });"
        " return Promise.resolve({}); }\n"
        "const renderCalls = [];\n"
        "function showToast(message, type) { calls.push({ toast: message }); }\n"
        "function handleError(error) { calls.push({ error: String(error) }); }\n"
        "function refreshTaskIntakeData() {\n"
        "  state.snapshot.tasks[0].execution_status = 'todo';\n"
        "  return Promise.resolve();\n"
        "}\n"
        "function renderTaskContract(task) {\n"
        "  renderCalls.push(['contract', task.execution_status]);\n"
        "  elements['task-release-button'].classList.toggle('is-hidden', !taskReleaseVisible(task));\n"
        "}\n"
        "function renderTaskAssignments(task) { renderCalls.push(['assignments', task.execution_status]); }\n"
        "function renderTaskTimeline(task) { renderCalls.push(['timeline', task.execution_status]); }\n"
        "function escapeHtml(value) { return String(value ?? ''); }\n"
        "const cache = {};\n"
        "function makeEl(id) {\n"
        "  const el = {\n"
        "    id, open: false, value: '', disabled: false, textContent: '',\n"
        "    innerHTML: '', _hidden: null,\n"
        "    addEventListener(type, fn) { cache[id + ':' + type] = fn; },\n"
        "    close() { this.open = false; },\n"
        "    showModal() { this.open = true; },\n"
        "  };\n"
        "  el.classList = { toggle(_c, v) { el._hidden = v; } };\n"
        "  return el;\n"
        "}\n"
        "const elements = new Proxy({}, { get(_, id) {"
        " return cache[String(id)] || (cache[String(id)] = makeEl(String(id))); } });\n"
        "const document = { getElementById: (id) => elements[id] };\n"
    )

    scenario = (
        "const outcomes = {};\n"
        "(async () => {\n"
        "  outcomes.showsForClaimed = taskReleaseVisible({ execution_status: 'claimed' });\n"
        "  outcomes.showsForInProgress = taskReleaseVisible({ execution_status: 'in_progress' });\n"
        "  outcomes.showsForBlocked = taskReleaseVisible({ execution_status: 'blocked' });\n"
        "  outcomes.hidesForAwaiting = !taskReleaseVisible({ execution_status: 'completed' });\n"
        "  outcomes.hidesForDone = !taskReleaseVisible({ execution_status: 'done' });\n"
        "  outcomes.hidesForCancelled = !taskReleaseVisible({ execution_status: 'cancelled' });\n"
        "\n"
        "  await cache['task-release-button:click']();\n"
        "  cache['task-release-reason'].value = 'quota_exhausted';\n"
        "  cache['task-release-reason-text'].value = '  because  ';\n"
        "  await cache['task-release-form:submit']({ preventDefault() {} });\n"
        "  outcomes.dialogClosedAfterSubmit = !cache['task-release-dialog'].open;\n"
        "  const post = calls.find((c) => c.path && c.path.endsWith('/tasks/task_x/release'));\n"
        "  outcomes.postsToReleaseEndpoint = Boolean(post);\n"
        "  const body = post ? JSON.parse(post.options.body) : {};\n"
        "  outcomes.structuredBody = body.reason_code === 'quota_exhausted' && body.reason === 'because';\n"
        "\n"
        "  const before = calls.length;\n"
        "  await cache['task-release-button:click']();\n"
        "  outcomes.reopenResetsReason = cache['task-release-reason'].value === '';\n"
        "  await cache['task-release-form:submit']({ preventDefault() {} });\n"
        "  outcomes.missingReasonBlocked = !calls.slice(before).some((c) => c.path);\n"
        "  outcomes.toastHint = calls.slice(before).some((c) => c.toast === '请选择释放原因');\n"
        "  outcomes.detailRefreshedFromSnapshot = renderCalls.some((c) => c[0] === 'contract' && c[1] === 'todo');\n"
        "  outcomes.buttonRehiddenAfterRefresh = cache['task-release-button']._hidden === true;\n"
        "\n"
        "  console.log(JSON.stringify(outcomes));\n"
        "})();\n"
    )

    harness = tmp_path / "release_flow_harness.js"
    harness.write_text(prelude + visible_fn + "\n" + wiring + "\n" + scenario, encoding="utf-8")
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "showsForClaimed": True,
        "showsForInProgress": True,
        "showsForBlocked": True,
        "hidesForAwaiting": True,
        "hidesForDone": True,
        "hidesForCancelled": True,
        "dialogClosedAfterSubmit": True,
        "postsToReleaseEndpoint": True,
        "structuredBody": True,
        "reopenResetsReason": True,
        "missingReasonBlocked": True,
        "toastHint": True,
        "detailRefreshedFromSnapshot": True,
        "buttonRehiddenAfterRefresh": True,
    }
