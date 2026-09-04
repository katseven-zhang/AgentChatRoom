from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


WEB_DIR = Path(__file__).parents[1] / "src" / "agentchatroom" / "web"


def test_registered_web_elements_exist_in_markup():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    registry = re.search(
        r"const elements = Object\.fromEntries\(\s*\[(.*?)\]\.map",
        javascript,
        re.DOTALL,
    )

    assert registry is not None
    element_ids = re.findall(r'"([a-z0-9-]+)"', registry.group(1))
    assert element_ids
    assert len(element_ids) == len(set(element_ids))
    assert [element_id for element_id in element_ids if f'id="{element_id}"' not in markup] == []


def test_web_declares_a_static_favicon():
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert 'rel="icon"' in markup
    assert (WEB_DIR / "favicon.svg").is_file()


def test_web_bootstrap_and_phase_one_local_agent_hooks_are_complete():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    assert "async function loadAuthenticatedApp()" in javascript
    assert 'api("/api/v1/auth/status")' in javascript
    assert 'id="login-username"' not in markup
    assert 'autocomplete="current-password"' in markup
    assert "function renderIntegrationJoin()" in javascript
    assert 'data-integration-transport=' not in markup
    assert '"streamable_http_config_text"' not in javascript
    assert '"remote_bridge_config_text"' not in javascript
    assert 'payload.host_key = "<stable-host-key>"' not in javascript
    assert 'payload.host_name = "<computer-name>"' not in javascript
    assert '"<path-to-project-on-this-computer>"' not in javascript
    assert "function renderProjectInstructions()" in javascript
    assert 'localStorage.getItem("agentchatroom.projectKey")' not in javascript
    assert 'localStorage.setItem("agentchatroom.projectKey"' not in javascript
    assert 'id="integration-project-rules-code"' in markup
    assert 'model: "<actual model or unknown>"' in javascript
    assert "function messageModelBadge(event)" in javascript
    assert "模型未上报" in javascript
    assert "event.payload?.model_display_name" in javascript
    assert "agent.model" not in javascript
    assert "上报模型" not in javascript
    assert ".model-badge" in (WEB_DIR / "app.css").read_text(encoding="utf-8")
    assert "function currentAgentRoster(agentIdentities)" in javascript
    assert "agent.member_status !== \"revoked\"" in javascript
    assert "currentAgentRoster(snapshot.agent_identities)" in javascript
    assert "可选择所有已接入且未吊销的 Agent" in javascript
    assert "当前未连接的 Agent 会在重新接入后受理任务" in javascript
    assert "target.connection_status === \"connected\" ? \"已连接\" : \"未连接\"" in javascript
    assert "function connectedAgentCount(agentIdentities)" in javascript


def test_web_styles_cover_versioned_task_and_assignment_states():
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    for status in (
        "pending",
        "approved",
        "accepted",
        "completed",
        "changes_requested",
        "declined",
        "failed",
    ):
        assert f".status-badge.{status}" in stylesheet


def test_web_renders_explicit_no_code_work_evidence():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'report.no_code_change_reason || "未说明原因"' in javascript
    assert "无代码变更" in javascript


def test_web_renders_state_view_from_server_config_without_hardcoded_buckets():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    # The shared projection is the single source of truth in the front end.
    assert "function taskView(task)" in javascript
    assert "function taskNotFinished(task)" in javascript
    assert "task?.state_view" in javascript
    # The retired hard-coded bucket/status helpers must be gone.
    for retired in (
        "function bucketOf(",
        "function bucketLabel(",
        "function taskStatus(",
        "function taskDisplayLabel(",
        "function executionStatus(",
        "function verificationStatus(",
        "function integrationStatus(",
    ):
        assert retired not in javascript
    assert "legacy_status" not in javascript
    # Navigation entries and expert filters are rendered from server config.
    assert 'id="task-navigation"' in markup
    assert 'id="task-expert-filters"' in markup
    assert "data-filter" not in markup
    assert 'data-task-entry="${escapeHtml(entry.key)}"' in javascript
    assert "state.config?.domain?.task_view" in javascript
    # Cards show task number and priority side by side, plus view badges.
    assert "P${task.priority}" in javascript
    assert "#${task.task_number}" in javascript
    assert "function taskViewBadgeHtml(view)" in javascript
    assert 'class="status-badge aux' in javascript
    # Terminal cancelled tasks are never treated as integration candidates.
    assert '["done", "cancelled"].includes(group)' in javascript
    # The three anomaly phases carry visible emphasis styles.
    for phase in ("changes_requested", "integration_failed", "unclassified"):
        assert f".status-badge.{phase}" in stylesheet


def test_web_task_list_renders_task_number_and_priority_together():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    render_table = javascript[
        javascript.index("function renderTaskTable(tasks)") : javascript.index(
            "function intakeTargetName",
            javascript.index("function renderTaskTable(tasks)"),
        )
    ]

    assert '<span class="task-number">#${task.task_number}</span>' in render_table
    assert '<span class="priority p${task.priority}">P${task.priority}</span>' in render_table


def test_web_supports_human_reading_and_guided_interactions():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert "function renderMessageBody(" in javascript
    assert "function renderMessageLines(" in javascript
    assert "data-expand-event" in javascript
    assert "data-collapse-event" in javascript
    assert 'event.key === "Enter"' in javascript
    assert "requestSubmit()" in javascript
    assert "function projectGroupKey(" in javascript
    assert "data-group-key" in javascript
    assert 'id="onboarding"' in markup
    assert 'id="new-message-notice"' in markup
    assert "composer-advanced" in markup
    assert ".msg-heading" in stylesheet
    assert ".project-group-header" in stylesheet
    assert ".new-message-notice" in stylesheet
    assert ".agent-item.is-disconnected" in stylesheet
    assert "snapshot.agent_identities" in javascript
    assert "当前连接" in javascript
    assert "累计" in javascript and "次接入" in javascript
    assert 'app.css?v=1.0.0-central27' in markup
    assert 'app.js?v=1.0.0-central27' in markup
    assert 'id="task-history-filter"' in markup
    assert "function loadTaskHistory(" in javascript
    assert "function renderHistoryEvidence(" in javascript
    assert "data-copy-event" in javascript
    assert "data-open-event" in javascript
    assert "function renderIntegrationTabs()" in javascript
    assert 'class="segmented-control integration-tabs" id="integration-format-tabs"' in markup
    assert "state.integration.profiles" in javascript
    assert 'id="integration-onboarding-prompt"' in markup
    assert "让 Agent 配置 MCP" in markup
    assert "复制当前环境的 MCP 连接参数，由 Agent 自行完成接入" in markup
    assert "受权限限制时返回可直接粘贴的配置文本" not in markup
    assert "复制配置指令" in markup
    assert "这段提示词包含当前项目" not in markup
    assert "function renderOnboardingPrompt()" in javascript
    assert "onboarding_prompts" in javascript
    assert 'class="integration-fallback integration-advanced"' in markup
    assert "高级手动配置" in markup
    assert "function eventIdBadge(eventId)" in javascript
    assert "eventIdBadge(event.id)" in javascript
    assert javascript.count("eventIdBadge(event.id)") >= 4
    assert markup.count('class="tab-status">未闭环</span>') == 3
    assert 'class="tab-status">协作视图</span>' in markup
    assert markup.count('class="scope-status"><span>未闭环</span>') == 3
    assert ".event-id" in stylesheet
    assert ".scope-status" in stylesheet
    assert ".integration-chooser {\n  display: grid;\n  grid-template-columns: 1fr;" in stylesheet
    assert ".workspace-actions > button" in stylesheet
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in stylesheet
    assert "#connect-agent-button," in stylesheet
    assert ".agent-list {" in stylesheet
    assert "grid-template-columns: 1fr;" in stylesheet


def test_web_event_and_audit_panels_catch_up_to_latest_cursor():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert "function loadEventWindow(" in javascript
    assert "function loadRecentEvents(" in javascript
    assert "function loadRecentAuditEvents(" in javascript
    assert "loadRecentEvents(projectId)" in javascript
    assert "loadRecentAuditEvents(state.projectId, eventType)" in javascript
    assert "connectEvents(eventPage.cursor)" in javascript
    assert "audit.events.slice(-AUDIT_WINDOW_SIZE)" in javascript
    assert "const EVENT_WINDOW_SIZE = 500;" in javascript
    assert "const AUDIT_WINDOW_SIZE = 100;" in javascript
    assert "result.events.length < windowSize || after >= latest" in javascript
    assert "result.latest_cursor" in javascript
    assert "tailJump && latest - after > windowSize" in javascript
    assert "{ tailJump: !eventType }" in javascript
    assert "events?after=0&limit=500" not in javascript
    assert "audit?after=0&limit=100" not in javascript
    assert "connectEvents(snapshot.cursor)" not in javascript


def test_web_project_creation_uses_the_real_folder_and_local_picker():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert 'id="project-folder-picker-button" type="button" class="secondary-button" disabled' in markup
    assert 'id="project-logical-path-input"' not in markup
    assert "project-logical-path-input" not in javascript
    assert 'api("/api/v1/local/folders/pick"' in javascript
    assert "state.config.capabilities?.local_folder_picker" in javascript
    assert "无法打开系统文件夹选择器，请手动输入项目路径" in javascript
    assert "logical_path:" not in javascript[javascript.index('elements["project-form"]'):]
    assert ".path-picker-control" in stylesheet


def test_web_local_mcp_assistant_separates_write_reload_and_presence_states():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert 'id="integration-local-assistant"' in markup
    assert '>配置本机 Agent</button>' in markup
    assert '<h2>配置本机 Agent</h2>' in markup
    assert 'data-integration-transport=' not in markup
    assert '连接方式' not in markup
    assert 'id="integration-local-refresh"' in markup
    assert 'id="integration-local-apply"' in markup
    assert "function renderLocalMcpPlan()" in javascript
    assert "function refreshLocalMcpPlan(" in javascript
    assert "function applyLocalMcpPlan()" in javascript
    assert "/integrations/mcp/local/${encodeURIComponent(profileId)}/plan" in javascript
    assert "/integrations/mcp/local/${encodeURIComponent(profileId)}/apply" in javascript
    assert "只新增或更新 mcpServers.agentchatroom" in javascript
    assert "配置存在不代表客户端当前已经连接" in javascript
    assert "当前 Room 尚未连接" in javascript
    assert "Presence 不是当前模型对话同步" in javascript
    assert "浏览器无法观察" in javascript
    assert 'id="integration-local-facts"' in markup
    assert "function renderLocalMcpFacts(" in javascript
    assert "软件配置" in javascript
    assert "进程连接" in javascript
    assert "Room Session" in javascript
    assert "当前对话同步" in javascript
    assert "room_bootstrap" in javascript
    assert "不会自动提权" in javascript
    assert ".local-mcp-assistant" in stylesheet
    assert ".local-mcp-facts" in stylesheet
    assert '.local-mcp-fact[data-state="ready"]' in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in stylesheet
    assert ".integration-tabs button:last-child" in stylesheet
    assert ".integration-transport-tabs" not in stylesheet


def test_web_desktop_panels_are_resizable_readable_and_persistent():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert markup.count('role="separator"') == 2
    assert 'id="left-panel-resizer"' in markup
    assert 'id="right-panel-resizer"' in markup
    assert markup.count('aria-orientation="vertical"') == 2
    assert "function initializePanelLayout()" in javascript
    assert "agentchatroom.layout.leftPanelWidth" in javascript
    assert "agentchatroom.layout.rightPanelWidth" in javascript
    assert "setPointerCapture" in javascript
    assert 'addEventListener("dblclick"' in javascript
    assert 'event.key === "ArrowLeft"' in javascript
    assert "--left-panel-default-width: 288px;" in stylesheet
    assert "--right-panel-default-width: 480px;" in stylesheet
    assert "--workspace-min-width: 480px;" in stylesheet
    assert ".panel-resizer {" in stylesheet
    assert '"side left-resizer work right-resizer chat"' in stylesheet
    assert "grid-template-columns: 232px minmax(500px, 1fr) 360px;" not in stylesheet


def test_web_sse_and_project_switch_guard_stale_snapshots():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    assert "function refreshSnapshot(projectId)" in javascript
    assert "JSON.parse(message.data)" in javascript
    assert "Ignored malformed room event" in javascript
    assert "applySnapshotIfCurrent" in javascript
    assert "function renderForEvent(event)" in javascript
    assert "state.streamHadError" in javascript
    assert "resetProjectFilters()" in javascript
    assert "clearDialogDrafts(dialog)" in javascript
    assert 'aria-live="polite"' in markup
    assert 'role="tablist"' in markup
    assert 'role="tabpanel"' in markup
    assert "function withBusy(work)" in javascript
    assert 'document.body.setAttribute("aria-busy", "true")' in javascript
    assert "function activateTab(button)" in javascript
    assert 'event.key === "ArrowRight"' in javascript
    assert "alreadyKnown && previousId" in javascript
    assert "streamVisible" in javascript
    assert "profile.software_key" in javascript
    assert 'replaceAll("_", "-")' not in javascript
    assert "--danger:" in stylesheet
    assert 'maxlength="20000"' in markup
    assert "rememberExpandedEvent" in javascript


def test_web_task_view_harness_covers_state_triple_samples(tmp_path):
    """Node harness proving the browser renders the shared state.view codes.

    The front end never re-derives phases: samples carry the exact
    ``state_view`` payload produced by the Python projection, so this test
    locks the server projection -> server config labels -> Web rendering
    chain end to end. It runs with an explicit UTF-8 decoding contract for
    the child process so the default Windows GBK locale cannot break the
    regression.
    """
    from agentchatroom.contracts import task_view

    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # Rendering helper functions that read the projection and server config.
    start_a = javascript.index("function taskViewConfig()")
    end_marker_a = "function taskNeedsIntegration(task) {\n  return taskView(task).execution_status !== \"cancelled\";\n}\n"
    end_a = javascript.index(end_marker_a) + len(end_marker_a)
    # Pure navigation/filter/sort logic used by the tasks panel.
    start_b = javascript.index("function taskNavigationEntries(tasks)")
    end_b = javascript.index("function renderTaskNavigation(tasks)")
    # Expert filter rendering (exact-state options must carry counts).
    start_c = javascript.index("function renderTaskExpertFilters(tasks)")
    end_c = javascript.index("function renderTasks(tasks)")

    config = {
        "schema_version": 2,
        "phases": [
            "todo", "claimed", "in_progress", "blocked", "awaiting_review",
            "changes_requested", "pending_integration", "integration_failed",
            "done", "cancelled", "unclassified",
        ],
        "phase_labels": {
            "todo": "待认领", "claimed": "已认领", "in_progress": "执行中",
            "blocked": "阻塞", "awaiting_review": "待验收",
            "changes_requested": "已退回", "pending_integration": "待集成",
            "integration_failed": "集成失败", "done": "已完成",
            "cancelled": "已取消", "unclassified": "未归类",
        },
        "group_labels": {
            "claimable": "待认领", "active": "进行中", "review": "待验收",
            "integration": "待集成", "done": "已完成", "cancelled": "已取消",
            "unclassified": "未归类",
        },
        "attention_phases": ["blocked", "changes_requested", "integration_failed"],
        "attention_label": "需要处理",
        "active_subgroup_order": ["changes_requested", "blocked", "in_progress", "claimed"],
    }

    sample_triples = [
        ("todo", "todo", "not_required", "pending"),
        ("claimed", "claimed", "not_required", "pending"),
        ("in_progress", "in_progress", "not_required", "pending"),
        ("blocked", "blocked", "not_required", "pending"),
        ("awaiting_review", "completed", "pending", "pending"),
        ("returned", "in_progress", "changes_requested", "pending"),
        ("pending_integration", "completed", "approved", "pending"),
        ("integration_failed", "completed", "approved", "failed"),
        ("done", "completed", "approved", "done"),
        ("cancelled", "cancelled", "not_required", "pending"),
        ("released_returned", "todo", "changes_requested", "pending"),
        ("verified_without_review", "completed", "not_required", "pending"),
        ("residue", "todo", "approved", "pending"),
    ]
    # Each sample embeds the exact projection payload the server would send.
    samples = []
    expected_mapping = {}
    for name, execution, verification, integration in sample_triples:
        view = task_view(
            execution_status=execution,
            verification_status=verification,
            integration_status=integration,
        )
        samples.append(
            {
                "name": name,
                "id": f"task-{len(samples) + 1:04d}",
                "task_number": len(samples) + 1,
                "priority": 1,
                "execution_status": execution,
                "verification_status": verification,
                "integration_status": integration,
                "state_view": view,
            }
        )
        expected_mapping[name] = {
            "phase": view["phase"],
            "group": view["group"],
            "label": config["phase_labels"][view["phase"]],
            "needs_attention": view["needs_attention"],
        }

    harness = tmp_path / "task_view_harness.js"
    harness.write_text(
        "const state = { config: { domain: { task_view: "
        + json.dumps(config, ensure_ascii=False)
        + " } }, taskExpert: { execution: '', verification: '', integration: '', priority: '', owner: '', number: '', phase: '' }, snapshot: { agents: [] } };\n"
        "function escapeHtml(value) { return String(value ?? ''); }\n"
        "function shortId(value) { return String(value); }\n"
        "const captured = { expert: '' };\n"
        "const document = { getElementById: (id) => ({ set innerHTML(value) { captured[id === 'task-expert-filters' ? 'expert' : id] = value; }, get innerHTML() { return captured[id] || ''; } }) };\n"
        + javascript[start_a:end_a]
        + "\n"
        + javascript[start_b:end_b]
        + "\n"
        + javascript[start_c:end_c]
        + "\nconst samples = "
        + json.dumps(samples, ensure_ascii=False)
        + ";\n"
        "renderTaskExpertFilters(samples);\n"
        "const mapping = Object.fromEntries(samples.map((sample) => {\n"
        "  const view = taskView(sample);\n"
        "  return [sample.name, { phase: view.phase, group: view.group, label: viewLabel(view.phase), needs_attention: view.needs_attention }];\n"
        "}));\n"
        "const entries = taskNavigationEntries(samples);\n"
        "const attentionEntry = entries.find((entry) => entry.key === 'attention');\n"
        "const activeEntry = entries.find((entry) => entry.key === 'active');\n"
        "const attentionTasks = samples.filter((sample) => matchesEntry(sample, attentionEntry));\n"
        "const activeOrder = sortForEntry(samples.filter((sample) => matchesEntry(sample, activeEntry)), activeEntry)\n"
        "  .map((sample) => taskView(sample).phase);\n"
        "console.log(JSON.stringify({ mapping, attentionCount: attentionEntry.count, attentionPhases: attentionTasks.map((sample) => taskView(sample).phase), activeOrder, expertHtml: captured.expert }));\n",
        encoding="utf-8",
    )
    output = subprocess.check_output(
        ["node", str(harness)],
        text=True,
        encoding="utf-8",
    )
    result = json.loads(output)
    assert result["mapping"] == expected_mapping
    # Inbox dedup: 4 anomalous tasks, one blocked+returned overlap counted once.
    assert result["attentionCount"] == 4
    assert sorted(result["attentionPhases"]) == sorted(
        ["blocked", "changes_requested", "integration_failed", "changes_requested"]
    )
    # Active sub-group ordering: returned first, then blocked, then execution.
    assert result["activeOrder"] == [
        "changes_requested",
        "changes_requested",
        "blocked",
        "in_progress",
        "claimed",
    ]
    # Event #2780: every exact-state option is selectable and shows its count.
    expert_html = result["expertHtml"]
    expected_counts = {
        "todo": 1,
        "claimed": 1,
        "in_progress": 1,
        "blocked": 1,
        "awaiting_review": 1,
        "changes_requested": 2,  # returned + released_returned
        "pending_integration": 2,  # P7 + P11
        "integration_failed": 1,
        "done": 1,
        "cancelled": 1,
        "unclassified": 1,
    }
    for phase, count in expected_counts.items():
        label = config["phase_labels"][phase]
        # Chinese labels contain no HTML-special characters, so the rendered
        # option text appears verbatim inside the expert filter markup.
        assert f">{label} ({count})<" in expert_html, (
            f"exact-state option for {phase} must show count {count}"
        )
    # The ten states required by event #2780 are all present.
    for phase in (
        "todo", "claimed", "in_progress", "blocked", "awaiting_review",
        "changes_requested", "pending_integration", "integration_failed",
        "done", "cancelled",
    ):
        assert f'value="{phase}"' in expert_html


def test_web_dialog_close_scopes_draft_cleanup_to_owner_dialog(tmp_path):
    """Regression for task #46: closing the nested assign dialog must keep
    the surrounding task-detail context alive.

    Runs the real ``clearDialogDrafts`` body in Node against dialog doubles
    so the scenario is exercised as behavior (state transitions across the
    actual close-event sequence), not as source-string matching:
    open task details -> open the assign dialog -> close it without
    choosing -> the "指定 Agent" flow must still be able to reopen, so the
    close of ``task-assign-dialog`` must NOT clear ``editingTaskId``.
    """
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function clearDialogDrafts(dialog)")
    end_marker = 'if (dialog.id === "member-dialog") state.editingMemberId = null;\n}'
    end = javascript.index(end_marker) + len(end_marker)

    harness = tmp_path / "dialog_cleanup_harness.js"
    harness.write_text(
        "const state = { editingTaskId: 'task_abc', editingMemberId: null };\n"
        + javascript[start:end]
        + "\n"
        + r"""
const outcomes = {};
// Nested assign dialog closes while task details stay open: editingTaskId survives.
clearDialogDrafts({ id: 'task-assign-dialog' });
outcomes.assignCloseKeepsTaskContext = state.editingTaskId === 'task_abc';
// The nested release confirmation dialog behaves the same way.
clearDialogDrafts({ id: 'task-release-dialog' });
outcomes.releaseCloseKeepsTaskContext = state.editingTaskId === 'task_abc';
// Unknown dialogs never mutate draft state.
clearDialogDrafts({ id: 'workspace-dialog' });
outcomes.unknownCloseIsInert = state.editingTaskId === 'task_abc' && state.editingMemberId === null;
// Auth dialogs are exempt and never clear anything.
state.editingMemberId = 'member_1';
clearDialogDrafts({ id: 'login-dialog' });
outcomes.loginCloseIsExempt = state.editingMemberId === 'member_1';
clearDialogDrafts({ id: 'token-secret-dialog' });
outcomes.tokenSecretCloseIsExempt = state.editingMemberId === 'member_1';
// Closing the task detail dialog itself still hands the context back.
clearDialogDrafts({ id: 'task-edit-dialog' });
outcomes.taskDetailCloseClearsTaskContext = state.editingTaskId === null;
// Closing the member dialog clears only its own draft.
clearDialogDrafts({ id: 'member-dialog' });
outcomes.memberDialogCloseClearsMemberContext =
  state.editingMemberId === null && state.editingTaskId === null;
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "assignCloseKeepsTaskContext": True,
        "releaseCloseKeepsTaskContext": True,
        "unknownCloseIsInert": True,
        "loginCloseIsExempt": True,
        "tokenSecretCloseIsExempt": True,
        "taskDetailCloseClearsTaskContext": True,
        "memberDialogCloseClearsMemberContext": True,
    }


def test_web_every_dialog_close_listener_uses_scoped_cleanup():
    """The close listener must pass its own dialog into the scoped cleanup,
    so no dialog close can clear another dialog's editing context."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    listener = javascript[
        javascript.index('dialog.addEventListener("close"') : javascript.index(
            "});",
            javascript.index('dialog.addEventListener("close"'),
        )
    ]
    assert "clearDialogDrafts(dialog)" in listener


def test_web_room_feed_hides_system_events_by_default_with_single_filter():
    """Regression for task #45: the Room feed checkbox defaults to checked
    and one shared filter function decides what is visible on first load,
    live appends, manual refresh and project switches (all of them render
    through renderEvents -> visibleFeedEvents)."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    markup = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    # The checkbox exists, defaults to checked, and sits in the feed header.
    assert 'id="event-hide-system" checked' in markup
    assert "只看消息动态" in markup
    assert "hideSystemFeedEvents: true" in javascript
    # Rendering goes through the single shared filter entry point.
    assert "function visibleFeedEvents(events)" in javascript
    assert "let events = visibleFeedEvents([...state.events]);" in javascript
    # The old inline filter chain must be gone (single source of truth).
    assert 'if (state.eventFilter === "messages") events = events.filter' not in javascript
    # The checkbox wiring rerenders the feed from the current snapshot.
    listener = javascript[
        javascript.index('elements["event-hide-system"].addEventListener') : javascript.index(
            "});",
            javascript.index('elements["event-hide-system"].addEventListener'),
        )
    ]
    assert "state.hideSystemFeedEvents = elements[\"event-hide-system\"].checked" in listener
    assert "renderEvents(state.snapshot.agents, state.snapshot.tasks)" in listener
    # Display-layer only: no backend contract strings inside the filter.
    assert "api(" not in javascript[javascript.index("function visibleFeedEvents(events)"):javascript.index("function renderEvents(")]

    start = javascript.index("function visibleFeedEvents(events)")
    end = javascript.index("function renderEvents(")
    harness = tmp_path_feed_harness(javascript, start, end)
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "defaultCheckedHidesSystemEvents": True,
        "messageKindsSurvive": True,
        "uncheckShowsEverything": True,
        "recheckRestoresImmediately": True,
        "filterCombinesWithDropdown": True,
    }


def tmp_path_feed_harness(javascript, start, end):
    import tempfile

    harness = Path(tempfile.mkstemp(suffix=".js")[1])
    harness.write_text(
        "const state = { hideSystemFeedEvents: true, eventFilter: 'all' };\n"
        + javascript[start:end].replace("function renderEvents(agents, tasks) {", "function renderEventsUnused() {", 1)
        # renderEvents body references DOM elements; only visibleFeedEvents is exercised.
        .split("function renderEventsUnused() {")[0]
        + r"""
const samples = [
  { id: 1, event_type: 'message.message' },
  { id: 2, event_type: 'message.decision' },
  { id: 3, event_type: 'message.blocker' },
  { id: 4, event_type: 'message.system' },
  { id: 5, event_type: 'message.acknowledged' },
  { id: 6, event_type: 'agent.joined' },
  { id: 7, event_type: 'task.claimed' },
  { id: 8, event_type: 'lease.acquired' },
  { id: 9, event_type: 'work.reported' },
  { id: 10, event_type: 'credential.issued' },
];
const kinds = (events) => events.map((event) => event.id);
const outcomes = {};
// Default (checked): only ordinary messages, decisions and blockers survive.
outcomes.defaultCheckedHidesSystemEvents =
  JSON.stringify(kinds(visibleFeedEvents(samples))) === JSON.stringify([1, 2, 3]);
// The three allowed kinds are exactly the message.message/decision/blocker trio.
outcomes.messageKindsSurvive = visibleFeedEvents(samples).every((event) =>
  ['message.message', 'message.decision', 'message.blocker'].includes(event.event_type));
// Unchecking shows the full append-only feed, including joins and leases.
state.hideSystemFeedEvents = false;
outcomes.uncheckShowsEverything = kinds(visibleFeedEvents(samples)).length === samples.length;
// Rechecking filters again immediately, without any reload.
state.hideSystemFeedEvents = true;
outcomes.recheckRestoresImmediately =
  JSON.stringify(kinds(visibleFeedEvents(samples))) === JSON.stringify([1, 2, 3]);
// The dropdown filter composes with the checkbox instead of being replaced.
state.eventFilter = 'decisions';
const decisionsOnly = kinds(visibleFeedEvents(samples));
state.eventFilter = 'all';
outcomes.filterCombinesWithDropdown =
  JSON.stringify(decisionsOnly) === JSON.stringify([2, 3]);
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    return harness


def test_web_structured_renderer_renders_safe_markdown_and_blocks_attacks(tmp_path):
    """Regression for task #51: the structured body renderer turns the safe
    Markdown subset (fenced code, headings, lists, quotes, inline code)
    into readable blocks while every attacker payload stays inert escaped
    text, and a renderer crash degrades to escaped plain text for that
    event only."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    def extract(name, end_name):
        start = javascript.index(f"function {name}")
        end = javascript.index(f"function {end_name}", start)
        return javascript[start:end]

    parts = [
        extract("renderInlineCode", "renderMessageLine"),
        extract("renderMessageLine", "renderStructuredBody"),
        extract("renderStructuredBody", "renderMessageLines"),
        extract("renderMessageLines", "renderMessageBodySafely"),
        extract("renderMessageBodySafely", "showToast"),
    ]

    harness = tmp_path / "structured_renderer_harness.js"
    harness.write_text(
        "const state = { expandedEvents: new Set() };\n"
        "const MESSAGE_COLLAPSE_LINES = 40;\n"
        "const MESSAGE_PREVIEW_LINES = 12;\n"
        "function escapeHtml(value) { return String(value ?? '')\n"
        "  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')\n"
        "  .replace(/\"/g, '&quot;').replace(/'/g, '&#39;'); }\n"
        "const consoleWarn = [];\n"
        "const __realConsole = globalThis.console;\n"
        "const console = { warn: (...args) => consoleWarn.push(args), log: (...args) => __realConsole.log(...args) };\n"
        + "\n".join(parts)
        + "\n"
        + r"""
const outcomes = {};
// Safe markdown subset becomes readable blocks.
const sample = [
  '## 修复说明',
  '',
  '根因是分页边界：',
  '- 命中了全局游标',
  '* 未按任务过滤',
  '',
  '> 复现命令如下',
  '```bash',
  'pytest --basetemp=<tmp>',
  '```',
  '见 `task_history.py`',
].join('\n');
const html = renderStructuredBody(sample);
outcomes.heading = html.includes('msg-h2') && html.includes('修复说明');
outcomes.listItems = (html.match(/<li>/g) || []).length === 2;
outcomes.quote = html.includes('msg-quote') && html.includes('复现命令如下');
outcomes.fencedCode = html.includes('msg-code') && html.includes('pytest --basetemp=&lt;tmp&gt;');
outcomes.inlineCode = html.includes('<code>task_history.py</code>');
// Attack payloads stay inert: no raw tags, no attribute breakout, no javascript: href.
const attacks = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '"><a href="javascript:alert(1)">x</a>',
  '```</code><script>alert(1)</script>```',
];
const attackHtml = attacks.map((a) => renderStructuredBody(a)).join('');
outcomes.noScriptTag = !attackHtml.includes('<script>');
outcomes.noOnerror = !attackHtml.includes('<img');
outcomes.noRawHref = !attackHtml.includes('<a href');
outcomes.escapedVisible = attackHtml.includes('&lt;script&gt;');
// A renderer crash isolates the single event and degrades to escaped text.
const original = renderMessageBody;
renderMessageBody = () => { throw new Error('boom'); };
const degraded = renderMessageBodySafely(42, '<img src=x>');
outcomes.degradesToEscapedText = degraded.includes('&lt;img src=x&gt;');
outcomes.warnedOnce = consoleWarn.length === 1;
renderMessageBody = original;
outcomes.recoversAfterCrash = renderMessageBodySafely(43, 'ok').includes('ok');
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    output = subprocess.check_output(["node", str(harness)], text=True, encoding="utf-8")
    outcomes = json.loads(output)
    assert outcomes == {
        "heading": True,
        "listItems": True,
        "quote": True,
        "fencedCode": True,
        "inlineCode": True,
        "noScriptTag": True,
        "noOnerror": True,
        "noRawHref": True,
        "escapedVisible": True,
        "degradesToEscapedText": True,
        "warnedOnce": True,
        "recoversAfterCrash": True,
    }


def test_web_feed_uses_safe_structured_renderer():
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    # The Room feed must go through the fallback wrapper, never the raw renderer.
    assert "renderMessageBodySafely(event.id, event.payload?.body || \"\")" in javascript
    assert 'renderMessageBody(event.id, event.payload?.body' not in javascript


def test_web_agent_cards_use_unified_projection_with_model_fallback(tmp_path):
    """Regression for task #53: every agent card renders the same field
    sequence (name, client · role · model, task context, heartbeat) from
    the shared identity projection, with an explicit `unknown` model when
    the backend reports none — and never branches on vendor names."""
    javascript = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    start = javascript.index("function renderAgents(agents)")
    end = javascript.index("function taskNotFinished(task)", start)

    harness = tmp_path / "agent_cards_harness.js"
    harness.write_text(
        "const state = { snapshot: { tasks: [] } };\n"
        "function escapeHtml(value) { return String(value ?? ''); }\n"
        "function shortId(value) { return String(value); }\n"
        "function initials() { return 'AB'; }\n"
        "function avatarColorClass() { return 'a'; }\n"
        "function formatRelativeTime() { return '刚刚'; }\n"
        "function legacyStatus(status) { return String(status); }\n"
        "function taskPhaseLabel() { return '待认领'; }\n"
        "function currentAgentRoster(agents) { return agents; }\n"
        "const captured = {};\n"
        "const document = { getElementById: (id) => ({ set innerHTML(value) { captured[id] = value; }, get innerHTML() { return captured[id] || ''; }, set textContent(value) { captured[id + ':text'] = value; } }) };\n"
        "const elements = new Proxy({}, { get: (target, key) => document.getElementById(key) });\n"
        + javascript[start:end]
        + "\n"
        + r"""
const agents = [
  {
    id: 'a1', name: 'Alpha', client: 'codex', role: 'executor',
    connection_status: 'connected', session_count: 5, models: ['GPT-5'],
    last_heartbeat: 'now', last_activity_at: 'now', unread_count: 0,
  },
  {
    id: 'a2', name: 'Beta', client: 'trae', role: 'reviewer',
    connection_status: 'disconnected', session_count: 2, models: [],
    last_heartbeat: 'earlier', last_activity_at: 'earlier', unread_count: 3,
  },
  {
    id: 'a3', name: 'Gamma', client: 'workbuddy', role: 'executor',
    connection_status: 'disconnected', session_count: 1, models: null,
    last_heartbeat: 'old', last_activity_at: 'old', unread_count: 0,
  },
];
renderAgents(agents);
const html = captured['agent-list'];
const outcomes = {};
outcomes.rendersAllThree = (html.match(/agent-item/g) || []).length === 3;
outcomes.modelShown = html.includes('模型 GPT-5');
outcomes.unknownFallbacks = (html.match(/模型 unknown/g) || []).length === 2;
outcomes.unifiedClientRoleLine = (html.match(/· 模型 /g) || []).length === 3;
outcomes.disconnectedBadge = html.includes('已接入 · 未连接');
outcomes.offlineSortedLast = html.indexOf('agent-item') < html.indexOf('is-disconnected');
// 模型缺失必须显式 unknown；渲染层绝不硬编码厂商名或猜测模型。
console.log(JSON.stringify(outcomes));
""",
        encoding="utf-8",
    )
    run = subprocess.run(["node", str(harness)], capture_output=True, text=True, encoding="utf-8")
    assert run.returncode == 0, f"node harness failed: {run.stderr[-2000:]}"
    outcomes = json.loads(run.stdout)
    assert outcomes == {
        "rendersAllThree": True,
        "modelShown": True,
        "unknownFallbacks": True,
        "unifiedClientRoleLine": True,
        "disconnectedBadge": True,
        "offlineSortedLast": True,
    }
    # 渲染层不得按厂商名分支：renderAgents 段不允许出现厂商字面量比较。
    agent_section = javascript[start:end]
    vendor_pattern = re.compile(r"""(===|!==)\s*["'](codex|workbuddy|grok|trae)""", re.I)
    assert not vendor_pattern.search(agent_section)


def test_web_typography_baseline_wraps_long_content_and_narrow_selects():
    """Regression for task #52: a shared wrap baseline keeps CJK/Latin mix,
    long paths and commands inside every panel without horizontal
    overflow, and composer selects shrink on narrow viewports."""
    stylesheet = (WEB_DIR / "app.css").read_text(encoding="utf-8")
    # 统一换行基线覆盖主要正文/路径/证据容器。
    baseline = stylesheet[stylesheet.index("/* #52 排版基线") :]
    for selector in (
        ".event-content,", ".event-body,", ".msg-line,", ".task-contract-description,",
        ".lease-item,", ".agent-copy,", ".review-item,", ".criteria-list,",
    ):
        assert selector in baseline
    # 选择器列表聚合声明：一条声明覆盖全部容器。
    assert baseline.count("overflow-wrap: anywhere;") == 1
    assert baseline.count(",") >= 7
    assert "line-height: 1.5;" in baseline
    # 窄屏下 composer 下拉收缩，不再撑出横向滚动。
    assert ".composer-options select {" in stylesheet
    assert "flex: 1 1 auto;" in stylesheet
