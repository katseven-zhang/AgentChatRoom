const state = {
  config: null,
  integration: null,
  integrationFormat: "generic",
  integrationOnboardingMode: "first_setup",
  integrationTransport: "http",
  pendingHttpSetup: null,
  issuedHttpSetup: null,
  integrationLocalPlan: null,
  integrationLocalApplyResult: null,
  integrationLocalRequest: 0,
  authRequired: false,
  authenticated: false,
  projects: [],
  projectId: null,
  snapshot: null,
  events: [],
  eventIds: new Set(),
  eventSource: null,
  presenceTimer: null,
  presenceRefreshInFlight: false,
  snapshotInFlight: null,
  selectGeneration: 0,
  streamHadError: false,
  busyCount: 0,
  taskEntry: "",
  taskSort: { key: "", direction: "asc" },
  taskExpert: { execution: "", verification: "", integration: "", priority: "", owner: "", number: "" },
  eventFilter: "all",
  hideSystemFeedEvents: true,
  taskIntakeTargets: [],
  taskIntakes: [],
  editingTaskId: null,
  editingCredentialId: null,
  members: [],
  credentials: [],
  workspaces: [],
  managedBackups: [],
  autoBackupInfo: null,
  documentContentCache: {},
  auditEvents: [],
  auditHasOlder: false,
  auditHasNewer: false,
  auditFilter: "",
  auditPage: 1,
  backupPage: 1,
  runtime: null,
  expandedEvents: new Set(),
  collapsedGroups: new Set(),
  lastRenderedEventId: 0,
  taskHistory: null,
  focusEventId: 0,
};

const elements = Object.fromEntries(
  [
    "app-shell", "left-panel-resizer", "right-panel-resizer",
    "product-name", "connection-state", "connection-label", "project-count",
    "project-list", "agent-count", "agent-list", "room-name", "room-path",
    "create-task-button", "archive-project-button", "project-settings-button", "export-project-button",
    "connect-agent-button", "logout-button",
    "metric-agents", "metric-active", "metric-leases",
    "metric-reviews", "active-task-list", "recent-event-list", "recent-activity-project",
    "lease-list", "review-list", "chat-subtitle", "chat-stream", "event-filter", "event-hide-system",
    "message-form", "message-input", "message-kind", "message-channel", "message-task", "message-priority",
    "message-requires-ack", "send-message-button", "onboarding", "new-message-notice",
    "project-dialog", "project-form", "project-name-input", "project-path-input",
    "project-folder-picker-button",
    "task-dialog", "task-form", "task-raw-description-input", "task-target-agent-input",
    "task-target-agent-empty", "task-intake-submit", "task-intake-list", "task-table",
    "task-sort-controls",
    "document-list",
    "task-edit-dialog", "task-detail-heading", "task-detail-contract", "task-timeline",
    "task-history-filter", "task-history-load-earlier", "task-history-load-later",
    "task-assign-button", "task-assignment-list", "task-assign-dialog", "task-assign-form",
    "task-release-button", "task-release-dialog", "task-release-form", "task-release-title",
    "task-release-reason", "task-release-reason-text", "task-release-submit", "task-cancel-button",
    "task-assign-title", "task-assign-agent", "task-assign-agent-empty", "task-assign-note", "task-assign-submit",
    "settings-dialog", "settings-form", "settings-project-name", "settings-lease-policy", "settings-roles",
    "settings-default-priority", "settings-mcp-message-limit", "settings-audit-retention", "settings-auto-backup", "settings-backup-max-kept", "settings-backup-hint",
    "archive-dialog", "archive-form", "archive-project-name", "permanent-delete-input",
    "remove-project-hint", "remove-project-submit",
    "integration-dialog", "integration-data-dir", "integration-log-path",
    "integration-config-path", "integration-config-code", "integration-join-code", "integration-cli-code",
    "integration-project-rules-path", "integration-project-rules-code", "integration-onboarding-prompt", "toast-region",
    "integration-local-assistant", "integration-local-state", "integration-local-mode",
    "integration-local-path", "integration-local-message", "integration-local-changes",
    "integration-local-reload", "integration-local-facts", "integration-local-backup",
    "integration-local-refresh", "integration-local-apply",
    "integration-transport-tabs", "integration-http-token-guide", "integration-open-token-button",
    "integration-http-action-title", "integration-http-action-description", "integration-http-action-steps",
    "integration-target-project",
    "member-list", "refresh-audit-button", "audit-event-filter",
    "create-token-button", "token-list", "token-project-context", "workspace-list", "audit-list",
    "create-backup-button", "backup-list",
    "refresh-runtime-button", "runtime-status", "runtime-config", "runtime-config-raw", "runtime-log",
    "login-dialog", "login-form", "login-token", "login-error",
    "token-dialog", "token-form", "token-name", "token-member", "token-days", "token-permissions",
    "token-agent-name", "token-agent-name-group",
    "token-dialog-context", "token-dialog-title", "token-submit-button",
    "token-existing-config-advanced", "token-existing-config-group", "token-project-credential-name", "token-existing-config",
    "token-permissions-dialog", "token-permissions-form", "token-permissions-project",
    "token-permissions-title", "token-permissions-edit", "token-permissions-submit",
    "token-extend-dialog", "token-extend-form", "token-extend-project", "token-extend-title",
    "token-extend-current", "token-extend-days", "token-extend-submit",
    "token-secret-dialog", "token-secret-section", "token-secret-value", "token-secret-close", "token-secret-copy",
    "token-secret-context", "token-secret-title",
    "token-config-section", "token-config-value", "token-config-path", "token-config-projects", "token-config-format",
    "token-config-heading", "token-config-copy", "token-config-hint",
  ].map((id) => [id, document.getElementById(id)])
);

const PANEL_LAYOUT = Object.freeze({
  left: Object.freeze({
    cssProperty: "--left-panel-width",
    defaultProperty: "--left-panel-default-width",
    minProperty: "--left-panel-min-width",
    maxProperty: "--left-panel-max-width",
    storageKey: "agentchatroom.layout.leftPanelWidth",
    label: "左侧栏",
    dragDirection: 1,
  }),
  right: Object.freeze({
    cssProperty: "--right-panel-width",
    defaultProperty: "--right-panel-default-width",
    minProperty: "--right-panel-min-width",
    maxProperty: "--right-panel-max-width",
    storageKey: "agentchatroom.layout.rightPanelWidth",
    label: "Room 动态",
    dragDirection: -1,
  }),
});

const panelLayoutState = {
  preferred: { left: 0, right: 0 },
  effective: { left: 0, right: 0 },
  activeSide: null,
  pointerId: null,
  startX: 0,
  startWidth: 0,
  resizeFrame: null,
};

function clampNumber(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), maximum);
}

function cssPixelValue(property) {
  const value = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(property));
  return Number.isFinite(value) ? value : 0;
}

function panelRange(side) {
  const config = PANEL_LAYOUT[side];
  const minimum = cssPixelValue(config.minProperty);
  const maximum = Math.max(minimum, cssPixelValue(config.maxProperty));
  return {
    minimum,
    maximum,
    defaultWidth: clampNumber(cssPixelValue(config.defaultProperty), minimum, maximum),
  };
}

function readPanelPreference(side) {
  const range = panelRange(side);
  try {
    const stored = Number.parseFloat(localStorage.getItem(PANEL_LAYOUT[side].storageKey));
    return Number.isFinite(stored)
      ? clampNumber(stored, range.minimum, range.maximum)
      : range.defaultWidth;
  } catch (_error) {
    return range.defaultWidth;
  }
}

function persistPanelPreferences() {
  try {
    Object.keys(PANEL_LAYOUT).forEach((side) => {
      localStorage.setItem(
        PANEL_LAYOUT[side].storageKey,
        String(Math.round(panelLayoutState.preferred[side])),
      );
    });
  } catch (_error) {
    // The layout still works for this page when browser storage is unavailable.
  }
}

function fittedPanelWidths(preferred = panelLayoutState.preferred) {
  const leftRange = panelRange("left");
  const rightRange = panelRange("right");
  let left = clampNumber(preferred.left, leftRange.minimum, leftRange.maximum);
  let right = clampNumber(preferred.right, rightRange.minimum, rightRange.maximum);
  const shellWidth = elements["app-shell"].getBoundingClientRect().width;
  const resizerSpace = cssPixelValue("--panel-resizer-width") * 2;
  const workspaceMinimum = cssPixelValue("--workspace-min-width");
  const availableForPanels = Math.max(
    leftRange.minimum + rightRange.minimum,
    shellWidth - workspaceMinimum - resizerSpace,
  );
  let excess = Math.max(0, left + right - availableForPanels);

  // Room 动态是主要观察面板。窗口变窄时优先压缩左栏，再压缩右栏。
  const leftReduction = Math.min(excess, left - leftRange.minimum);
  left -= leftReduction;
  excess -= leftReduction;
  right -= Math.min(excess, right - rightRange.minimum);
  return { left, right };
}

function dynamicPanelMaximum(side) {
  const range = panelRange(side);
  const otherSide = side === "left" ? "right" : "left";
  const otherWidth = panelLayoutState.effective[otherSide] || panelLayoutState.preferred[otherSide];
  const shellWidth = elements["app-shell"].getBoundingClientRect().width;
  const remaining = shellWidth
    - otherWidth
    - cssPixelValue("--workspace-min-width")
    - (cssPixelValue("--panel-resizer-width") * 2);
  return Math.max(range.minimum, Math.min(range.maximum, remaining));
}

function updatePanelResizerAccessibility() {
  Object.keys(PANEL_LAYOUT).forEach((side) => {
    const range = panelRange(side);
    const value = Math.round(panelLayoutState.effective[side]);
    const resizer = elements[`${side}-panel-resizer`];
    resizer.setAttribute("aria-valuemin", String(Math.round(range.minimum)));
    resizer.setAttribute("aria-valuemax", String(Math.round(dynamicPanelMaximum(side))));
    resizer.setAttribute("aria-valuenow", String(value));
    resizer.setAttribute("aria-valuetext", `${PANEL_LAYOUT[side].label} ${value} 像素`);
  });
}

function applyPreferredPanelLayout() {
  const fitted = fittedPanelWidths();
  panelLayoutState.effective = fitted;
  document.documentElement.style.setProperty(PANEL_LAYOUT.left.cssProperty, `${fitted.left}px`);
  document.documentElement.style.setProperty(PANEL_LAYOUT.right.cssProperty, `${fitted.right}px`);
  updatePanelResizerAccessibility();
}

function setPreferredPanelWidth(side, width, persist = false) {
  const range = panelRange(side);
  panelLayoutState.preferred[side] = clampNumber(
    width,
    range.minimum,
    dynamicPanelMaximum(side),
  );
  applyPreferredPanelLayout();
  if (persist) persistPanelPreferences();
}

function finishPanelResize(side, event) {
  if (panelLayoutState.activeSide !== side) return;
  const resizer = elements[`${side}-panel-resizer`];
  if (event && panelLayoutState.pointerId !== null && resizer.hasPointerCapture(panelLayoutState.pointerId)) {
    resizer.releasePointerCapture(panelLayoutState.pointerId);
  }
  panelLayoutState.activeSide = null;
  panelLayoutState.pointerId = null;
  resizer.classList.remove("is-active");
  document.body.classList.remove("is-resizing-panels");
  persistPanelPreferences();
}

function initializePanelLayout() {
  panelLayoutState.preferred.left = readPanelPreference("left");
  panelLayoutState.preferred.right = readPanelPreference("right");
  applyPreferredPanelLayout();

  Object.keys(PANEL_LAYOUT).forEach((side) => {
    const config = PANEL_LAYOUT[side];
    const resizer = elements[`${side}-panel-resizer`];
    resizer.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || getComputedStyle(resizer).display === "none") return;
      event.preventDefault();
      panelLayoutState.activeSide = side;
      panelLayoutState.pointerId = event.pointerId;
      panelLayoutState.startX = event.clientX;
      panelLayoutState.startWidth = panelLayoutState.effective[side];
      resizer.setPointerCapture(event.pointerId);
      resizer.classList.add("is-active");
      document.body.classList.add("is-resizing-panels");
    });
    resizer.addEventListener("pointermove", (event) => {
      if (panelLayoutState.activeSide !== side || panelLayoutState.pointerId !== event.pointerId) return;
      const delta = (event.clientX - panelLayoutState.startX) * config.dragDirection;
      setPreferredPanelWidth(side, panelLayoutState.startWidth + delta);
    });
    resizer.addEventListener("pointerup", (event) => finishPanelResize(side, event));
    resizer.addEventListener("pointercancel", (event) => finishPanelResize(side, event));
    resizer.addEventListener("dblclick", () => {
      setPreferredPanelWidth(side, panelRange(side).defaultWidth, true);
    });
    resizer.addEventListener("keydown", (event) => {
      const step = event.shiftKey ? 32 : 16;
      let nextWidth = panelLayoutState.effective[side];
      if (event.key === "Home") nextWidth = panelRange(side).minimum;
      else if (event.key === "End") nextWidth = dynamicPanelMaximum(side);
      else if (event.key === "Enter") nextWidth = panelRange(side).defaultWidth;
      else if (event.key === "ArrowLeft") nextWidth += side === "right" ? step : -step;
      else if (event.key === "ArrowRight") nextWidth += side === "right" ? -step : step;
      else return;
      event.preventDefault();
      setPreferredPanelWidth(side, nextWidth, true);
    });
  });

  window.addEventListener("resize", () => {
    if (panelLayoutState.resizeFrame !== null) cancelAnimationFrame(panelLayoutState.resizeFrame);
    panelLayoutState.resizeFrame = requestAnimationFrame(() => {
      panelLayoutState.resizeFrame = null;
      applyPreferredPanelLayout();
    });
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function initials(name) {
  const clean = String(name || "Agent").trim();
  return clean.slice(0, 2).toUpperCase();
}

function shortId(value) {
  return value ? value.slice(-6) : "-";
}

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}

function formatRelativeTime(value) {
  if (!value) return "无记录";
  const elapsed = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(elapsed)) return "时间未知";
  const seconds = Math.max(0, Math.floor(elapsed / 1000));
  if (seconds < 10) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function agentStatus(status) {
  return {
    connected: "已连接", disconnected: "未连接", online: "已连接", offline: "未连接", registered: "已接入",
  }[status] || status;
}

function projectSource(project) {
  return project.git_remote ? "Git" : "本地路径";
}

function currentAgentRoster(agentIdentities) {
  return (agentIdentities || []).filter((agent) => agent.member_status !== "revoked");
}

function connectedAgentCount(agentIdentities) {
  return currentAgentRoster(agentIdentities)
    .filter((agent) => agent.connection_status === "connected").length;
}

function taskViewConfig() {
  return state.config?.domain?.task_view || null;
}

function taskView(task) {
  const view = task?.state_view;
  if (view) return view;
  // Snapshot data always carries state_view; treat anything else as
  // unclassified so unknown shapes surface instead of being guessed.
  return {
    schema_version: 0,
    phase: "unclassified",
    group: "unclassified",
    needs_attention: false,
    primary_badge: "unclassified",
    auxiliary_badges: [],
    execution_status: task?.execution_status || "",
    verification_status: task?.verification_status || "",
    integration_status: task?.integration_status || "",
  };
}

function viewLabel(code) {
  const labels = taskViewConfig()?.phase_labels || {};
  return labels[code] || code;
}

function viewGroupLabel(code) {
  const labels = taskViewConfig()?.group_labels || {};
  return labels[code] || code;
}

function viewFaceLabel(kind, code) {
  const labels = taskViewConfig()?.[`${kind}_labels`] || {};
  return labels[code] || code;
}

function taskPhaseLabel(task) {
  return viewLabel(taskView(task).phase);
}

function taskPhaseClass(task) {
  return taskView(task).phase;
}

function taskViewBadgeHtml(view) {
  const auxiliary = (view.auxiliary_badges || [])
    .map((code) => `<span class="status-badge aux ${escapeHtml(code)}">${escapeHtml(viewLabel(code))}</span>`)
    .join("");
  return `<span class="status-badge ${escapeHtml(view.phase)}">${escapeHtml(viewLabel(view.phase))}</span>${auxiliary}`;
}

function legacyStatus(status) {
  return viewFaceLabel("status", status);
}

function executionFaceLabel(status) {
  return viewFaceLabel("execution", status);
}

function verificationFaceLabel(status) {
  return viewFaceLabel("verification", status);
}

function integrationFaceLabel(status) {
  return viewFaceLabel("integration", status);
}

function taskNeedsIntegration(task) {
  return taskView(task).execution_status !== "cancelled";
}

function assignmentStatus(assignment) {
  if (typeof assignment === "string") {
    return { pending: "待确认", accepted: "已接受", declined: "已拒绝", blocked: "受阻", cancelled: "已取消" }[assignment] || assignment;
  }
  // 指派生命周期与任务生命周期分离：cancelled 需要区分
  // 「因释放/改派失效」与「目标明确取消受理」，不能都显示成已取消。
  const note = String(assignment.response_note || "");
  if (assignment.status === "cancelled") {
    if (note.includes("task release")) return "因任务释放失效";
    if (note.includes("superseded by reassignment")) return "因改派失效";
  }
  return assignmentStatus(assignment.status);
}

function integrationResult(result) {
  return { done: "集成通过", failed: "集成失败" }[result] || result;
}

function textLines(value) {
  return String(value || "").split("\n").map((item) => item.trim()).filter(Boolean);
}

function parseTestLines(value) {
  return textLines(value).map((line) => {
    const separator = line.lastIndexOf("::");
    if (separator < 1) throw new Error(`无效测试记录：${line}`);
    const exitCode = Number(line.slice(separator + 2));
    if (!Number.isInteger(exitCode)) throw new Error(`无效测试退出码：${line}`);
    return { command: line.slice(0, separator).trim(), exit_code: exitCode };
  });
}

function eventLabel(type) {
  const labels = {
    "project.created": "项目已创建", "project.updated": "更新了项目设置",
    "project.archived": "归档了项目", "project.restored": "恢复了项目",
    "agent.joined": "加入了 Room",
    "task.created": "创建了任务", "task.claimed": "认领了任务",
    "task.assigned": "派发了任务", "task.assignment_acknowledged": "回应了任务派发",
    "task.handoff_requested": "请求了任务交接", "task.handoff_acknowledged": "回应了任务交接",
    "task.completed": "声明执行完成",
    "task.updated": "更新了任务", "task.blocked": "阻塞了任务",
    "task.intake_submitted": "提交了任务意图", "task.intake_acknowledged": "受理了任务意图",
    "task.intake_reassigned": "改派了任务意图", "task.intake_defined": "正式定义了任务",
    "task.unblocked": "解除了任务阻塞", "task.released": "释放了任务",
    "task.cancelled": "取消了任务", "lease.acquired": "占用了文件范围",
    "lease.released": "释放了文件范围", "lease.conflict": "检测到文件冲突",
    "lease.pre_commit_blocked": "提交前检查被文件占用阻断",
    "work.reported": "提交了工作证据", "review.submitted": "提交了验证结论",
    "task.integration_completed": "完成了最终集成", "task.integration_failed": "记录了集成失败",
    "message.acknowledged": "确认了消息",
    "message.message": "发布了消息", "message.decision": "发布了决策",
    "message.blocker": "发布了阻塞", "message.system": "发布了系统消息",
    "credential.issued": "签发了 Agent Token", "credential.rotated": "更换了 Agent Token",
    "credential.permissions_updated": "修改了 Agent Token 权限", "credential.extended": "续期了 Agent Token",
    "credential.revoked": "吊销了 Agent Token", "workspace.registered": "登记了 Workspace",
    "workspace.updated": "更新了 Workspace",
    "member.created": "创建了项目成员", "member.updated": "更新了项目成员",
    "member.revoked": "吊销了项目成员",
  };
  return labels[type] || type;
}

function leaseMode(mode) {
  return { readonly: "只读", shared: "共享", exclusive: "独占" }[mode] || mode;
}

function messageKind(kind) {
  return { message: "普通消息", decision: "决策", blocker: "阻塞", system: "系统" }[kind] || kind;
}

function messageChannel(channel) {
  return { public: "公共", task: "任务", review: "评审", system: "系统" }[channel] || channel;
}

function messageModelBadge(event) {
  if (!event.actor_session_id) return "";
  const reported = typeof event.payload?.model_display_name === "string"
    ? event.payload.model_display_name.trim()
    : "";
  const missing = !reported;
  const label = missing ? "模型未上报" : `模型 · ${reported}`;
  const details = missing
    ? "这条历史 Agent 消息没有结构化模型信息；AgentChatRoom 不会使用会话初始模型补全。"
    : `客户端上报的本次回复 UI 模型名称：${reported}。该名称未由 AgentChatRoom 独立验证。`;
  return `<span class="model-badge${missing ? " is-missing" : ""}" title="${escapeHtml(details)}">${escapeHtml(label)}</span>`;
}

function eventIdBadge(projectSeq, eventId) {
  const internalId = Number(eventId);
  if (!Number.isFinite(internalId)) return "";
  const seq = Number(projectSeq);
  // 用户可见编号是项目级序号；全局 event_id 只作内部深链定位。
  if (!Number.isFinite(seq) || seq <= 0) {
    return `<button type="button" class="event-id" data-open-event="${internalId}" title="打开并定位事件（全局 ID ${internalId}）" aria-label="事件 全局 ID ${internalId}">#${internalId}</button>`;
  }
  return `<button type="button" class="event-id" data-open-event="${internalId}" title="事件编号 #${seq}（项目内序号，全局 ID ${internalId}）" aria-label="事件编号 ${seq}">#${seq}</button>`;
}

function avatarColorClass(seed) {
  let hash = 0;
  const text = String(seed || "");
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
  }
  return `avatar-c${hash % 6}`;
}

const MESSAGE_COLLAPSE_LINES = 12;
const MESSAGE_PREVIEW_LINES = 8;

function renderInlineCode(escapedLine) {
  // 输入已整体转义；此处仅把成对反引号内的片段包成 <code>。
  return escapedLine.replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderMessageLine(line) {
  const trimmed = line.trim();
  const escaped = escapeHtml(line);
  const heading = trimmed.match(/^【(.+)】/);
  if (heading) {
    return `<div class="msg-heading">${escapeHtml(trimmed)}</div>`;
  }
  if (/^(✓|✔|✅)/.test(trimmed)) {
    return `<div class="msg-line msg-pass">${renderInlineCode(escaped)}</div>`;
  }
  if (/^(✗|✘|❌)/.test(trimmed)) {
    return `<div class="msg-line msg-fail">${renderInlineCode(escaped)}</div>`;
  }
  const keyValue = trimmed.match(/^([A-Z][A-Z_]{2,}|[\u4e00-\u9fa5A-Za-z]{2,12})(\s*[:：]\s*)(\S.*)$/);
  if (keyValue && !trimmed.startsWith("http")) {
    return `<div class="msg-line"><span class="msg-key">${escapeHtml(keyValue[1])}${escapeHtml(keyValue[2])}</span>${renderInlineCode(escapeHtml(keyValue[3]))}</div>`;
  }
  return `<div class="msg-line">${renderInlineCode(escaped)}</div>`;
}

// 安全 Markdown 子集渲染契约：围栏代码块、# 标题、-/*/数字 列表、> 引用、
// 普通段落与行内 `code`。所有源文本先整体转义，结构标签由渲染器生成，
// 输入中的 HTML/脚本/事件属性永远以字面文本呈现。
function renderStructuredBody(body) {
  const lines = String(body || "").split("\n");
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      index += 1; // 跳过闭合围栏（缺失时也安全结束）
      blocks.push(`<pre class="msg-code"><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }
    if (!trimmed) {
      index += 1;
      continue;
    }
    const heading = trimmed.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      blocks.push(`<div class="msg-h msg-h${heading[1].length}">${renderInlineCode(escapeHtml(heading[2]))}</div>`);
      index += 1;
      continue;
    }
    if (/^>\s?/.test(trimmed)) {
      const quoteLines = [];
      while (index < lines.length && /^>\s?/.test(lines[index].trim())) {
        quoteLines.push(lines[index].trim().replace(/^>\s?/, ""));
        index += 1;
      }
      blocks.push(`<blockquote class="msg-quote">${quoteLines.map((item) => renderMessageLine(item)).join("")}</blockquote>`);
      continue;
    }
    if (/^(-|\*|•|·)\s+/.test(trimmed) || /^\d+[.)]\s+/.test(trimmed)) {
      const ordered = /^\d+[.)]\s+/.test(trimmed);
      const items = [];
      while (index < lines.length) {
        const candidate = lines[index].trim();
        if (ordered && /^\d+[.)]\s+/.test(candidate)) {
          items.push(candidate.replace(/^\d+[.)]\s+/, ""));
        } else if (!ordered && /^(-|\*|•|·)\s+/.test(candidate)) {
          items.push(candidate.replace(/^(-|\*|•|·)\s+/, ""));
        } else {
          break;
        }
        index += 1;
      }
      const tag = ordered ? "ol" : "ul";
      blocks.push(`<${tag} class="msg-list">${items.map((item) => `<li>${renderInlineCode(escapeHtml(item))}</li>`).join("")}</${tag}>`);
      continue;
    }
    blocks.push(renderMessageLine(line));
    index += 1;
  }
  return blocks.join("");
}

function renderMessageLines(lines) {
  return lines.map((line) => renderMessageLine(line)).join("");
}

// 降级契约：结构化渲染抛错时只隔离当前事件，退回整段转义纯文本，
// 不阻塞动态流、历史分页或实时追加。
function renderMessageBodySafely(eventId, body) {
  try {
    return renderMessageBody(eventId, body);
  } catch (error) {
    console.warn("Ignored malformed event body", eventId, error);
    return `<div class="event-body"><div class="msg-line">${escapeHtml(String(body || ""))}</div></div>`;
  }
}

function renderMessageBody(eventId, body) {
  const text = String(body || "");
  const lines = text.split("\n");
  const expanded = state.expandedEvents.has(eventId);
  if (lines.length <= MESSAGE_COLLAPSE_LINES || expanded) {
    const toggle = lines.length > MESSAGE_COLLAPSE_LINES
      ? `<button class="expand-toggle" type="button" data-collapse-event="${eventId}">收起</button>`
      : "";
    return `<div class="event-body">${renderStructuredBody(text)}</div>${toggle}`;
  }
  return `<div class="event-body is-collapsed">${renderStructuredBody(lines.slice(0, MESSAGE_PREVIEW_LINES).join("\n"))}</div>
    <button class="expand-toggle" type="button" data-expand-event="${eventId}">展开全部（共 ${lines.length} 行）</button>`;
}

function showToast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  const region = elements["toast-region"];
  region.setAttribute("aria-live", type === "error" ? "assertive" : "polite");
  region.append(item);
  setTimeout(() => item.remove(), 3600);
}

function withBusy(work) {
  state.busyCount += 1;
  document.body.setAttribute("aria-busy", "true");
  return Promise.resolve()
    .then(work)
    .finally(() => {
      state.busyCount = Math.max(0, state.busyCount - 1);
      if (state.busyCount === 0) document.body.removeAttribute("aria-busy");
    });
}

function rememberExpandedEvent(eventId) {
  state.expandedEvents.add(Number(eventId));
  if (state.expandedEvents.size <= 80) return;
  const oldest = [...state.expandedEvents][0];
  state.expandedEvents.delete(oldest);
}

function resetProjectFilters() {
  state.taskEntry = "";
  state.taskExpert = { execution: "", verification: "", integration: "", priority: "", owner: "", number: "" };
  state.eventFilter = "all";
  document.querySelectorAll("#task-navigation [data-task-entry]").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.taskEntry === "");
  });
  if (elements["event-filter"]) elements["event-filter"].value = "all";
}

function taskReleaseVisible(task) {
  // 释放按钮只在可释放的执行阶段显示；终态与待验收阶段不换执行者。
  return ["claimed", "in_progress", "blocked"].includes(task.execution_status);
}

function taskCancelVisible(task) {
  // 取消入口与服务端状态转换表一致：done 是终态不可取消，已取消无需重复取消。
  const phase = taskView(task).phase;
  return phase !== "done" && phase !== "cancelled";
}

async function cancelTask(task) {
  elements["task-cancel-button"].disabled = true;
  try {
    await api(`/api/v1/projects/${state.projectId}/tasks/${task.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "cancelled" }),
    });
    await refreshTaskIntakeData();
    const updatedTask = state.snapshot?.tasks.find((item) => item.id === task.id) || task;
    renderTaskContract(updatedTask);
    renderTaskAssignments(updatedTask);
    renderTaskTimeline(updatedTask);
    renderTasks(state.snapshot?.tasks || []);
    showToast(`任务 #${task.task_number} 已取消，历史与审计保留`);
  } catch (error) {
    handleError(error);
    showToast(
      error.code === "invalid_transition"
        ? "任务当前状态不允许取消，请刷新后重试"
        : `取消失败：${error.message}`,
      "error",
    );
  } finally {
    elements["task-cancel-button"].disabled = false;
  }
}

function clearDialogDrafts(dialog) {
  // 嵌套弹窗（如指定 Agent）关闭时不能清掉外层任务详情仍需要的编辑上下文。
  if (["login-dialog", "token-secret-dialog"].includes(dialog.id)) return;
  if (dialog.id === "task-edit-dialog") state.editingTaskId = null;
}

function fetchSnapshotDirect(projectId) {
  const minCursor = Number(arguments[1] || 0);
  if (!projectId) return Promise.resolve(null);
  const request = { projectId, promise: null };
  const cacheBuster = `_=${Date.now()}`;
  const query = minCursor ? `?min_cursor=${minCursor}&${cacheBuster}` : `?${cacheBuster}`;
  request.promise = api(`/api/v1/projects/${projectId}/snapshot${query}`, {
    cache: "no-store",
    headers: { "Cache-Control": "no-cache, no-store" },
  }).finally(() => {
    if (state.snapshotInFlight === request) state.snapshotInFlight = null;
  });
  state.snapshotInFlight = request;
  return request.promise;
}

function refreshSnapshot(projectId) {
  const minCursor = Number(arguments[1] || 0);
  if (!projectId) return Promise.resolve(null);
  if (minCursor > 0 && state.snapshot && Number(state.snapshot.cursor || 0) >= minCursor) {
    return Promise.resolve(state.snapshot);
  }
  if (state.snapshotInFlight && state.snapshotInFlight.projectId === projectId) {
    if (!minCursor) {
      return state.snapshotInFlight.promise;
    }
    return state.snapshotInFlight.promise.then((snapshot) => {
      if (snapshot && Number(snapshot.cursor || 0) >= minCursor) {
        return snapshot;
      }
      return refreshSnapshot(projectId, minCursor);
    });
  }
  return fetchSnapshotDirect(projectId, minCursor);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.error?.message || `请求失败 (${response.status})`);
    error.code = payload?.error?.code || "http_error";
    error.status = response.status;
    if (error.code === "management_auth_required") showLoginDialog();
    throw error;
  }
  return payload;
}

function showLoginDialog() {
  closeEventSource();
  stopPresenceRefresh();
  state.authenticated = false;
  state.projects = [];
  renderProjects();
  renderEmptyRoom();
  elements["logout-button"].classList.add("is-hidden");
  setConnection("offline", "需要登录");
  if (!elements["login-dialog"].open) elements["login-dialog"].showModal();
}

function setConnection(status, label) {
  elements["connection-state"].dataset.state = status;
  elements["connection-label"].textContent = label;
}

async function loadProjects(preferredId) {
  const cachedId = preferredId || localStorage.getItem("agentchatroom.projectId") || state.projectId;
  const result = await api("/api/v1/projects");
  state.projects = result.projects;
  const available = new Set(state.projects.map((project) => project.id));
  let selectedId = cachedId && available.has(cachedId) ? cachedId : null;
  if (cachedId && !available.has(cachedId)) {
    localStorage.removeItem("agentchatroom.projectId");
  }
  if (!selectedId) selectedId = state.projects[0]?.id || null;
  state.projectId = selectedId;
  if (selectedId) {
    localStorage.setItem("agentchatroom.projectId", selectedId);
  } else {
    localStorage.removeItem("agentchatroom.projectId");
  }
  renderProjects();
  if (state.projectId) {
    await selectProject(state.projectId);
  } else {
    renderEmptyRoom();
  }
}

const EVENT_WINDOW_SIZE = 500;

async function loadEventWindow(buildPage, windowSize, { tailJump = true, maxPages = 40 } = {}) {
  let after = 0;
  let collected = [];
  let latest = 0;
  for (let page = 0; page < maxPages; page += 1) {
    const result = await api(buildPage(after, windowSize));
    collected = collected.concat(result.events);
    latest = result.latest_cursor;
    if (result.events.length) after = result.events[result.events.length - 1].id;
    if (result.events.length < windowSize || after >= latest) break;
    if (tailJump && latest - after > windowSize) after = latest - windowSize;
  }
  return { events: collected, cursor: after };
}

async function loadRecentEvents(projectId) {
  return loadEventWindow(
    (after, limit) => `/api/v1/projects/${projectId}/events?after=${after}&limit=${limit}`,
    EVENT_WINDOW_SIZE,
  );
}

function auditPageSize() {
  const configured = Number(state.runtime?.settings?.audit_window_size);
  return Number.isFinite(configured) && configured > 0 ? configured : 100;
}

const AUDIT_PAGE_SIZE = 10;

function auditQueryUrl(projectId, { after = 0, before = 0, eventType = "", limit = AUDIT_PAGE_SIZE } = {}) {
  const filter = eventType ? `&event_type=${encodeURIComponent(eventType)}` : "";
  return `/api/v1/projects/${projectId}/audit?after=${after}&before=${before}&limit=${limit}${filter}`;
}

async function fetchAuditTail(projectId, eventType) {
  const filter = eventType ? `&event_type=${encodeURIComponent(eventType)}` : "";
  const probe = await api(`/api/v1/projects/${projectId}/audit?after=0&limit=1${filter}`);
  return api(auditQueryUrl(projectId, { before: (probe.latest_cursor || 0) + 1, eventType }));
}

async function resetAuditPages(projectId, eventType) {
  const page = await fetchAuditTail(projectId, eventType);
  state.auditEvents = page.events;
  state.auditHasOlder = page.has_older;
  state.auditHasNewer = page.has_newer;
  state.auditFilter = eventType;
  state.auditPage = 1;
}

async function loadOlderAuditEvents() {
  if (!state.projectId || !state.auditHasOlder || !state.auditEvents.length) return;
  const eventType = elements["audit-event-filter"].value;
  const page = await api(auditQueryUrl(state.projectId, {
    before: state.auditEvents[0].id,
    eventType,
  }));
  if (!page.events.length) {
    state.auditHasOlder = false;
    renderAudit();
    return;
  }
  state.auditEvents = page.events;
  state.auditHasOlder = page.has_older;
  state.auditHasNewer = true;
  state.auditPage += 1;
  renderAudit();
}

async function loadNewerAuditEvents() {
  if (!state.projectId || !state.auditHasNewer || !state.auditEvents.length) return;
  const eventType = elements["audit-event-filter"].value;
  const page = await api(auditQueryUrl(state.projectId, {
    after: state.auditEvents[state.auditEvents.length - 1].id,
    eventType,
  }));
  if (!page.events.length) {
    state.auditHasNewer = false;
    renderAudit();
    return;
  }
  state.auditEvents = page.events;
  state.auditHasNewer = page.has_newer;
  state.auditHasOlder = true;
  state.auditPage = Math.max(1, state.auditPage - 1);
  renderAudit();
}

async function selectProject(projectId) {
  const previousId = state.projectId;
  const generation = ++state.selectGeneration;
  closeEventSource();
  stopPresenceRefresh();
  try {
    const [snapshot, eventPage, members, credentials, workspaces, audit, runtime, backups, intakeTargets, intakes] = await Promise.all([
      refreshSnapshot(projectId),
      loadRecentEvents(projectId),
      api(`/api/v1/projects/${projectId}/members`),
      api(`/api/v1/projects/${projectId}/agent-tokens`),
      api(`/api/v1/projects/${projectId}/workspaces`),
      fetchAuditTail(projectId, ""),
      api("/api/v1/admin/runtime?lines=80"),
      api("/api/v1/admin/backups"),
      api(`/api/v1/projects/${projectId}/task-intakes/targets`),
      api(`/api/v1/projects/${projectId}/task-intakes`),
    ]);
    if (generation !== state.selectGeneration) return;
    if (snapshot?.project?.id && snapshot.project.id !== projectId) return;
    state.projectId = projectId;
    localStorage.setItem("agentchatroom.projectId", projectId);
    resetProjectFilters();
    state.events = [];
    state.eventIds = new Set();
    state.snapshot = snapshot;
    state.members = members.members;
    state.credentials = credentials.credentials;
    state.workspaces = workspaces.workspaces;
    state.taskIntakeTargets = intakeTargets.targets || [];
    state.taskIntakes = intakes.intakes || [];
    state.auditEvents = audit.events;
    state.auditHasOlder = audit.has_older;
    state.auditHasNewer = audit.has_newer;
    state.auditFilter = "";
    state.auditPage = 1;
    state.runtime = runtime;
    state.managedBackups = backups.backups || [];
    state.autoBackupInfo = backups.auto_backup || null;
    mergeEvents(eventPage.events);
    renderProjects();
    renderAll();
    connectEvents(eventPage.cursor);
    startPresenceRefresh();
  } catch (error) {
    if (generation !== state.selectGeneration) return;
    if (previousId && previousId !== projectId) {
      showToast(error.message || "项目切换失败，已回到上一个项目", "error");
      await selectProject(previousId);
      return;
    }
    throw error;
  }
}

function mergeEvents(events) {
  for (const event of events) {
    if (!state.eventIds.has(event.id)) {
      state.eventIds.add(event.id);
      state.events.push(event);
    }
  }
  state.events.sort((a, b) => a.id - b.id);
  if (state.events.length > 500) {
    state.events = state.events.slice(-500);
    state.eventIds = new Set(state.events.map((event) => event.id));
  }
}

function closeEventSource() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
}

function stopPresenceRefresh() {
  if (state.presenceTimer) clearInterval(state.presenceTimer);
  state.presenceTimer = null;
}

function startPresenceRefresh() {
  stopPresenceRefresh();
  const interval = Math.max(250, Number(state.config?.presence_refresh_interval_seconds || 1) * 1000);
  state.presenceTimer = setInterval(refreshPresence, interval);
}

async function refreshPresence() {
  if (!state.projectId || state.presenceRefreshInFlight) return;
  const projectId = state.projectId;
  state.presenceRefreshInFlight = true;
  try {
    const snapshot = await refreshSnapshot(projectId);
    if (projectId !== state.projectId || snapshot?.project?.id !== state.projectId) return;
    if (state.snapshot && Number(snapshot.cursor || 0) < Number(state.snapshot.cursor || 0)) return;
    const previousTasksJson = JSON.stringify(state.snapshot?.tasks || []);
    state.snapshot = snapshot;
    state.members = snapshot.members || state.members;
    const agentIdentities = currentAgentRoster(snapshot.agent_identities);
    renderAgents(agentIdentities);
    renderMetrics(agentIdentities, snapshot.tasks, snapshot.leases);
    renderLeases(snapshot.leases, snapshot.agents);
    elements["chat-subtitle"].textContent = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent`;
    elements["chat-subtitle"].title = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent / 累计 ${snapshot.agents.length} 次接入 · 游标 ${snapshot.cursor}`;
    if (JSON.stringify(snapshot.tasks || []) !== previousTasksJson) {
      renderTasks(snapshot.tasks);
      renderTaskIntakes();
      renderReviews(snapshot.tasks, snapshot.agents);
      renderMessageTaskOptions(snapshot.tasks);
    }
  } catch (error) {
    console.warn("Presence refresh failed", error);
    setConnection("offline", "浏览器正在重连");
  } finally {
    state.presenceRefreshInFlight = false;
  }
}

function applySnapshotIfCurrent(projectId, snapshot) {
  if (!snapshot || projectId !== state.projectId || snapshot.project?.id !== state.projectId) {
    return false;
  }
  if (state.snapshot && Number(snapshot.cursor || 0) < Number(state.snapshot.cursor || 0)) {
    return false;
  }
  state.snapshot = snapshot;
  state.members = snapshot.members || state.members;
  return true;
}

function renderForEvent(event) {
  if (!state.snapshot) return renderEmptyRoom();
  const type = String(event?.event_type || "");
  const { agents, tasks, leases } = state.snapshot;
  const agentIdentities = currentAgentRoster(state.snapshot.agent_identities);
  if (type.startsWith("message.")) {
    renderEvents(agents, tasks);
    return;
  }
  if (
    type.startsWith("task.")
    || type.startsWith("work.")
    || type.startsWith("review.")
    || type.startsWith("integration.")
    || type.startsWith("assignment.")
    || type.startsWith("handoff.")
    || type.startsWith("intake.")
  ) {
    renderTasks(tasks);
    if (type.startsWith("task.intake_") || type.startsWith("intake.")) {
      refreshTaskIntakeData().catch(console.warn);
    } else {
      renderTaskIntakes();
    }
    renderReviews(tasks, agents);
    renderMetrics(agentIdentities, tasks, leases);
    renderEvents(agents, tasks);
    renderMessageTaskOptions(tasks);
    elements["chat-subtitle"].textContent = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent`;
    elements["chat-subtitle"].title = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent / 累计 ${state.snapshot.agents.length} 次接入 · 游标 ${state.snapshot.cursor}`;
    return;
  }
  if (type.startsWith("lease.")) {
    renderLeases(leases, agents);
    renderMetrics(agentIdentities, tasks, leases);
    renderEvents(agents, tasks);
    return;
  }
  if (type.startsWith("agent.") || type.startsWith("credential.") || type.startsWith("member.")) {
    renderAgents(agentIdentities);
    renderMetrics(agentIdentities, tasks, leases);
    renderEvents(agents, tasks);
    renderManagement();
    elements["chat-subtitle"].textContent = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent`;
    elements["chat-subtitle"].title = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent / 累计 ${state.snapshot.agents.length} 次接入 · 游标 ${state.snapshot.cursor}`;
    return;
  }
  renderAll();
}

function connectEvents(after) {
  if (!state.projectId) return;
  const projectId = state.projectId;
  setConnection("connecting", "浏览器正在连接");
  const source = new EventSource(`/api/v1/projects/${projectId}/events/stream?after=${after}`);
  state.eventSource = source;
  source.onopen = () => {
    setConnection("online", "浏览器已连接");
    if (state.streamHadError) {
      state.streamHadError = false;
      refreshSnapshot(state.projectId).then((snapshot) => {
        if (!applySnapshotIfCurrent(state.projectId, snapshot)) return;
        renderAll();
      }).catch((error) => showToast(error.message, "error"));
    }
  };
  source.onerror = () => {
    state.streamHadError = true;
    setConnection("offline", "浏览器正在重连");
  };
  source.addEventListener("room_event", async (message) => {
    const capturedProjectId = projectId;
    let event;
    try {
      event = JSON.parse(message.data);
    } catch (error) {
      console.warn("Ignored malformed room event", error);
      return;
    }
    if (capturedProjectId !== state.projectId) return;
    mergeEvents([event]);
    try {
      const minCursor = Number(event?.id || 0);
      const snapshot = await refreshSnapshot(capturedProjectId, minCursor);
      if (!applySnapshotIfCurrent(capturedProjectId, snapshot)) return;
      renderForEvent(event);
    } catch (error) {
      showToast(error.message, "error");
    }
  });
}

function projectGroupKey(project) {
  return String(project.root_path || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase() || "未分组";
}

function renderProjects() {
  elements["project-count"].textContent = state.projects.length;
  if (!state.projects.length) {
    setInnerHtmlIfChanged(elements["project-list"], '<div class="empty-state">还没有项目</div>');
    return;
  }
  const groups = new Map();
  for (const project of state.projects) {
    const key = projectGroupKey(project);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(project);
  }
  const showHeaders = groups.size > 1 || [...groups.values()].some((projects) => projects.length > 1);
  const projectListHtml = [...groups.entries()].map(([groupKey, projects]) => {
    const collapsed = showHeaders && state.collapsedGroups.has(groupKey);
    const items = collapsed ? "" : projects.map((project) => `
      <button class="project-item ${project.id === state.projectId ? "is-active" : ""}" data-project-id="${escapeHtml(project.id)}" type="button"
        title="${escapeHtml(`${project.name}\n${project.root_path}\nID: ${project.id}\n工作区类型: ${projectSource(project)}`)}">
        <span class="project-indicator"></span>
        <span class="project-copy">
          <strong>${escapeHtml(project.name)}</strong>
          <small class="project-identity">${escapeHtml(shortId(project.id))} · ${escapeHtml(projectSource(project))}</small>
          ${showHeaders ? "" : `<small>${escapeHtml(project.root_path)}</small>`}
        </span>
      </button>`).join("");
    const header = showHeaders ? `
      <button class="project-group-header" type="button" data-group-key="${escapeHtml(groupKey)}"
        title="${escapeHtml(`工作空间 ${projects[0].root_path} · 共 ${projects.length} 个 Room`)}">
        <span class="group-chevron">${collapsed ? "▸" : "▾"}</span>
        <span class="group-path">${escapeHtml(projects[0].root_path)}</span>
        <span class="count">${projects.length}</span>
      </button>` : "";
    return `<div class="project-group">${header}${items}</div>`;
  }).join("");
  setInnerHtmlIfChanged(elements["project-list"], projectListHtml);
}

function renderEmptyRoom() {
  stopPresenceRefresh();
  state.snapshot = null;
  state.events = [];
  state.members = [];
  state.credentials = [];
  state.workspaces = [];
  state.taskIntakeTargets = [];
  state.taskIntakes = [];
  state.auditEvents = [];
  elements["room-name"].textContent = "尚未添加项目";
  elements["room-path"].textContent = "添加本地项目后即可开始协作";
  elements["chat-subtitle"].textContent = "等待选择项目";
  elements["onboarding"].classList.remove("is-hidden");
  ["create-task-button", "archive-project-button", "project-settings-button", "export-project-button", "connect-agent-button",
    "create-token-button", "refresh-audit-button", "audit-event-filter", "create-backup-button",
    "event-filter", "message-input", "message-kind", "message-channel", "message-task", "message-priority",
    "message-requires-ack", "send-message-button"]
    .forEach((id) => { elements[id].disabled = true; });
  elements["agent-count"].textContent = "0";
  elements["agent-list"].innerHTML = '<div class="empty-state">Agent 完成「接入 Agent」并连接当前 Room 后，会显示在这里</div>';
  elements["chat-stream"].innerHTML = '<div class="empty-state">Room 动态会实时显示在这里：Agent 加入、任务进展和消息按时间排列</div>';
  elements["task-table"].innerHTML = '<div class="empty-state">还没有正式任务。点右上角「+ 新建任务」提交原始任务意图，等待 Agent 受理和定义。</div>';
  elements["token-list"].innerHTML = '<div class="empty-state">选择项目后管理 Agent Token</div>';
  elements["token-project-context"].textContent = "当前 Project：未选择";
  elements["member-list"].innerHTML = '<div class="empty-state">选择项目后管理项目成员</div>';
  elements["workspace-list"].innerHTML = '<div class="empty-state">选择项目后查看 Workspace</div>';
  elements["audit-list"].innerHTML = '<div class="empty-state">选择项目后查看审计历史</div>';
}

function renderAll() {
  if (!state.snapshot) return renderEmptyRoom();
  const { project, agents, tasks, leases } = state.snapshot;
  const agentIdentities = currentAgentRoster(state.snapshot.agent_identities);
  state.members = state.snapshot.members || state.members;
  state.projects = state.projects.map((item) => item.id === project.id ? project : item);
  renderProjects();
  elements["room-name"].textContent = project.name;
  elements["room-path"].textContent = project.root_path;
  elements["chat-subtitle"].textContent = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent`;
  elements["chat-subtitle"].title = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent / 累计 ${agents.length} 次接入 · 游标 ${state.snapshot.cursor}`;
  elements["onboarding"].classList.add("is-hidden");
  ["create-task-button", "archive-project-button", "project-settings-button", "export-project-button", "connect-agent-button",
    "create-token-button", "refresh-audit-button", "audit-event-filter", "create-backup-button",
    "event-filter", "message-input", "message-kind", "message-channel", "message-task", "message-priority",
    "message-requires-ack", "send-message-button"]
    .forEach((id) => { elements[id].disabled = false; });
  renderAgents(agentIdentities);
  renderMetrics(agentIdentities, tasks, leases);
  renderTasks(tasks);
  renderTaskIntakes();
  renderLeases(leases, agents);
  renderReviews(tasks, agents);
  renderEvents(agents, tasks);
  renderMessageTaskOptions(tasks);
  renderManagement();
}

// 原生 title 提示框在元素被替换的瞬间销毁；presence 轮询每 2 秒全量重建
// 列表会让悬停提示不稳定。内容未变化时跳过 innerHTML 赋值，DOM 保持不动。
function setInnerHtmlIfChanged(element, html) {
  if (element.innerHTML !== html) element.innerHTML = html;
}

function renderAgents(agents) {
  const roster = currentAgentRoster(agents);
  elements["agent-count"].textContent = roster.length;
  const ordered = [...roster].sort((left, right) => {
    const leftDisconnected = left.connection_status === "disconnected" ? 1 : 0;
    const rightDisconnected = right.connection_status === "disconnected" ? 1 : 0;
    return leftDisconnected - rightDisconnected;
  });
  const agentListHtml = ordered.length
    ? ordered.map((agent) => {
      const connected = agent.connection_status === "connected";
      const heartbeat = formatRelativeTime(agent.last_heartbeat);
      const activity = formatRelativeTime(agent.last_activity_at);
      const connectionSummary = connected
        ? `当前已连接 · 累计接入 ${agent.session_count} 次`
        : `已接入 · 未连接 · 累计接入 ${agent.session_count} 次`;
      const presenceStatus = connected ? "online" : "offline";
      const presenceLabel = connected ? "已连接" : "未连接";
      const currentTask = (state.snapshot?.tasks || []).find((task) => task.id === agent.current_task_id);
      const taskSummary = currentTask
        ? `任务：${agent.current_task_title} · ${taskPhaseLabel(currentTask)}`
        : agent.current_task_id
          ? `任务：${agent.current_task_title} · ${legacyStatus(agent.current_task_status)}`
          : "当前无任务";
      // 统一模型标签：显示当前（最近活跃）Session 的模型；后端未上报
      // 或为占位 unknown 时明确显示 unknown，不猜测、不拼接历史模型。
      const modelLabel = String(agent.current_model || "").trim() || "unknown";
      const details = `${agent.name}\n软件: ${agent.client}\n本次角色: ${agent.role}\n模型: ${modelLabel}\n${connectionSummary}\n${taskSummary}\n最后心跳: ${heartbeat}\n最后活动: ${activity}`;
      return `
      <div class="agent-item ${connected ? "" : "is-disconnected"}" title="${escapeHtml(details)}">
        <span class="agent-avatar ${avatarColorClass(agent.id)}">${escapeHtml(initials(agent.name))}</span>
        <span class="agent-copy">
          <strong>${escapeHtml(agent.name)}</strong>
          <small>模型 ${escapeHtml(modelLabel)}</small>
          <small class="agent-task-summary">${escapeHtml(taskSummary)}</small>
        </span>
        <span class="agent-presence ${escapeHtml(presenceStatus)}"><span class="status-dot ${escapeHtml(presenceStatus)}"></span>${escapeHtml(presenceLabel)}</span>
      </div>`;
    }).join("")
    : '<div class="empty-state">等待 Agent 通过 MCP 或 CLI 加入</div>';
  setInnerHtmlIfChanged(elements["agent-list"], agentListHtml);
}

function taskNotFinished(task) {
  const group = taskView(task).group;
  return !["done", "cancelled"].includes(group);
}

const LIFECYCLE_ACTIVITY_TYPES = new Set([
  "agent.joined", "agent.left", "agent.session_replaced", "workspace.updated",
]);

// Aggregate high-frequency session lifecycle events per (type, actor) for the
// overview card only; the append-only event history and the Room feed stay
// complete. Entries keep the position of their latest event.
function mergeLifecycleActivity(events) {
  const entries = [];
  const byKey = new Map();
  for (const event of events) {
    if (!LIFECYCLE_ACTIVITY_TYPES.has(event.event_type)) {
      entries.push({ event });
      continue;
    }
    const actor = event.actor?.name || "";
    const key = `${event.event_type}|${actor}`;
    let entry = byKey.get(key);
    if (!entry) {
      entry = { merged: true, event_type: event.event_type, actor, count: 0, last: event };
      byKey.set(key, entry);
      entries.push(entry);
    }
    entry.count += 1;
    entry.last = event;
  }
  const referenceId = (item) => (item.merged ? item.last.id : item.event.id);
  entries.sort((a, b) => referenceId(a) - referenceId(b));
  return entries;
}

function lifecycleActivitySummary(item) {
  const times = item.count >= 3 ? ` ×${item.count}` : "";
  const who = item.actor ? `${item.actor} ` : "";
  const verbs = {
    "agent.joined": "加入 Room",
    "agent.left": "离开 Room",
    "agent.session_replaced": "替换会话",
    "workspace.updated": "更新工作区登记",
  };
  return `${who}${verbs[item.event_type] || eventLabel(item.event_type)}${times}`;
}

function renderMetrics(agents, tasks, leases) {
  const roster = currentAgentRoster(agents);
  elements["metric-agents"].textContent = roster.length;
  elements["metric-active"].textContent = tasks.filter(taskNotFinished).length;
  elements["metric-leases"].textContent = leases.length;
  elements["metric-reviews"].textContent = tasks.filter((task) => taskView(task).needs_attention).length;

  const active = tasks.filter(taskNotFinished).slice(0, 6);
  elements["active-task-list"].innerHTML = active.length
    ? active.map((task) => `
      <div class="compact-item">
        <div class="task-meta"><span class="task-number">任务 #${task.task_number}</span><span class="priority p${task.priority}">P${task.priority}</span>${taskViewBadgeHtml(taskView(task))}</div>
        <p><strong>${escapeHtml(task.title)}</strong></p>
      </div>`).join("")
    : '<div class="empty-state">当前没有进行中的工作。点「+ 新建任务」把第一件事交给受理 Agent。</div>';

  const projectName = state.snapshot?.project?.name || "";
  if (elements["recent-activity-project"]) {
    elements["recent-activity-project"].textContent = projectName
      ? `当前项目：${projectName} · 实时更新`
      : "实时更新";
  }
  const recent = mergeLifecycleActivity(state.events).slice(-6).reverse();
  elements["recent-event-list"].innerHTML = recent.length
    ? recent.map((item) => {
      if (item.merged && item.count >= 3) {
        return `
      <div class="compact-item merged-activity">
        <div class="compact-heading"><span><strong>${escapeHtml(lifecycleActivitySummary(item))}</strong></span>${eventIdBadge(item.last.project_seq, item.last.id)}</div>
        <div class="compact-body"><div class="msg-line">${escapeHtml(formatTime(item.last.created_at))}</div></div>
      </div>`;
      }
      const event = item.merged ? item.last : item.event;
      const isMessage = event.event_type.startsWith("message.") && event.payload?.body !== undefined;
      const modelBadge = isMessage ? messageModelBadge(event) : "";
      const preview = isMessage
        ? renderMessageLines(String(event.payload.body).split("\n").slice(0, 3))
        : `<div class="msg-line">${escapeHtml(event.payload?.title || event.payload?.path_pattern || formatTime(event.created_at))}</div>`;
      return `
      <div class="compact-item ${isMessage ? `kind-${escapeHtml(event.event_type.split(".")[1])}` : ""}">
        <div class="compact-heading"><span><strong>${escapeHtml(eventLabel(event.event_type))}</strong>${modelBadge}</span>${eventIdBadge(event.project_seq, event.id)}</div>
        <div class="compact-body">${preview}</div>
      </div>`;
    }).join("")
    : '<div class="empty-state">还没有动态。Agent 加入、认领任务、提交报告都会按时间显示在这里。</div>';
}

function taskNavigationEntries(tasks) {
  const config = taskViewConfig();
  const counts = { attention: 0 };
  const attentionIds = new Set();
  for (const task of tasks) {
    const view = taskView(task);
    counts[view.group] = (counts[view.group] || 0) + 1;
    if (view.needs_attention) attentionIds.add(task.id);
  }
  counts.attention = attentionIds.size;
  const entries = [
    { key: "", label: "全部任务", count: tasks.length, kind: "reset" },
    { key: "attention", label: config?.attention_label || "需要处理", count: counts.attention || 0, kind: "attention" },
    ...["claimable", "active", "review", "integration", "done", "cancelled"]
      .map((group) => ({ key: group, label: viewGroupLabel(group), count: counts[group] || 0, kind: "group" })),
  ];
  const unclassified = counts.unclassified || 0;
  if (unclassified) {
    entries.push({ key: "unclassified", label: viewGroupLabel("unclassified"), count: unclassified, kind: "group warning" });
  }
  return entries;
}

function activeSubgroupRank(phase) {
  const order = taskViewConfig()?.active_subgroup_order || ["changes_requested", "blocked", "in_progress", "claimed"];
  const index = order.indexOf(phase);
  return index === -1 ? order.length : index;
}

function sortForEntry(tasks, entry) {
  const sorted = [...tasks];
  if (entry.key === "attention") {
    const phaseOrder = { integration_failed: 0, changes_requested: 1, blocked: 2 };
    sorted.sort((left, right) => {
      const leftPhase = taskView(left).phase;
      const rightPhase = taskView(right).phase;
      return (phaseOrder[leftPhase] ?? 9) - (phaseOrder[rightPhase] ?? 9);
    });
    return sorted;
  }
  if (entry.key === "active") {
    sorted.sort((left, right) => {
      const leftView = taskView(left);
      const rightView = taskView(right);
      return activeSubgroupRank(leftView.phase) - activeSubgroupRank(rightView.phase);
    });
    return sorted;
  }
  return sorted;
}

function applyTaskSort(tasks, sortConfig = state.taskSort) {
  if (!sortConfig || !sortConfig.key) return tasks;
  const copy = [...tasks];
  const dir = sortConfig.direction === "desc" ? -1 : 1;
  if (sortConfig.key === "priority") {
    copy.sort((a, b) => {
      const pa = Number(a.priority ?? 2);
      const pb = Number(b.priority ?? 2);
      if (pa !== pb) return (pa - pb) * dir;
      return Number(b.task_number ?? 0) - Number(a.task_number ?? 0);
    });
  } else if (sortConfig.key === "number") {
    copy.sort((a, b) => {
      const na = Number(a.task_number ?? 0);
      const nb = Number(b.task_number ?? 0);
      return (na - nb) * dir;
    });
  }
  return copy;
}

function matchesEntry(task, entry) {
  const view = taskView(task);
  if (entry.key === "") return true;
  if (entry.key === "attention") return view.needs_attention;
  return view.group === entry.key;
}

function matchesExpert(task, expert) {
  const view = taskView(task);
  if (expert.execution && view.execution_status !== expert.execution) return false;
  if (expert.verification && view.verification_status !== expert.verification) return false;
  if (expert.integration && view.integration_status !== expert.integration) return false;
  if (expert.priority !== "" && String(task.priority) !== String(expert.priority)) return false;
  if (expert.owner && (task.owner_session_id || "") !== expert.owner) return false;
  if (expert.number && String(task.task_number) !== String(expert.number).trim()) return false;
  return true;
}

function renderTaskNavigation(tasks) {
  const container = document.getElementById("task-navigation");
  if (!container) return;
  const entries = taskNavigationEntries(tasks);
  container.innerHTML = entries.map((entry) => `
    <button type="button" class="${entry.kind.startsWith("attention") ? "attention-entry" : entry.kind} ${state.taskEntry === entry.key ? "is-active" : ""}" data-task-entry="${escapeHtml(entry.key)}">
      ${escapeHtml(entry.label)}<span class="entry-count">${entry.count}</span>
    </button>`).join("");
}

function renderTaskExpertFilters(tasks) {
  const container = document.getElementById("task-expert-filters");
  if (!container) return;
  const expert = state.taskExpert;
  const names = Object.fromEntries((state.snapshot?.agents || []).map((agent) => [agent.id, agent.name]));
  const owners = [...new Set(tasks.map((task) => task.owner_session_id).filter(Boolean))]
    .map((id) => `<option value="${escapeHtml(id)}" ${expert.owner === id ? "selected" : ""}>${escapeHtml(names[id] || shortId(id))}</option>`)
    .join("");
  // Exact-phase options carry the same projection-derived counts as the
  // navigation entries (event #2780: every one of the 10 states must be
  // individually selectable AND show its count).
  const phaseCounts = {};
  for (const task of tasks) {
    const phase = taskView(task).phase;
    phaseCounts[phase] = (phaseCounts[phase] || 0) + 1;
  }
  const options = (kind, codes) => codes
    .map((code) => `<option value="${escapeHtml(code)}" ${expert[kind] === code ? "selected" : ""}>${escapeHtml(kind === "phase" ? viewLabel(code) : viewFaceLabel(kind, code))}</option>`)
    .join("");
  const phaseOptions = (codes) => codes
    .map((code) => `<option value="${escapeHtml(code)}" ${expert.phase === code ? "selected" : ""}>${escapeHtml(viewLabel(code))} (${phaseCounts[code] || 0})</option>`)
    .join("");
  const phases = taskViewConfig()?.phases || [];
  const executionCodes = taskViewConfig()?.phases
    ? ["todo", "claimed", "in_progress", "blocked", "completed", "cancelled"]
    : [];
  container.innerHTML = `
    <label>执行<select data-expert="execution"><option value="">全部</option>${options("execution", executionCodes)}</select></label>
    <label>验收<select data-expert="verification"><option value="">全部</option>${options("verification", ["not_required", "pending", "changes_requested", "approved"])}</select></label>
    <label>集成<select data-expert="integration"><option value="">全部</option>${options("integration", ["pending", "done", "failed"])}</select></label>
    <label>优先级<select data-expert="priority"><option value="">全部</option>${[0, 1, 2, 3, 4].map((value) => `<option value="${value}" ${expert.priority !== "" && String(expert.priority) === String(value) ? "selected" : ""}>P${value}</option>`).join("")}</select></label>
    <label>负责人<select data-expert="owner"><option value="">全部</option>${owners}</select></label>
    <label>任务号<input type="search" inputmode="numeric" placeholder="#号" data-expert="number" value="${escapeHtml(expert.number)}"></label>
    ${phases.length ? `<label>精确状态<select data-expert="phase"><option value="">全部</option>${phaseOptions(phases)}</select></label>` : ""}`;
}

function renderTaskSortControls() {
  const container = elements["task-sort-controls"];
  if (!container) return;
  const sort = state.taskSort || { key: "", direction: "asc" };
  const priActive = sort.key === "priority";
  const numActive = sort.key === "number";
  const priLabel = priActive ? (sort.direction === "asc" ? "优先级 P0→P4 ↑" : "优先级 P4→P0 ↓") : "优先级 P0-P4";
  const numLabel = numActive ? (sort.direction === "desc" ? "任务号 #N 降序 ↓" : "任务号 #N 升序 ↑") : "任务号 #N";
  container.innerHTML = `
    <span class="sort-caption secondary-text">排序:</span>
    <button type="button" class="sort-button ${priActive ? "is-active" : ""}" data-task-sort="priority" title="按优先级排序（点击切换升/降序）">${escapeHtml(priLabel)}</button>
    <button type="button" class="sort-button ${numActive ? "is-active" : ""}" data-task-sort="number" title="按任务号排序（点击切换升/降序）">${escapeHtml(numLabel)}</button>
    ${sort.key ? '<button type="button" class="secondary-button sort-reset-button" data-task-sort="reset" title="恢复默认排序">重置</button>' : ""}`;
}

function renderTasks(tasks) {
  renderTaskNavigation(tasks);
  renderTaskSortControls();
  renderTaskExpertFilters(tasks);
  renderTaskTable(tasks);
}

function renderTaskTable(tasks) {
  const entry = taskNavigationEntries(tasks).find((item) => item.key === state.taskEntry) || { key: "" };
  const expert = state.taskExpert;
  const expertActive = Object.values(expert).some((value) => value !== "");
  let filtered = tasks.filter((task) => matchesEntry(task, entry));
  if (expertActive) {
    const exactPhase = expert.phase;
    filtered = filtered.filter((task) => {
      if (!matchesExpert(task, expert)) return false;
      if (exactPhase && taskView(task).phase !== exactPhase) return false;
      return true;
    });
  }
  if (state.taskSort && state.taskSort.key) {
    filtered = applyTaskSort(filtered, state.taskSort);
  } else {
    filtered = sortForEntry(filtered, entry);
  }
  const names = Object.fromEntries((state.snapshot?.agents || []).map((agent) => [agent.id, agent.name]));
  elements["task-table"].innerHTML = filtered.length
    ? filtered.map((task) => {
      const view = taskView(task);
      return `
      <button class="task-row ${view.needs_attention ? "needs-attention" : ""}" type="button" data-task-id="${escapeHtml(task.id)}">
        <span class="task-number">#${task.task_number}</span>
        <div>
          <h3>${escapeHtml(task.title)}</h3>
          <p>${escapeHtml(task.description || task.acceptance_criteria.join(" · ") || "尚未填写正式说明")}${task.current_step ? ` · 当前：${escapeHtml(task.current_step)}` : ""}${task.blocker_reason ? ` · 阻塞：${escapeHtml(task.blocker_reason)}` : ""}</p>
        </div>
        <div class="task-meta">
          <span class="priority p${task.priority}">P${task.priority}</span>
          ${taskViewBadgeHtml(view)}
          <span class="secondary-text">${task.progress_percent}%${task.owner_session_id ? ` · ${escapeHtml(names[task.owner_session_id] || shortId(task.owner_session_id))}` : " · 尚未指定 Agent"}${task.depends_on?.length ? ` · 依赖 ${task.depends_on.length} 项` : ""}</span>
        </div>
      </button>`;
    }).join("")
    : '<div class="empty-state">当前筛选下没有正式任务。新提交的原始意图会显示在下方，等待 Agent 受理和定义。</div>';
}

function intakeTargetName(intake) {
  const target = state.taskIntakeTargets.find((item) => item.member_id === intake.target_member_id);
  return target?.name || intake.target_member_id || "未指定 Agent";
}

function taskIntakeStatus(status) {
  return {
    pending: "待 Agent 受理", accepted: "已受理待定义", defined: "已正式定义",
    declined: "Agent 已拒绝", blocked: "受理受阻", cancelled: "已取消",
  }[status] || status;
}

function taskIntakeTaskReference(intake) {
  if (intake.status !== "defined" || !intake.formal_task_id) return "";
  const task = (state.snapshot?.tasks || []).find((item) => item.id === intake.formal_task_id);
  if (task?.task_number) {
    return `<button class="task-intake-task-ref" type="button" data-task-id="${escapeHtml(intake.formal_task_id)}">正式任务 #${escapeHtml(String(task.task_number))} · ${escapeHtml(task.title)}</button>`;
  }
  return `<span class="task-intake-task-ref secondary-text">正式任务 ${escapeHtml(intake.formal_task_id)}</span>`;
}

function renderTaskIntakes() {
  const activeStatuses = new Set(["pending", "accepted"]);
  const active = state.taskIntakes.filter((intake) => activeStatuses.has(intake.status));
  const archived = state.taskIntakes.filter((intake) => !activeStatuses.has(intake.status));
  const renderItem = (intake) => `
      <article class="task-intake-item">
        <div class="task-intake-meta"><span class="status-badge ${escapeHtml(intake.status)}">${escapeHtml(taskIntakeStatus(intake.status))}</span><span class="secondary-text">受理 Agent：${escapeHtml(intakeTargetName(intake))}</span></div>
        <p>${escapeHtml(intake.raw_description)}</p>
        ${intake.note ? `<small>${escapeHtml(intake.note)}</small>` : ""}
        ${taskIntakeTaskReference(intake)}
      </article>`;
  const heading = '<div class="task-intake-heading"><h3>待受理任务意图</h3><span class="secondary-text">正式标题、优先级、验收条件和依赖由 Agent 定义</span></div>';
  elements["task-intake-list"].innerHTML = [
    heading,
    active.length
      ? active.map(renderItem).join("")
      : '<div class="empty-state">当前没有待受理的任务意图。提交新意图后，目标 Agent 会在这里受理并补全正式任务合同。</div>',
    archived.length
      ? `<details class="task-intake-archive"><summary>意图留档（${archived.length}）· 已定义 / 已拒绝 / 已取消</summary>${archived.map(renderItem).join("")}</details>`
      : "",
  ].join("");
}

function renderLeases(leases, agents) {
  const names = Object.fromEntries(agents.map((agent) => [agent.id, agent.name]));
  const onlineCutoff = Date.now() - 90 * 1000;
  const parseTime = (value) => (value ? Date.parse(value) : 0);
  elements["lease-list"].innerHTML = leases.length
    ? leases.map((lease) => {
      // snapshot 只包含活跃租约；持有者最近无心跳即标记为可回收。
      const holderSeen = parseTime(lease.last_heartbeat || lease.last_activity_at);
      const holderOffline = holderSeen > 0 && holderSeen < onlineCutoff;
      const holder = holderOffline
        ? `${escapeHtml(names[lease.session_id] || shortId(lease.session_id))} · 持有者离线，租约可回收`
        : escapeHtml(names[lease.session_id] || shortId(lease.session_id));
      return `<article class="lease-item">
        <div class="lease-meta">
          <span class="type-badge">${escapeHtml(leaseMode(lease.mode))}</span>
          <strong>${escapeHtml(lease.path_pattern)}</strong>
        </div>
        <p>${holder} · TTL ${lease.ttl_seconds}s · 到期 ${escapeHtml(formatTime(lease.expires_at))}${lease.renewed_at ? ` · 续于 ${escapeHtml(formatTime(lease.renewed_at))}` : ""}${lease.reason ? ` · ${escapeHtml(lease.reason)}` : ""}</p>
      </article>`;
    }).join("")
    : '<div class="empty-state">当前没有活跃文件占用。Agent 编辑文件前会在这里声明路径范围，避免两个 Agent 同时改同一文件。</div>';
}

function renderReviews(tasks, agents) {
  const names = Object.fromEntries(agents.map((agent) => [agent.id, agent.name]));
  const awaiting = tasks.filter((task) => taskView(task).phase === "awaiting_review");
  const completed = state.snapshot.reviews.slice(0, 8);
  const items = [
    ...awaiting.map((task) => ({ type: "awaiting", task })),
    ...completed.map((review) => ({ type: "review", review, task: tasks.find((task) => task.id === review.task_id) })),
  ];
  elements["review-list"].innerHTML = items.length
    ? items.map((item) => item.type === "awaiting" ? `
      <article class="review-item">
        <div class="task-meta"><span class="status-badge ${escapeHtml(taskPhaseClass(item.task))}">${escapeHtml(taskPhaseLabel(item.task))}</span><strong>${escapeHtml(item.task.title)}</strong></div>
        <p>等待独立 Agent 检查 ${item.task.acceptance_criteria.length} 条验收条件</p>
        ${formatSpecReceipt(item.task.spec_receipt) ? `<p>适用规范（认领时版本回执）：${escapeHtml(formatSpecReceipt(item.task.spec_receipt))}</p>` : ""}
        ${renderReportEvidence(item.task.id)}
      </article>` : `
      <article class="review-item">
        <div class="task-meta"><span class="status-badge ${item.review.verdict === "approved" ? "verified" : "blocked"}">${item.review.verdict === "approved" ? "通过" : "退回"}</span><strong>${escapeHtml(item.task?.title || shortId(item.review.task_id))}</strong></div>
        <p>${escapeHtml(names[item.review.reviewer_session_id] || shortId(item.review.reviewer_session_id))} · ${escapeHtml(item.review.notes || `${item.review.criteria.length} 条验收记录`)}</p>
        ${item.task && formatSpecReceipt(item.task.spec_receipt) ? `<p>适用规范（认领时版本回执）：${escapeHtml(formatSpecReceipt(item.task.spec_receipt))}</p>` : ""}
        <div class="criteria-list">${item.review.criteria.map((criterion) => `<span class="criterion ${escapeHtml(criterion.status)}" title="${escapeHtml(criterion.evidence || "")}">${escapeHtml(criterion.status)} · ${escapeHtml(criterion.criterion)}${criterion.evidence ? `<span class="criterion-evidence">证据：${escapeHtml(criterion.evidence)}</span>` : ""}</span>`).join("")}</div>
        ${renderReportEvidence(item.review.task_id)}
      </article>`).join("")
    : '<div class="empty-state">这里是独立验收区。Agent 声明「执行完成」后，需要另一个 Agent 检查测试证据并批准或退回 —— 执行者不能自己批准自己。当前没有待验证的工作。</div>';
}

function permissionLabel(permission) {
  return {
    "room:join": "加入 Room", "room:read": "读取 Room", "message:write": "发布消息",
    "task:write": "管理任务", "lease:write": "管理文件占用", "review:write": "提交验证",
    "integration:write": "提交集成",
    "audit:read": "读取审计",
    "member:read": "读取成员", "member:write": "管理成员",
    "document:write": "管理项目文档",
  }[permission] || permission;
}

function memberStatusLabel(status) {
  return {
    invited: "待邀请", active: "有效", suspended: "已暂停", revoked: "已吊销",
  }[status] || status;
}

function memberStatusClass(status) {
  return {
    invited: "pending", active: "verified", suspended: "blocked", revoked: "cancelled",
  }[status] || "pending";
}

function renderTokenMemberOptions(selected = "") {
  const activeMembers = state.members.filter((member) => member.status === "active");
  elements["token-member"].innerHTML = [
    '<option value="">不关联；首次连接时按下面填写的显示名称自动创建成员</option>',
    ...activeMembers.map((member) => `<option value="${escapeHtml(member.id)}">${escapeHtml(member.name)} · ${escapeHtml(member.member_key)}</option>`),
  ].join("");
  if (activeMembers.some((member) => member.id === selected)) {
    elements["token-member"].value = selected;
  }
}

function syncTokenAgentNameVisibility() {
  // 首次接入且未关联成员时必须由用户给出实际显示名称；增量加入 Project 与
  // 已关联成员场景沿用现有身份，不生成第二套身份，因此不显示该字段。
  const setup = state.pendingHttpSetup;
  const needsAgentName = Boolean(setup) && setup.mode !== "add_project"
    && !elements["token-member"].value;
  elements["token-agent-name-group"].hidden = !needsAgentName;
}

function renderManagement() {
  renderRuntime();
  renderMembers();
  renderCredentials();
  renderWorkspaces();
  renderBackups();
  renderDocuments();
  renderAudit();
}

function backupSizeLabel(size) {
  const kb = Number(size) / 1024;
  if (!Number.isFinite(kb) || kb <= 0) return "-";
  return `${kb.toFixed(kb >= 1024 ? 0 : 1)} KB`;
}

const BACKUP_PAGE_SIZE = 5;

function renderBackups() {
  const autoBackup = state.autoBackupInfo;
  const autoSummary = autoBackup
    ? `自动备份：${autoBackup.enabled ? `已开启 · 每 ${Math.round(autoBackup.interval_seconds / 60)} 分钟 · 保留 ${autoBackup.max_kept} 份` : "未开启（可在项目设置或配置 [backup] 开启）"}`
    : "";
  const total = state.managedBackups.length;
  const pages = Math.max(1, Math.ceil(total / BACKUP_PAGE_SIZE));
  state.backupPage = Math.min(Math.max(1, state.backupPage || 1), pages);
  const summary = autoSummary ? `<p class="secondary-text backup-summary">${escapeHtml(autoSummary)}</p>` : "";
  if (!total) {
    elements["backup-list"].innerHTML = `${summary}<div class="empty-state">还没有备份。点「立即备份」把当前协作数据库另存为快照。</div>`;
    return;
  }
  const rows = state.managedBackups
    .slice((state.backupPage - 1) * BACKUP_PAGE_SIZE, state.backupPage * BACKUP_PAGE_SIZE)
    .map((backup) => {
      const fileName = String(backup.file).split(/[\\/]/).pop();
      return `
      <article class="management-item backup-item">
        <time class="audit-time">${escapeHtml(formatTime(backup.created_at))}</time>
        <div class="backup-meta">
          <span class="backup-file" title="${escapeHtml(backup.file)}">${escapeHtml(fileName)}</span>
          <span class="secondary-text backup-detail">${escapeHtml(`${backup.backend || "-"} · schema v${backup.schema_version ?? "-"} · ${backupSizeLabel(backup.size)} · ${backup.source === "auto" ? "自动" : "手动"}`)}</span>
        </div>
        <div class="management-actions">
          <button type="button" class="secondary-button" data-backup-copy="${escapeHtml(backup.file)}">复制路径</button>
          <button type="button" class="danger-button" data-backup-restore="${escapeHtml(backup.file)}">回滚</button>
          <button type="button" class="danger-button" data-backup-delete="${escapeHtml(fileName)}">删除</button>
        </div>
      </article>`;
    }).join("");
  const pager = total > BACKUP_PAGE_SIZE
    ? `<div class="audit-pager">
        <button type="button" class="secondary-button" data-backup-page="prev" ${state.backupPage <= 1 ? "disabled" : ""}>← 上一页</button>
        <span class="secondary-text">第 ${state.backupPage} / ${pages} 页 · 共 ${total} 份</span>
        <button type="button" class="secondary-button" data-backup-page="next" ${state.backupPage >= pages ? "disabled" : ""}>下一页 →</button>
      </div>`
    : "";
  elements["backup-list"].innerHTML = `${summary}${rows}${pager}`;
}

async function createManagedBackup() {
  if (!state.projectId) return;
  const result = await api("/api/v1/admin/backups", { method: "POST", body: "{}" });
  showToast(`备份已创建：${result.output}`);
  await refreshManagement();
}

async function restoreManagedBackup(backupFile) {
  const first = window.confirm(
    `确定要把整个协作数据库回滚到这份备份吗？\n\n${backupFile}\n\n回滚会丢弃备份之后的所有消息、任务与审计事件，且无法撤销。`
  );
  if (!first) return;
  try {
    await api("/api/v1/admin/backups/restore", {
      method: "POST",
      body: JSON.stringify({ backup_path: backupFile, confirm: "REPLACE" }),
    });
  } catch (error) {
    if (error?.code !== "backup_stale") {
      throw error;
    }
    const second = window.confirm(
      "这份备份落后于当前数据库，回滚将丢失较新的数据。\n\n再次确认：放弃备份之后的全部数据并继续回滚？"
    );
    if (!second) return;
    await api("/api/v1/admin/backups/restore", {
      method: "POST",
      body: JSON.stringify({ backup_path: backupFile, confirm: "REPLACE", allow_data_loss: true }),
    });
  }
  showToast("数据库已回滚，页面将刷新");
  window.setTimeout(() => window.location.reload(), 1200);
}

async function deleteManagedBackup(fileName) {
  const confirmed = window.confirm(
    `确定要删除备份文件 ${fileName} 吗？\n\n删除后不可恢复，对应的元数据快照也会被移除。`
  );
  if (!confirmed) return;
  await api(`/api/v1/admin/backups/${encodeURIComponent(fileName)}`, {
    method: "DELETE",
  });
  showToast(`备份文件已删除：${fileName}`);
  const remaining = (state.managedBackups || []).length - 1;
  const maxPage = Math.max(1, Math.ceil(remaining / BACKUP_PAGE_SIZE));
  if (state.backupPage > maxPage) {
    state.backupPage = maxPage;
  }
  await refreshManagement();
}

function runtimeConfigCards(runtime) {
  const settings = runtime.settings || {};
  const paths = runtime.paths || {};
  const processInfo = runtime.process || {};
  const yesNo = (value) => (value ? "已开启" : "未开启");
  const databaseValue = settings.database_path
    || (settings.database_backend === "postgresql" && settings.database_url_env
      ? `连接串经环境变量 ${settings.database_url_env} 注入（已脱敏）`
      : settings.database_backend || "-");
  const configSource = paths.config_path || settings.config_path
    || "未提供配置文件，使用内置默认值";
  const mcpValue = settings.mcp_http_enabled
    ? `已启用 · ${settings.mcp_http_path || "-"}`
    : "未启用";
  const cards = [
    ["服务地址", settings.host && settings.port ? `${settings.host}:${settings.port}` : "-", `部署形态 ${settings.deployment_profile || "-"}`],
    ["数据库", databaseValue, `类型 ${settings.database_backend || "-"}`],
    ["配置来源", configSource, settings.config_schema_version ? `schema v${settings.config_schema_version}` : ""],
    ["数据目录", paths.data_dir || settings.data_dir || "-"],
    ["日志文件", paths.log_path || "-"],
    ["进程", processInfo.pid ? `PID ${processInfo.pid}${processInfo.managed ? " · 受管" : ""}` : "前台运行（无 PID 文件）"],
    ["管理认证", yesNo(settings.management_auth_required), settings.management_token_env ? `令牌环境变量 ${settings.management_token_env}` : ""],
    ["MCP HTTP", mcpValue, `认证 ${yesNo(settings.mcp_http_auth_required)}`],
  ];
  return cards
    .map(([label, value, hint]) => `
      <div class="config-card">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(String(value ?? "-"))}</strong>
        ${hint ? `<small>${escapeHtml(hint)}</small>` : ""}
      </div>`)
    .join("");
}

function renderRuntime() {
  const runtime = state.runtime;
  if (!runtime) {
    elements["runtime-status"].innerHTML = '<div class="empty-state">暂无运行状态</div>';
    elements["runtime-config"].innerHTML = "";
    elements["runtime-config-raw"].textContent = "";
    elements["runtime-log"].textContent = "";
    return;
  }
  const settings = runtime.settings || {};
  const processInfo = runtime.process || {};
  elements["runtime-status"].innerHTML = `
    <div class="runtime-metric"><span>服务地址</span><strong>${escapeHtml(`${settings.host || "-"}:${settings.port || "-"}`)}</strong></div>
    <div class="runtime-metric"><span>数据库</span><strong>${escapeHtml(settings.database_backend || "-")}</strong></div>
    <div class="runtime-metric"><span>MCP</span><strong>${escapeHtml(settings.mcp_http_path || "-")}</strong></div>
    <div class="runtime-metric"><span>管理认证</span><strong>${settings.management_auth_required ? "已开启" : "未开启"}</strong></div>
    <div class="runtime-metric"><span>进程</span><strong>${escapeHtml(processInfo.pid || "前台运行")}</strong></div>`;
  elements["runtime-config"].innerHTML = runtimeConfigCards(runtime);
  elements["runtime-config-raw"].textContent = JSON.stringify({
    settings,
    paths: runtime.paths,
  }, null, 2);
  elements["runtime-log"].textContent = runtime.log?.lines?.join("\n") || "暂无日志";
}

function renderMembers() {
  elements["member-list"].innerHTML = state.members.length
    ? state.members.map((member) => `
      <article class="management-item">
        <div>
          <h4>${escapeHtml(member.name)} <span class="status-badge ${memberStatusClass(member.status)}">${escapeHtml(memberStatusLabel(member.status))}</span></h4>
          <p>${escapeHtml(member.member_key)} · ${escapeHtml(member.kind)}${member.role ? ` · 角色 ${escapeHtml(member.role)}` : ""}</p>
          <p>${member.credential_count || 0} 个 Token · 累计接入 ${member.session_count || 0} 次 · 更新于 ${escapeHtml(formatTime(member.updated_at))}</p>
        </div>
        <div class="management-actions">
          ${member.status !== "revoked" ? `<button type="button" class="danger-button" data-member-action="revoke" data-member-id="${escapeHtml(member.id)}">吊销</button>` : ""}
        </div>
      </article>`).join("")
    : '<div class="empty-state">尚未登记项目成员</div>';
  renderTokenMemberOptions(elements["token-member"].value);
}

function renderCredentials() {
  const memberNames = Object.fromEntries(state.members.map((member) => [member.id, member.name]));
  const projectName = state.snapshot?.project?.name || "未选择";
  elements["token-project-context"].textContent = `当前 Project：${projectName}`;
  elements["token-list"].innerHTML = state.credentials.length
    ? state.credentials.map((credential) => {
      const manageable = !credential.revoked_at;
      return `
      <article class="management-item">
        <div>
          <h4>${escapeHtml(credential.name)} <span class="status-badge ${credential.active ? "verified" : "cancelled"}">${credential.active ? "有效" : "已失效"}</span></h4>
          <p><strong>所属 Project：${escapeHtml(projectName)}</strong></p>
          ${credential.member_id ? `<p>成员 ${escapeHtml(memberNames[credential.member_id] || shortId(credential.member_id))}</p>` : ""}
          <p>到期 ${escapeHtml(formatTime(credential.expires_at))}${credential.last_used_at ? ` · 最近使用 ${escapeHtml(formatTime(credential.last_used_at))}` : " · 尚未使用"}</p>
        </div>
        <div class="management-actions">
          ${manageable ? `<button type="button" class="secondary-button" data-token-action="permissions" data-credential-id="${escapeHtml(credential.id)}">修改权限</button>
          <button type="button" class="secondary-button" data-token-action="extend" data-credential-id="${escapeHtml(credential.id)}">续期</button>
          <button type="button" class="danger-button" data-token-action="revoke" data-credential-id="${escapeHtml(credential.id)}">吊销</button>` : ""}
        </div>
      </article>`;
    }).join("")
    : '<div class="empty-state">尚未签发 Agent Token</div>';
}

function renderWorkspaces() {
  elements["workspace-list"].innerHTML = state.workspaces.length
    ? state.workspaces.map((workspace) => `
      <article class="management-item">
        <div>
          <h4>${escapeHtml(workspace.host_name)} · ${escapeHtml(workspace.local_path)}</h4>
          <p>${workspace.branch ? `分支 ${escapeHtml(workspace.branch)} · ` : ""}${workspace.worktree ? `Worktree ${escapeHtml(workspace.worktree)} · ` : ""}${escapeHtml(workspace.host_key)}</p>
        </div>
        <span class="secondary-text">${escapeHtml(formatTime(workspace.updated_at))}</span>
      </article>`).join("")
    : '<div class="empty-state">Agent 远程加入后会自动登记 Workspace</div>';
}

function documentCacheEntry(docKey, version) {
  const cached = state.documentContentCache[docKey];
  return cached && String(cached.version) === String(version) ? cached : null;
}

function documentBodyHtml(doc) {
  const cached = documentCacheEntry(doc.doc_key, doc.version);
  if (cached) {
    return `<pre class="code-block project-doc-content" data-doc-content="${escapeHtml(doc.doc_key)}">${escapeHtml(cached.content)}</pre>`;
  }
  return `<div class="loading-state" data-doc-content="${escapeHtml(doc.doc_key)}">正在加载文档…</div>`;
}

function documentHistoryText(loaded) {
  return loaded.history && loaded.history.length
    ? `版本历史：${loaded.history.map((item) => `v${item.version}（${formatTime(item.created_at)}）`).join(" · ")}`
    : "";
}

function renderDocuments() {
  const docs = state.snapshot?.documents || [];
  elements["document-list"].innerHTML = docs.length
    ? docs.map((doc) => `
      <details class="project-doc" data-doc-key="${escapeHtml(doc.doc_key)}">
        <summary>
          <span class="type-badge ${doc.kind === "binding" ? "verified" : ""}">${doc.kind === "binding" ? "规范" : "参考"}</span>
          <strong>${escapeHtml(doc.title)}</strong>
          <span class="secondary-text">v${escapeHtml(String(doc.version))} · ${escapeHtml(String(doc.size))}B</span>
        </summary>
        <div class="project-doc-body">
          ${documentBodyHtml(doc)}
          <div class="doc-history secondary-text">${escapeHtml(documentHistoryText(documentCacheEntry(doc.doc_key, doc.version) || {}))}</div>
        </div>
      </details>`).join("")
    : '<div class="empty-state">还没有项目文档。规范（binding）会在 Agent 认领任务时自动注入上下文并盖版本回执。</div>';
  docs.forEach((doc) => {
    if (!documentCacheEntry(doc.doc_key, doc.version)) refreshDocumentBody(doc).catch(() => {});
  });
}

function patchDocumentBody(loaded) {
  const container = elements["document-list"].querySelector(`[data-doc-content="${loaded.doc_key}"]`);
  if (!container) return;
  if (!container.classList.contains("project-doc-content")) {
    const pre = document.createElement("pre");
    pre.className = "code-block project-doc-content";
    pre.dataset.docContent = loaded.doc_key;
    pre.textContent = loaded.content;
    container.replaceWith(pre);
  }
  const history = container.closest(".project-doc")?.querySelector(".doc-history");
  if (history) history.textContent = documentHistoryText(loaded);
}

async function refreshDocumentBody(doc) {
  const result = await api(`/api/v1/projects/${state.projectId}/documents/${encodeURIComponent(doc.doc_key)}`);
  const loaded = result.document;
  state.documentContentCache[doc.doc_key] = loaded;
  patchDocumentBody(loaded);
  return loaded;
}

function showDocumentError(docKey, error) {
  const container = elements["document-list"].querySelector(`[data-doc-content="${docKey}"]`);
  if (!container) return;
  const box = document.createElement("div");
  box.className = "state-box error-state";
  box.dataset.docContent = docKey;
  box.innerHTML = `<span class="state-title">文档加载失败</span><span>${escapeHtml(error.message || "未知错误")}</span><button type="button" class="secondary-button state-retry" data-doc-retry="${escapeHtml(docKey)}">重试</button>`;
  container.replaceWith(box);
}

function wireDocumentList() {
  elements["document-list"].addEventListener("click", async (event) => {
    const retryButton = event.target.closest("[data-doc-retry]");
    if (retryButton) {
      const docKey = retryButton.dataset.docRetry;
      const doc = (state.snapshot?.documents || []).find((item) => item.doc_key === docKey);
      const loading = document.createElement("div");
      loading.className = "loading-state";
      loading.dataset.docContent = docKey;
      loading.textContent = "正在加载文档…";
      retryButton.closest(".state-box").replaceWith(loading);
      if (doc) refreshDocumentBody(doc).catch((error) => showDocumentError(docKey, error));
      return;
    }
  });
}

function auditPager() {
  // 列表按新→旧展示：视觉向下翻页加载更旧事件并递增页码，
  // 视觉向上翻页加载更新事件，与阅读顺序保持一致。
  return `<div class="audit-pager">
    <button type="button" class="secondary-button" data-audit-action="newer" ${state.auditHasNewer ? "" : "disabled"}>← 上一页</button>
    <span class="secondary-text">第 ${state.auditPage || 1} 页 · 每页 ${AUDIT_PAGE_SIZE} 条</span>
    <button type="button" class="secondary-button" data-audit-action="older" ${state.auditHasOlder ? "" : "disabled"}>下一页 →</button>
  </div>`;
}

function renderAudit() {
  const items = state.auditEvents.length
    ? state.auditEvents.slice().reverse().map((event) => `
      <article class="management-item">
        <time class="audit-time">${escapeHtml(formatTime(event.created_at))}</time>
        <div>
          <h4>${escapeHtml(eventLabel(event.event_type))} ${eventIdBadge(event.project_seq, event.id)}</h4>
          <p>${escapeHtml(event.task_id ? `任务 ${shortId(event.task_id)}` : event.actor_session_id ? `接入 ${shortId(event.actor_session_id)}` : "管理主体")}</p>
        </div>
      </article>`).join("")
    : '<div class="empty-state">当前筛选下没有审计事件</div>';
  elements["audit-list"].innerHTML = `${auditPager()}${items}`;
}

async function refreshManagement() {
  if (!state.projectId) return;
  const eventType = elements["audit-event-filter"].value;
  const auditRefresh = eventType === state.auditFilter && state.auditEvents.length
    ? api(auditQueryUrl(state.projectId, {
        after: state.auditEvents[0].id - 1,
        before: state.auditEvents[state.auditEvents.length - 1].id + 1,
        eventType,
      }))
    : fetchAuditTail(state.projectId, eventType);
  const [members, credentials, workspaces, audit, runtime, backups] = await Promise.all([
    api(`/api/v1/projects/${state.projectId}/members`),
    api(`/api/v1/projects/${state.projectId}/agent-tokens`),
    api(`/api/v1/projects/${state.projectId}/workspaces`),
    auditRefresh,
    api("/api/v1/admin/runtime?lines=80"),
    api("/api/v1/admin/backups"),
  ]);
  state.members = members.members;
  state.credentials = credentials.credentials;
  state.workspaces = workspaces.workspaces;
  if (eventType !== state.auditFilter) {
    state.auditFilter = eventType;
    state.auditPage = 1;
  }
  state.auditEvents = audit.events;
  state.auditHasOlder = audit.has_older;
  state.auditHasNewer = audit.has_newer;
  state.runtime = runtime;
  state.managedBackups = backups.backups || [];
  state.autoBackupInfo = backups.auto_backup || null;
  renderManagement();
}

function renderReportEvidence(taskId) {
  const report = state.snapshot?.reports.find((item) => item.task_id === taskId);
  if (!report) return '<div class="evidence-block secondary-text">尚未找到工作报告</div>';
  const tests = report.tests.map((test) => `${test.exit_code === 0 ? "通过" : "失败"} · ${test.command}`).join("；");
  const git = report.system_evidence || {};
  const fileEvidence = report.files.length
    ? escapeHtml(report.files.join("、"))
    : `<strong>无代码变更</strong> · ${escapeHtml(report.no_code_change_reason || "未说明原因")}`;
  return `<details class="evidence-block">
    <summary>查看工作证据</summary>
    <p><strong>摘要</strong> ${escapeHtml(report.summary)}</p>
    <p><strong>文件</strong> ${fileEvidence}</p>
    <p><strong>测试</strong> ${escapeHtml(tests)}</p>
    <p><strong>Git</strong> ${git.captured ? `${escapeHtml(git.branch || "detached")} · ${git.head && git.head !== "HEAD" ? `HEAD ${escapeHtml(shortId(git.head))}` : "暂无 commit"}${report.commit_hash ? ` · commit ${escapeHtml(shortId(report.commit_hash))}` : ""}` : escapeHtml(git.reason || "未采集")}</p>
  </details>`;
}

function visibleFeedEvents(events) {
  // Room 动态唯一的展示层过滤入口：首屏、实时追加、手动刷新与项目切换
  // 都经由 renderEvents 走这里。只影响展示，不触碰 append-only 事件历史。
  let filtered = events;
  if (state.hideSystemFeedEvents) {
    filtered = filtered.filter((event) =>
      ["message.message", "message.decision", "message.blocker"].includes(event.event_type));
  }
  if (state.eventFilter === "messages") filtered = filtered.filter((event) => event.event_type.startsWith("message.") && event.payload?.body !== undefined);
  if (state.eventFilter === "decisions") filtered = filtered.filter((event) => ["message.decision", "message.blocker"].includes(event.event_type));
  return filtered;
}

function renderEvents(agents, tasks) {
  const agentMap = Object.fromEntries(agents.map((agent) => [agent.id, agent]));
  const taskMap = Object.fromEntries(tasks.map((task) => [task.id, task]));
  let events = visibleFeedEvents([...state.events]);
  events = events.slice(-120);
  const stream = elements["chat-stream"];
  const streamVisible = stream.offsetParent !== null && stream.clientHeight > 0;
  const distanceFromBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
  const wasAtBottom = streamVisible && distanceFromBottom < 40;
  const previousScrollTop = stream.scrollTop;
  const latestEventId = events.length ? events[events.length - 1].id : 0;
  const hasNewEvents = latestEventId > state.lastRenderedEventId && state.lastRenderedEventId > 0;
  stream.innerHTML = events.length
    ? events.map((event) => {
      const agent = agentMap[event.actor_session_id];
      const task = taskMap[event.task_id];
      if (event.event_type.startsWith("message.") && event.payload?.body !== undefined) {
        const kind = event.event_type.split(".")[1];
        const ackCount = state.snapshot.acknowledgements.filter((item) => item.event_id === event.id).length;
        return `<article class="event-item kind-${escapeHtml(kind)}">
          <span class="event-avatar ${avatarColorClass(event.actor_session_id || agent?.name)}">${escapeHtml(initials(agent?.name || "用户"))}</span>
          <div class="event-content">
            <div class="event-meta"><span class="event-author"><strong>${escapeHtml(agent?.name || "用户")}</strong>${messageModelBadge(event)}${kind !== "message" ? ` <span class="type-badge ${escapeHtml(kind)}">${escapeHtml(messageKind(kind))}</span>` : ""}${event.payload?.priority <= 1 ? ` <span class="type-badge urgent">${event.payload.priority === 0 ? "紧急" : "高优先级"}</span>` : ""}</span><span class="event-stamp">${eventIdBadge(event.project_seq, event.id)}<time title="${escapeHtml(event.created_at)}">${escapeHtml(formatTime(event.created_at))}</time></span></div>
            ${renderMessageBodySafely(event.id, event.payload?.body || "")}
            <p class="secondary-text">${escapeHtml(messageChannel(event.channel))}频道${event.payload?.requires_ack ? ` · 需要确认 · 已确认 ${ackCount}` : ""}${event.payload?.mentions?.length ? ` · @${escapeHtml(event.payload.mentions.join("、"))}` : ""}</p>
            ${event.payload?.files?.length ? `<p class="secondary-text">关联文件：${escapeHtml(event.payload.files.join("、"))}</p>` : ""}
            ${task ? `<p class="secondary-text">关联任务：${escapeHtml(task.title)}</p>` : ""}
          </div>
        </article>`;
      }
      const conflict = event.event_type === "lease.conflict"
        ? ` · ${escapeHtml(event.payload?.path_pattern || "")} 与 ${escapeHtml((event.payload?.conflicts || []).map((item) => `${item.agent_name}:${item.path_pattern}`).join("、"))} 冲突`
        : "";
      return `<div class="system-event">${eventIdBadge(event.project_seq, event.id)} <strong>${escapeHtml(agent?.name || "系统")}</strong> ${escapeHtml(eventLabel(event.event_type))}${task ? ` · ${escapeHtml(task.title)} · ${escapeHtml(taskPhaseLabel(task))}` : ""}${conflict} <span>· ${escapeHtml(formatTime(event.created_at))}</span></div>`;
    }).join("")
    : '<div class="empty-state">当前筛选下没有动态</div>';
  const notice = elements["new-message-notice"];
  if (!streamVisible) {
    if (hasNewEvents) notice.classList.remove("is-hidden");
  } else if (wasAtBottom) {
    stream.scrollTop = stream.scrollHeight;
    notice.classList.add("is-hidden");
  } else {
    stream.scrollTop = previousScrollTop;
    if (hasNewEvents) notice.classList.remove("is-hidden");
  }
  state.lastRenderedEventId = latestEventId;
}

document.getElementById("add-project-button").addEventListener("click", () => elements["project-dialog"].showModal());
elements["project-folder-picker-button"].addEventListener("click", async () => {
  const button = elements["project-folder-picker-button"];
  const input = elements["project-path-input"];
  button.disabled = true;
  button.textContent = "正在选择...";
  try {
    const result = await withBusy(() => api("/api/v1/local/folders/pick", {
      method: "POST",
      body: JSON.stringify({ initial_path: input.value.trim() }),
    }));
    if (!result.cancelled && result.path) {
      input.value = result.path;
      input.focus();
    }
  } catch (error) {
    if (error.code === "local_folder_picker_unavailable") {
      showToast("无法打开系统文件夹选择器，请手动输入项目路径", "error");
    } else {
      handleError(error);
    }
  } finally {
    button.textContent = "选择文件夹";
    button.disabled = !state.config?.capabilities?.local_folder_picker;
  }
});
document.getElementById("create-task-button").addEventListener("click", async () => {
  if (!state.projectId) return;
  await refreshTaskIntakeTargets();
  elements["task-dialog"].showModal();
});
document.getElementById("archive-project-button").addEventListener("click", () => {
  if (!state.snapshot) return;
  elements["archive-project-name"].textContent = state.snapshot.project.name;
  elements["permanent-delete-input"].checked = false;
  elements["remove-project-submit"].textContent = "确认归档";
  elements["archive-dialog"].showModal();
});
document.getElementById("project-settings-button").addEventListener("click", () => {
  if (!state.snapshot) return;
  const { project } = state.snapshot;
  elements["settings-project-name"].value = project.name;
  elements["settings-lease-policy"].value = project.settings.lease_conflict_policy;
  elements["settings-roles"].value = project.settings.roles.join("\n");
  elements["settings-default-priority"].value = String(
    project.settings.default_task_priority ?? 2,
  );
  elements["settings-mcp-message-limit"].value = String(
    project.settings.mcp_message_limit ?? 5,
  );
  elements["settings-audit-retention"].value = String(
    project.settings.audit_retention_days ?? 0,
  );
  elements["settings-dialog"].showModal();
  api("/api/v1/admin/backup-settings").then((settings) => {
    elements["settings-auto-backup"].checked = !!settings.auto_backup_enabled;
    elements["settings-backup-max-kept"].value = settings.auto_backup_max_kept ?? 10;
    elements["settings-backup-hint"].textContent = `${settings.effective || ""} · 配置文件：${settings.config_path || "-"}`;
  }).catch(handleError);
});
document.getElementById("export-project-button").addEventListener("click", () => downloadProjectExport().catch(handleError));
document.getElementById("refresh-button").addEventListener("click", () => {
  if (!state.projectId) return;
  withBusy(() => selectProject(state.projectId)).catch(handleError);
});
elements["connect-agent-button"].addEventListener("click", () => openIntegrationDialog().catch(handleError));
elements["logout-button"].addEventListener("click", async () => {
  try {
    await api("/api/v1/auth/logout", { method: "POST", body: "{}" });
  } finally {
    showLoginDialog();
  }
});
elements["create-token-button"].addEventListener("click", () => {
  state.pendingHttpSetup = null;
  const permissions = state.config?.domain?.agent_permissions || [];
  const defaults = new Set(permissions.filter((permission) => permission !== "audit:read"));
  elements["token-permissions"].innerHTML = permissions.map((permission) => `
    <label class="check-control">
      <input type="checkbox" value="${escapeHtml(permission)}" ${defaults.has(permission) ? "checked" : ""}>
      <span>${escapeHtml(permissionLabel(permission))}</span>
    </label>`).join("");
  elements["token-form"].reset();
  elements["token-existing-config-advanced"].hidden = true;
  elements["token-agent-name-group"].hidden = true;
  elements["token-agent-name"].value = "";
  elements["token-project-credential-name"].value = "";
  elements["token-existing-config"].value = "";
  elements["token-dialog-context"].textContent = "Agent 凭据";
  elements["token-dialog-title"].textContent = "签发 Token";
  elements["token-submit-button"].textContent = "签发";
  renderTokenMemberOptions();
  elements["token-days"].value = "30";
  elements["token-dialog"].showModal();
});
elements["token-member"].addEventListener("change", () => syncTokenAgentNameVisibility());
elements["refresh-audit-button"].addEventListener("click", () => refreshManagement().catch(handleError));
elements["audit-event-filter"].addEventListener("change", () => refreshManagement().catch(handleError));
elements["audit-list"].addEventListener("click", (event) => {
  const button = event.target.closest("[data-audit-action]");
  if (!button) return;
  (button.dataset.auditAction === "older" ? loadOlderAuditEvents() : loadNewerAuditEvents())
    .catch(handleError);
});
elements["refresh-runtime-button"].addEventListener("click", () => refreshManagement().catch(handleError));
wireDocumentList();
elements["create-backup-button"].addEventListener("click", () => createManagedBackup().catch(handleError));
elements["backup-list"].addEventListener("click", (event) => {
  const pageButton = event.target.closest("[data-backup-page]");
  if (pageButton) {
    state.backupPage += pageButton.dataset.backupPage === "prev" ? -1 : 1;
    renderBackups();
    return;
  }
  const copy = event.target.closest("[data-backup-copy]");
  if (copy) {
    copyText(copy.dataset.backupCopy).then(() => showToast("备份路径已复制")).catch(handleError);
    return;
  }
  const restore = event.target.closest("[data-backup-restore]");
  if (restore) restoreManagedBackup(restore.dataset.backupRestore).catch(handleError);
  const del = event.target.closest("[data-backup-delete]");
  if (del) deleteManagedBackup(del.dataset.backupDelete).catch(handleError);
});
elements["task-assign-button"].addEventListener("click", () => openTaskAssignmentDialog().catch(handleError));

elements["task-cancel-button"].addEventListener("click", () => {
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task || !state.projectId) return;
  const view = taskView(task);
  const occupied = ["claimed", "in_progress", "blocked"].includes(task.execution_status);
  const cancelNote =
    "取消任务 #" + task.task_number + "「" + task.title + "」？\n\n" +
    "当前状态：" + view.phase + "\n" +
    (occupied
      ? "该任务有执行中的 Agent：取消会立即释放其文件占用并撤销待确认指派。\n"
      : "") +
    "取消后将保留全部历史与审计记录，停止后续执行。确定要取消吗？";
  const confirmed = window.confirm(cancelNote);
  if (!confirmed) return;
  cancelTask(task);
});

elements["task-release-button"].addEventListener("click", () => {
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task || !state.projectId) return;
  elements["task-release-title"].textContent = `释放任务 #${task.task_number}`;
  elements["task-release-reason"].value = "";
  elements["task-release-reason-text"].value = "";
  elements["task-release-dialog"].showModal();
});

elements["task-release-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task || !state.projectId) return;
  const reasonCode = elements["task-release-reason"].value;
  if (!reasonCode) {
    showToast("请选择释放原因", "error");
    return;
  }
  elements["task-release-submit"].disabled = true;
  try {
    await api(`/api/v1/projects/${state.projectId}/tasks/${task.id}/release`, {
      method: "POST",
      body: JSON.stringify({
        reason_code: reasonCode,
        reason: elements["task-release-reason-text"].value.trim(),
      }),
    });
    elements["task-release-dialog"].close();
    await refreshTaskIntakeData();
    const updatedTask = state.snapshot?.tasks.find((item) => item.id === task.id) || task;
    renderTaskContract(updatedTask);
    renderTaskAssignments(updatedTask);
    renderTaskTimeline(updatedTask);
    showToast(`任务 #${task.task_number} 已释放回待认领，进度和历史保留`);
  } catch (error) {
    handleError(error);
  } finally {
    elements["task-release-submit"].disabled = false;
  }
});
elements["token-secret-close"].addEventListener("click", () => {
  elements["token-secret-value"].textContent = "";
  elements["token-secret-dialog"].close();
});

elements["token-secret-dialog"].addEventListener("close", () => {
  state.issuedHttpSetup = null;
  elements["token-secret-section"].hidden = false;
  elements["token-secret-copy"].hidden = false;
  elements["token-secret-context"].textContent = "一次性 Secret";
  elements["token-secret-title"].textContent = "保存 Agent Token";
  elements["token-secret-value"].textContent = "";
  elements["token-config-value"].textContent = "";
  elements["token-config-path"].textContent = "";
  elements["token-config-projects"].textContent = "";
  elements["token-existing-config"].value = "";
  elements["token-config-section"].hidden = true;
});

elements["token-dialog"].addEventListener("close", () => {
  state.pendingHttpSetup = null;
  elements["token-existing-config"].value = "";
  elements["token-project-credential-name"].value = "";
  elements["token-existing-config-advanced"].hidden = true;
});

elements["token-config-format"].addEventListener("change", () => {
  renderIssuedHttpConfig();
});

document.querySelectorAll(".dialog-close").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
});

document.getElementById("integration-onboarding-mode").addEventListener("change", (event) => {
  state.integrationOnboardingMode = event.target.value;
  if (state.integrationOnboardingMode === "migrate_http") {
    state.integrationTransport = "http";
    document.querySelectorAll("[data-integration-transport]").forEach((item) => {
      item.classList.toggle("is-active", item.dataset.integrationTransport === "http");
    });
    renderIntegrationConfig();
  }
  state.integrationLocalRequest += 1;
  state.integrationLocalPlan = null;
  renderOnboardingPrompt();
  renderHttpTokenGuide();
  renderLocalMcpPlan();
  if (state.integrationOnboardingMode === "first_setup") void refreshLocalMcpPlan();
});

document.querySelector(".integration-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-integration-format]");
  if (!button) return;
  state.integrationFormat = button.dataset.integrationFormat;
  document.querySelectorAll("[data-integration-format]").forEach((item) => {
    item.classList.toggle("is-active", item === button);
  });
  renderIntegrationConfig();
  renderProjectInstructions();
  renderOnboardingPrompt();
  state.integrationLocalPlan = null;
  state.integrationLocalApplyResult = null;
  renderLocalMcpPlan();
  void refreshLocalMcpPlan();
});

elements["integration-transport-tabs"].addEventListener("click", (event) => {
  const button = event.target.closest("[data-integration-transport]");
  if (!button) return;
  state.integrationTransport = button.dataset.integrationTransport;
  document.querySelectorAll("[data-integration-transport]").forEach((item) => {
    item.classList.toggle("is-active", item === button);
  });
  renderIntegrationConfig();
  renderOnboardingPrompt();
  state.integrationLocalPlan = null;
  state.integrationLocalApplyResult = null;
  renderLocalMcpPlan();
  if (state.integrationTransport === "local") void refreshLocalMcpPlan();
});

elements["integration-open-token-button"].addEventListener("click", () => {
  const profile = state.integration?.profiles?.[state.integrationFormat];
  if (!profile) return;
  const mode = state.integrationOnboardingMode;
  const projectName = state.snapshot?.project?.name || "";
  const setup = {projectId: state.projectId, projectName, profile: {...profile},
    profiles: state.integration.profiles, format: state.integrationFormat, mode};
  if (mode === "reconnect") {
    elements["integration-dialog"].close();
    showIssuedHttpResult(setup);
    return;
  }
  elements["integration-dialog"].close();
  elements["create-token-button"].click();
  state.pendingHttpSetup = setup;
  const isAddProject = mode === "add_project";
  elements["token-existing-config-advanced"].hidden = !isAddProject;
  elements["token-project-credential-name"].value = projectName;
  elements["token-name"].value = `${projectName} · ${profile.label || "Agent"} HTTP`;
  elements["token-agent-name"].value = concreteIdentityValue(profile.software_name);
  elements["token-dialog-context"].textContent = isAddProject ? "加入当前 Project（增量）" : "首次接入";
  elements["token-dialog-title"].textContent = isAddProject ? "签发本项目 Token 并生成增量提示词" : "签发首次 HTTP 凭据";
  elements["token-submit-button"].textContent = isAddProject ? "签发并生成增量提示词" : "签发并生成接入提示词";
  const member = state.members.find((item) => item.active !== false
    && item.metadata?.software_key === profile.software_key);
  if (member) elements["token-member"].value = member.id;
  syncTokenAgentNameVisibility();
});

elements["integration-local-refresh"].addEventListener("click", () => {
  void refreshLocalMcpPlan(true);
});

elements["integration-local-apply"].addEventListener("click", () => {
  void applyLocalMcpPlan();
});

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    try {
      await copyText(target.textContent);
      showToast("已复制到剪贴板");
    } catch (error) {
      handleError(error);
    }
  });
});

elements["project-list"].addEventListener("click", (event) => {
  const groupHeader = event.target.closest("[data-group-key]");
  if (groupHeader) {
    const key = groupHeader.dataset.groupKey;
    if (state.collapsedGroups.has(key)) {
      state.collapsedGroups.delete(key);
    } else {
      state.collapsedGroups.add(key);
    }
    renderProjects();
    return;
  }
  const button = event.target.closest("[data-project-id]");
  if (button) withBusy(() => selectProject(button.dataset.projectId)).catch(handleError);
});

elements["chat-stream"].addEventListener("click", (event) => {
  const expand = event.target.closest("[data-expand-event]");
  if (expand) {
    rememberExpandedEvent(expand.dataset.expandEvent);
    if (state.snapshot) renderEvents(state.snapshot.agents, state.snapshot.tasks);
    return;
  }
  const collapse = event.target.closest("[data-collapse-event]");
  if (collapse) {
    state.expandedEvents.delete(Number(collapse.dataset.collapseEvent));
    if (state.snapshot) renderEvents(state.snapshot.agents, state.snapshot.tasks);
  }
});

elements["chat-stream"].addEventListener("scroll", () => {
  const stream = elements["chat-stream"];
  if (stream.scrollHeight - stream.scrollTop - stream.clientHeight < 40) {
    elements["new-message-notice"].classList.add("is-hidden");
  }
});

elements["new-message-notice"].addEventListener("click", () => {
  const stream = elements["chat-stream"];
  stream.scrollTop = stream.scrollHeight;
  elements["new-message-notice"].classList.add("is-hidden");
});

elements["message-input"].addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    elements["message-form"].requestSubmit();
  }
});

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog && !["login-dialog", "token-secret-dialog"].includes(dialog.id)) {
      dialog.close();
    }
  });
  dialog.addEventListener("close", () => {
    clearDialogDrafts(dialog);
  });
});

elements["task-table"].addEventListener("click", (event) => {
  const button = event.target.closest("[data-task-id]");
  if (button) openTaskDetails(button.dataset.taskId).catch(handleError);
});

elements["task-intake-list"].addEventListener("click", (event) => {
  const button = event.target.closest("[data-task-id]");
  if (button) openTaskDetails(button.dataset.taskId).catch(handleError);
});

elements["task-detail-contract"].addEventListener("click", (event) => {
  const button = event.target.closest("[data-related-task-id]");
  if (button) openTaskDetails(button.dataset.relatedTaskId).catch(handleError);
});

elements["token-list"].addEventListener("click", async (event) => {
  const button = event.target.closest("[data-token-action]");
  if (!button || !state.projectId) return;
  const credentialId = button.dataset.credentialId;
  const credential = state.credentials.find((item) => item.id === credentialId);
  if (!credential) return;
  try {
    if (button.dataset.tokenAction === "permissions") {
      state.editingCredentialId = credentialId;
      const selected = new Set(credential.permissions || []);
      const permissions = state.config?.domain?.agent_permissions || [];
      elements["token-permissions-project"].textContent = `所属 Project：${state.snapshot?.project?.name || "未选择"}`;
      elements["token-permissions-title"].textContent = `修改“${credential.name}”权限`;
      elements["token-permissions-edit"].innerHTML = permissions.map((permission) => `
        <label class="check-row">
          <input type="checkbox" value="${escapeHtml(permission)}" ${selected.has(permission) ? "checked" : ""}>
          <span>${escapeHtml(permissionLabel(permission))}</span>
        </label>`).join("");
      elements["token-permissions-dialog"].showModal();
      return;
    }
    if (button.dataset.tokenAction === "extend") {
      state.editingCredentialId = credentialId;
      elements["token-extend-project"].textContent = `所属 Project：${state.snapshot?.project?.name || "未选择"}`;
      elements["token-extend-title"].textContent = `续期“${credential.name}”`;
      elements["token-extend-current"].textContent = `当前到期时间：${formatTime(credential.expires_at)}`;
      elements["token-extend-days"].value = "30";
      elements["token-extend-dialog"].showModal();
      return;
    }
    if (button.dataset.tokenAction === "revoke") {
      if (!window.confirm("吊销后使用该 Token 的新连接会被拒绝，确定继续吗？")) return;
      await api(`/api/v1/projects/${state.projectId}/agent-tokens/${credentialId}`, { method: "DELETE" });
      showToast("Token 已吊销");
      await refreshManagement();
    }
  } catch (error) {
    handleError(error);
  }
});

elements["token-permissions-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const credentialId = state.editingCredentialId;
  if (!credentialId || !state.projectId) return;
  const permissions = Array.from(
    elements["token-permissions-edit"].querySelectorAll('input[type="checkbox"]:checked')
  ).map((input) => input.value);
  if (!permissions.length) {
    showToast("至少保留一项权限", "error");
    return;
  }
  elements["token-permissions-submit"].disabled = true;
  try {
    await api(`/api/v1/projects/${state.projectId}/agent-tokens/${credentialId}/permissions`, {
      method: "PATCH",
      body: JSON.stringify({ permissions }),
    });
    elements["token-permissions-dialog"].close();
    await refreshManagement();
    showToast("Token 权限已更新，无需重新配置客户端");
  } catch (error) {
    handleError(error);
  } finally {
    elements["token-permissions-submit"].disabled = false;
  }
});

elements["token-extend-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const credentialId = state.editingCredentialId;
  if (!credentialId || !state.projectId) return;
  const days = Number(elements["token-extend-days"].value);
  if (!Number.isInteger(days) || days < 1 || days > 365) {
    showToast("续期天数必须是 1 到 365 的整数", "error");
    return;
  }
  elements["token-extend-submit"].disabled = true;
  try {
    await api(`/api/v1/projects/${state.projectId}/agent-tokens/${credentialId}/extend`, {
      method: "POST",
      body: JSON.stringify({ extend_by_seconds: days * 24 * 60 * 60 }),
    });
    elements["token-extend-dialog"].close();
    await refreshManagement();
    showToast(`Token 已续期 ${days} 天，Token 未更换`);
  } catch (error) {
    handleError(error);
  } finally {
    elements["token-extend-submit"].disabled = false;
  }
});

elements["token-permissions-dialog"].addEventListener("close", () => {
  state.editingCredentialId = null;
});
elements["token-extend-dialog"].addEventListener("close", () => {
  state.editingCredentialId = null;
});

elements["member-list"].addEventListener("click", async (event) => {
  const button = event.target.closest("[data-member-action]");
  if (!button || !state.projectId) return;
  const member = state.members.find((item) => item.id === button.dataset.memberId);
  if (!member) return;
  try {
    if (button.dataset.memberAction === "revoke") {
      if (!window.confirm(`吊销成员“${member.name}”后，关联 Token 将不能再用于新连接，确定继续吗？`)) return;
      await api(`/api/v1/projects/${state.projectId}/members/${member.id}`, { method: "DELETE" });
      showToast("项目成员已吊销");
      await refreshManagement();
    }
  } catch (error) {
    handleError(error);
  }
});

function activateTab(button) {
  if (!button) return;
  document.querySelectorAll(".tab").forEach((item) => {
    const selected = item === button;
    item.classList.toggle("is-active", selected);
    item.setAttribute("aria-selected", selected ? "true" : "false");
    item.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${button.dataset.tab}`);
  });
}

document.querySelector(".tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (!button) return;
  withBusy(async () => activateTab(button)).catch(handleError);
});

document.querySelector(".tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = [...document.querySelectorAll(".tab")];
  if (!tabs.length) return;
  const current = event.target.closest("[data-tab]") || tabs.find((item) => item.classList.contains("is-active"));
  const index = Math.max(0, tabs.indexOf(current));
  let nextIndex = index;
  if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
  if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
  if (event.key === "Home") nextIndex = 0;
  if (event.key === "End") nextIndex = tabs.length - 1;
  event.preventDefault();
  tabs[nextIndex].focus();
  activateTab(tabs[nextIndex]);
});

document.getElementById("task-navigation").addEventListener("click", (event) => {
  const button = event.target.closest("[data-task-entry]");
  if (!button) return;
  state.taskEntry = button.dataset.taskEntry;
  if (state.snapshot) renderTasks(state.snapshot.tasks);
});

elements["task-sort-controls"]?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-task-sort]");
  if (!button) return;
  const sortKey = button.dataset.taskSort;
  if (sortKey === "reset") {
    state.taskSort = { key: "", direction: "asc" };
  } else if (sortKey === "priority") {
    if (state.taskSort.key === "priority") {
      state.taskSort.direction = state.taskSort.direction === "asc" ? "desc" : "asc";
    } else {
      state.taskSort = { key: "priority", direction: "asc" };
    }
  } else if (sortKey === "number") {
    if (state.taskSort.key === "number") {
      state.taskSort.direction = state.taskSort.direction === "desc" ? "asc" : "desc";
    } else {
      state.taskSort = { key: "number", direction: "desc" };
    }
  }
  renderTaskSortControls();
  if (state.snapshot) renderTaskTable(state.snapshot.tasks);
});

document.getElementById("task-expert-filters").addEventListener("change", (event) => {
  const field = event.target.dataset.expert;
  if (!field || field === "number") return;
  state.taskExpert[field] = event.target.value;
  if (state.snapshot) renderTaskTable(state.snapshot.tasks);
});

document.getElementById("task-expert-filters").addEventListener("input", (event) => {
  const field = event.target.dataset.expert;
  if (field !== "number") return;
  state.taskExpert.number = event.target.value;
  if (state.snapshot) renderTaskTable(state.snapshot.tasks);
});

elements["task-history-filter"].addEventListener("change", () => {
  if (!state.editingTaskId) return;
  state.taskHistory = {
    ...(state.taskHistory || {}),
    taskId: state.editingTaskId,
    items: [],
    eventType: elements["task-history-filter"].value || "",
  };
  withBusy(async () => {
    await loadTaskHistory(state.editingTaskId);
    const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
    if (task) renderTaskTimeline(task);
  }).catch(handleError);
});

elements["task-history-load-earlier"].addEventListener("click", () => {
  if (!state.editingTaskId) return;
  withBusy(async () => {
    await loadTaskHistory(state.editingTaskId, { direction: "earlier" });
    const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
    if (task) renderTaskTimeline(task);
  }).catch(handleError);
});

elements["task-history-load-later"].addEventListener("click", () => {
  if (!state.editingTaskId) return;
  withBusy(async () => {
    await loadTaskHistory(state.editingTaskId, { direction: "later" });
    const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
    if (task) renderTaskTimeline(task);
  }).catch(handleError);
});

elements["task-timeline"].addEventListener("click", (event) => {
  const expand = event.target.closest("[data-expand-history]");
  if (expand) {
    const eventId = Number(expand.dataset.expandHistory);
    if (state.expandedEvents.has(eventId)) state.expandedEvents.delete(eventId);
    else rememberExpandedEvent(eventId);
    const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
    if (task) renderTaskTimeline(task);
    return;
  }
  const copy = event.target.closest("[data-copy-event]");
  if (copy) {
    copyEventReference(copy.dataset.copyEvent, copy.dataset.taskNumber, copy.dataset.copySeq);
  }
});

document.addEventListener("click", (event) => {
  const openEvent = event.target.closest("[data-open-event]");
  if (!openEvent) return;
  if (openEvent.closest("#task-timeline")) return;
  withBusy(() => openEventReference(openEvent.dataset.openEvent)).catch(handleError);
});

window.addEventListener("hashchange", () => {
  const matched = location.hash.match(/^#event-(\d+)$/);
  if (!matched) return;
  withBusy(() => openEventReference(matched[1])).catch(handleError);
});

elements["event-filter"].addEventListener("change", () => {
  state.eventFilter = elements["event-filter"].value;
  if (state.snapshot) renderEvents(state.snapshot.agents, state.snapshot.tasks);
});

elements["event-hide-system"].addEventListener("change", () => {
  state.hideSystemFeedEvents = elements["event-hide-system"].checked;
  if (state.snapshot) renderEvents(state.snapshot.agents, state.snapshot.tasks);
});

elements["message-task"].addEventListener("change", () => {
  if (elements["message-task"].value && elements["message-channel"].value === "public") {
    elements["message-channel"].value = "task";
  } else if (!elements["message-task"].value && elements["message-channel"].value === "task") {
    elements["message-channel"].value = "public";
  }
});

elements["project-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const project = await withBusy(() => api("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify({
        name: elements["project-name-input"].value.trim() || null,
        root_path: elements["project-path-input"].value.trim(),
      }),
    }));
    elements["project-dialog"].close();
    elements["project-form"].reset();
    const previousId = state.projectId;
    const alreadyKnown = state.projects.some((item) => item.id === project.id);
    await loadProjects(alreadyKnown && previousId ? previousId : project.id);
    showToast("项目已添加");
  } catch (error) {
    handleError(error);
  }
});


elements["settings-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.snapshot) return;
  try {
    await api(`/api/v1/projects/${state.projectId}`, {
      method: "PATCH",
      body: JSON.stringify({
        name: elements["settings-project-name"].value.trim(),
        settings: {
          lease_conflict_policy: elements["settings-lease-policy"].value,
          roles: elements["settings-roles"].value.split("\n").map((item) => item.trim()).filter(Boolean),
          extensions: state.snapshot.project.settings.extensions || {},
          default_task_priority: Number(elements["settings-default-priority"].value),
          mcp_message_limit: Number(elements["settings-mcp-message-limit"].value),
          audit_retention_days: Number(elements["settings-audit-retention"].value),
        },
      }),
    });
    await api("/api/v1/admin/backup-settings", {
      method: "PUT",
      body: JSON.stringify({
        auto_backup_enabled: elements["settings-auto-backup"].checked,
        auto_backup_max_kept: Number(elements["settings-backup-max-kept"].value),
      }),
    });
    elements["settings-dialog"].close();
    showToast("项目设置已保存");
    await refreshSnapshot(state.projectId);
  } catch (error) {
    handleError(error);
  }
});

elements["task-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) return;
  const rawDescription = elements["task-raw-description-input"].value.trim();
  const targetMemberId = elements["task-target-agent-input"].value;
  if (!rawDescription || !targetMemberId) {
    showToast("请填写原始任务说明并选择受理 Agent", "error");
    return;
  }
  elements["task-intake-submit"].disabled = true;
  try {
    await withBusy(() => api(`/api/v1/projects/${state.projectId}/task-intakes`, {
      method: "POST",
      body: JSON.stringify({ raw_description: rawDescription, target_member_id: targetMemberId }),
    }));
    elements["task-dialog"].close();
    elements["task-form"].reset();
    await refreshTaskIntakeData();
    showToast("任务意图已提交，等待 Agent 受理");
  } catch (error) {
    handleError(error);
  } finally {
    elements["task-intake-submit"].disabled = !state.taskIntakeTargets.length;
  }
});

elements["archive-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) return;
  const permanent = elements["permanent-delete-input"].checked;
  try {
    closeEventSource();
    await api(`/api/v1/projects/${state.projectId}${permanent ? "?permanent=true" : ""}`, { method: "DELETE" });
    elements["archive-dialog"].close();
    localStorage.removeItem("agentchatroom.projectId");
    state.projectId = null;
    await loadProjects();
    showToast(permanent ? "项目数据已永久删除" : "项目已归档");
  } catch (error) {
    handleError(error);
  }
});

elements["permanent-delete-input"].addEventListener("change", () => {
  elements["remove-project-submit"].textContent = elements["permanent-delete-input"].checked
    ? "永久删除"
    : "确认归档";
});

elements["message-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = elements["message-input"].value.trim();
  if (!body || !state.projectId) return;
  elements["send-message-button"].disabled = true;
  try {
    await api(`/api/v1/projects/${state.projectId}/messages`, {
      method: "POST",
      body: JSON.stringify({
        body,
        kind: elements["message-kind"].value,
        channel: elements["message-channel"].value,
        task_id: elements["message-task"].value || null,
        priority: Number(elements["message-priority"].value),
        requires_ack: elements["message-requires-ack"].checked,
      }),
    });
    elements["message-input"].value = "";
    elements["message-requires-ack"].checked = false;
  } catch (error) {
    handleError(error);
  } finally {
    elements["send-message-button"].disabled = false;
  }
});

elements["login-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  elements["login-error"].textContent = "";
  try {
    await withBusy(() => api("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ token: elements["login-token"].value }),
    }));
    state.authenticated = true;
    elements["login-token"].value = "";
    elements["login-dialog"].close();
    elements["logout-button"].classList.remove("is-hidden");
    await loadAuthenticatedApp();
  } catch (error) {
    elements["login-error"].textContent = error.message || "登录失败";
  }
});

elements["token-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) return;
  const projectId = state.projectId;
  const setup = state.pendingHttpSetup;
  let existingProjectCredentials = [];
  let projectCredentialName = "";
  let manualImportText = "";
  if (setup?.projectId === projectId) {
    projectCredentialName = String(setup.projectName || "").trim();
    // 正常主流程不粘贴任何配置；文本只出现在默认折叠的高级故障恢复区。
    manualImportText = elements["token-existing-config"].value.trim();
    if (manualImportText) {
      try {
        existingProjectCredentials = parseExistingProjectCredentials(manualImportText);
      } catch (error) {
        showToast(error.message || "已有 HTTP 配置无法解析", "error");
        return;
      }
    }
  }
  const memberId = elements["token-member"].value;
  const member = state.members.find((item) => item.id === memberId);
  let softwareIdentity = null;
  if (setup?.projectId === projectId) {
    let selectedIdentity;
    try {
      selectedIdentity = softwareIdentityForMember(member);
    } catch (error) {
      showToast(error.message || "所选项目成员的软件身份不完整", "error");
      return;
    }
    if (manualImportText) {
      let existingIdentity;
      try {
        existingIdentity = parseExistingSoftwareIdentity(manualImportText);
      } catch (error) {
        showToast(error.message || "已有 HTTP 配置的软件身份无法解析", "error");
        return;
      }
      if (!existingIdentity) {
        showToast("已有配置缺少完整的软件身份字段，请粘贴完整 agentchatroom HTTP 配置", "error");
        return;
      }
      if (selectedIdentity && !sameSoftwareIdentity(existingIdentity, selectedIdentity)) {
        showToast("已有配置与所选项目成员不是同一个软件身份，不能合并", "error");
        return;
      }
      softwareIdentity = existingIdentity;
    } else if (selectedIdentity) {
      // 关联已有成员：沿用该成员现有软件身份，不新建身份。
      softwareIdentity = selectedIdentity;
    } else if (member) {
      showToast("所选项目成员的软件身份不完整，请改选成员，或改为不关联并填写 Agent 显示名称", "error");
      return;
    } else if (setup.mode === "add_project") {
      // 已配置软件增量加入 Project：客户端原身份由 Agent 在本地保留，
      // 服务端按现有身份登记，这里不生成第二套身份。
      softwareIdentity = null;
    } else {
      // 首次接入且不关联成员：名称必须由用户明确填写，禁止用接入格式标签兜底。
      try {
        softwareIdentity = createSoftwareIdentityForProfile(
          setup.profile,
          elements["token-agent-name"].value,
        );
      } catch (error) {
        showToast(error.message || "Agent 显示名称无效，请填写实际接入端名称", "error");
        return;
      }
    }
  }
  const permissions = [...elements["token-permissions"].querySelectorAll("input:checked")]
    .map((input) => input.value);
  if (!permissions.length) {
    showToast("至少选择一项权限", "error");
    return;
  }
  try {
    const result = await api(`/api/v1/projects/${projectId}/agent-tokens`, {
      method: "POST",
      body: JSON.stringify({
        name: elements["token-name"].value.trim(),
        member_id: memberId || null,
        permissions,
        expires_in_seconds: Number(elements["token-days"].value) * 24 * 60 * 60,
      }),
    });
    elements["token-dialog"].close();
    state.pendingHttpSetup = null;
    if (setup?.projectId === projectId) {
      const newCredential = {name: projectCredentialName, token: result.token};
      if (manualImportText || setup.mode !== "add_project") {
        const projectCredentials = mergeProjectCredentials(existingProjectCredentials, newCredential);
        showIssuedHttpResult({...setup, member, softwareIdentity, projectCredentials});
      } else {
        // 增量路径：后端只保存 Token 哈希，页面无法重建旧凭据；
        // 合并旧配置的责任交给已配置的 Agent 在客户端本地完成。
        showIssuedHttpResult({...setup, member, softwareIdentity,
          projectCredentials: [newCredential], incremental: true});
      }
    } else {
      showTokenSecret(result.token);
    }
    await refreshManagement();
  } catch (error) {
    handleError(error);
  }
});

elements["task-assign-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task || !state.projectId) return;
  const selected = elements["task-assign-agent"].value.split(":");
  const targetKind = selected[0];
  const targetValue = selected.slice(1).join(":");
  if (!targetValue) {
    showToast("请选择目标 Agent", "error");
    return;
  }
  const targetFields = targetKind === "member"
    ? { assigned_to_member_id: targetValue }
    : { assigned_to_session_id: targetValue };
  elements["task-assign-submit"].disabled = true;
  try {
    await api(`/api/v1/projects/${state.projectId}/tasks/${task.id}/assignments`, {
      method: "POST",
      body: JSON.stringify({
        ...targetFields,
        target_role: "",
        required_capability: "",
        note: elements["task-assign-note"].value.trim(),
      }),
    });
    elements["task-assign-dialog"].close();
    await refreshTaskIntakeData();
    const updatedTask = state.snapshot?.tasks.find((item) => item.id === task.id) || task;
    renderTaskContract(updatedTask);
    renderTaskAssignments(updatedTask);
    renderTaskTimeline(updatedTask);
    showToast(`任务 #${task.task_number} 已指定给 Agent`);
  } catch (error) {
    handleError(error);
  } finally {
    elements["task-assign-submit"].disabled = false;
  }
});


function showTokenSecret(token) {
  state.issuedHttpSetup = null;
  elements["token-secret-context"].textContent = "一次性 Secret";
  elements["token-secret-title"].textContent = "保存 Agent Token";
  elements["token-secret-section"].hidden = false;
  elements["token-secret-copy"].hidden = false;
  elements["token-config-value"].textContent = "";
  elements["token-config-section"].hidden = true;
  elements["token-secret-value"].textContent = token;
  if (!elements["token-secret-dialog"].open) {
    elements["token-secret-dialog"].showModal();
  }
}

function showIssuedHttpResult(setup) {
  state.issuedHttpSetup = setup;
  const reconnect = setup.mode === "reconnect";
  elements["token-secret-context"].textContent = reconnect
    ? "恢复当前 Project"
    : setup.incremental ? "加入当前 Project（增量）" : "一次性 HTTP 接入内容";
  elements["token-secret-title"].textContent = reconnect
    ? "复制恢复连接提示词"
    : setup.incremental ? "复制增量接入提示词" : "复制完整 HTTP 接入提示词";
  elements["token-secret-section"].hidden = true;
  elements["token-secret-copy"].hidden = true;
  elements["token-secret-value"].textContent = "";
  elements["token-config-format"].innerHTML = Object.entries(setup.profiles).map(([id, profile]) =>
    `<option value="${escapeHtml(id)}">${escapeHtml(profile.label || id)}</option>`).join("");
  elements["token-config-format"].value = setup.format;
  elements["token-config-section"].hidden = false;
  renderIssuedHttpConfig();
  if (!elements["token-secret-dialog"].open) elements["token-secret-dialog"].showModal();
}

function renderIssuedHttpConfig() {
  const setup = state.issuedHttpSetup;
  const profile = setup?.profiles?.[elements["token-config-format"].value];
  if (!profile) return;
  if (setup.mode === "reconnect") {
    elements["token-config-heading"].textContent = "恢复连接提示词";
    elements["token-config-copy"].textContent = "复制恢复提示词";
    elements["token-config-hint"].textContent = "本场景不签发新 Token，使用客户端已有的 agentchatroom HTTP 配置恢复连接。";
    elements["token-config-value"].textContent = profile?.onboarding_modes?.reconnect?.http
      || "恢复连接提示词尚未生成，请更新服务后重试。";
    elements["token-config-path"].textContent = "无需修改或新增 MCP；把本提示词交给 Agent 执行连接核对";
    elements["token-config-projects"].textContent = `目标 Project：${setup.projectName}`;
    return;
  }
  if (setup.incremental) {
    elements["token-config-heading"].textContent = "增量接入提示词（交给已配置的 Agent）";
    elements["token-config-copy"].textContent = "复制增量提示词";
    elements["token-config-hint"].textContent = "包含新 Project Token 明文，只整体复制给已配置过 agentchatroom 的那个 Agent；Agent 会在客户端本地保留旧配置并自行合并，不要发送到 Room、日志或仓库。";
    elements["token-config-value"].textContent = incrementalHttpPrompt(profile, setup);
    elements["token-config-path"].textContent = `${profile.config_path_hint || "客户端 MCP 配置"}；MCP Server 标准名称：agentchatroom（Agent 更新现有条目，不新增）`;
    elements["token-config-projects"].textContent = `目标 Project：${setup.projectName}；客户端原有 Project 凭据由 Agent 在本地保留`;
    return;
  }
  elements["token-config-heading"].textContent = "完整 HTTP 接入提示词";
  elements["token-config-copy"].textContent = "复制完整提示词";
  elements["token-config-hint"].textContent = "内容包含 Project 与 Token 的明文对应关系，只交给负责配置该客户端的 Agent；不要发送到 Room、日志或仓库。";
  elements["token-config-value"].textContent = issuedHttpPrompt(profile, setup);
  elements["token-config-path"].textContent = `${profile.config_path_hint || "客户端 MCP 配置"}；MCP Server 标准名称：agentchatroom`;
  elements["token-config-projects"].textContent = `已包含 ${setup.projectCredentials.length} 个 Project：${setup.projectCredentials.map((item) => item.name).join("、")}`;
}

const PROJECT_CREDENTIAL_BUNDLE_PREFIX = "acrb.v1.";

function encodeBase64UrlUtf8(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64UrlUtf8(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4));
  return new TextDecoder().decode(Uint8Array.from(binary, (character) => character.charCodeAt(0)));
}

function validateProjectCredentials(entries) {
  if (!Array.isArray(entries) || !entries.length || entries.length > 32) {
    throw new Error("项目凭据数量必须在 1 到 32 之间");
  }
  const names = new Set();
  const tokens = new Set();
  return entries.map((entry) => {
    const name = String(entry?.name || "").trim();
    const token = String(entry?.token || "").trim();
    if (!name || name.length > 200 || !/^acr\.(?!bundle\.)[^\s]+$/.test(token)) {
      throw new Error("项目名称或 Token 格式无效");
    }
    const folded = name.toLocaleLowerCase();
    if (names.has(folded) || tokens.has(token)) throw new Error("项目凭据名称和 Token 必须唯一");
    names.add(folded);
    tokens.add(token);
    return {name, token};
  });
}

function encodeProjectCredentialBundle(entries) {
  const projects = validateProjectCredentials(entries);
  return `${PROJECT_CREDENTIAL_BUNDLE_PREFIX}${encodeBase64UrlUtf8(JSON.stringify({projects}))}`;
}

function decodeProjectCredentialBundle(bundle) {
  const value = String(bundle || "").trim();
  if (!value.startsWith(PROJECT_CREDENTIAL_BUNDLE_PREFIX) || value.length > 16384) {
    throw new Error("多项目凭据包格式无效");
  }
  let parsed;
  try {
    parsed = JSON.parse(decodeBase64UrlUtf8(value.slice(PROJECT_CREDENTIAL_BUNDLE_PREFIX.length)));
  } catch (_error) {
    throw new Error("多项目凭据包无法解码");
  }
  if (!parsed || Object.keys(parsed).length !== 1 || !Array.isArray(parsed.projects)) {
    throw new Error("多项目凭据包结构无效");
  }
  const projects = validateProjectCredentials(parsed.projects);
  if (encodeProjectCredentialBundle(projects) !== value) throw new Error("多项目凭据包不是规范格式");
  return projects;
}

function parseExistingProjectCredentials(input) {
  const value = String(input || "").trim();
  if (!value) return [];
  const bundle = value.match(/acrb\.v1\.[A-Za-z0-9_-]+/)?.[0];
  if (bundle) return decodeProjectCredentialBundle(bundle);
  const entries = value.split(/\r?\n/).filter((line) => line.trim()).map((line) => {
    const separator = line.indexOf("=");
    if (separator < 1) throw new Error("请粘贴完整配置、凭据包，或使用“项目名称=Token”格式");
    return {name: line.slice(0, separator), token: line.slice(separator + 1)};
  });
  return validateProjectCredentials(entries);
}

const SOFTWARE_IDENTITY_HEADER_NAMES = Object.freeze({
  softwareKey: "X-AgentChatRoom-Software-Key",
  softwareName: "X-AgentChatRoom-Software-Name",
  softwareClient: "X-AgentChatRoom-Software-Client",
});

const HTTP_IDENTITY_UTF8_PREFIX = "acr-utf8.v1.";

function encodeHttpIdentityHeaderValue(value) {
  const normalized = String(value || "").trim();
  if (!normalized || /[\x00-\x1f\x7f]/.test(normalized)) {
    throw new Error("软件身份 Header 必须是非空且不含控制字符的文本");
  }
  return /^[\x20-\x7e]+$/.test(normalized)
    ? normalized
    : `${HTTP_IDENTITY_UTF8_PREFIX}${encodeBase64UrlUtf8(normalized)}`;
}

function decodeHttpIdentityHeaderValue(value) {
  const normalized = String(value || "").trim();
  if (!normalized.startsWith(HTTP_IDENTITY_UTF8_PREFIX)) return normalized;
  try {
    return decodeBase64UrlUtf8(normalized.slice(HTTP_IDENTITY_UTF8_PREFIX.length));
  } catch (_error) {
    throw new Error("已有配置包含无效的软件身份 Header 编码");
  }
}

function normalizedSoftwareIdentity(identity) {
  if (!identity) return null;
  const normalized = Object.fromEntries(
    Object.keys(SOFTWARE_IDENTITY_HEADER_NAMES).map((key) => [
      key,
      String(identity[key] || "").trim(),
    ])
  );
  const present = Object.values(normalized).filter(Boolean).length;
  if (!present) return null;
  if (present !== 3) throw new Error("软件身份字段不完整，必须同时包含 Key、Name 和 Client");
  return normalized;
}

function softwareIdentityForMember(member) {
  return normalizedSoftwareIdentity({
    softwareKey: member?.metadata?.software_key,
    softwareName: member?.name,
    softwareClient: member?.metadata?.client,
  });
}

function concreteIdentityValue(value) {
  const normalized = String(value || "").trim();
  return normalized && !normalized.startsWith("<") ? normalized : "";
}

function accessFormatProfileLabel(profile) {
  // 参考配置里软件身份仍是占位符的 profile（如“通用（标准 MCP）”）：
  // 它的 label 只描述接入格式/接入方式，不能当作软件身份显示名称。
  return concreteIdentityValue(profile?.software_name)
    ? ""
    : String(profile?.label || "").trim();
}

function createSoftwareIdentityForProfile(profile, agentName) {
  const client = concreteIdentityValue(profile?.software_client) || "standard-mcp";
  const name = concreteIdentityValue(agentName);
  const formatLabel = accessFormatProfileLabel(profile);
  if (!name) {
    throw new Error("请填写实际的 Agent 显示名称；接入格式标签不能作为软件身份名称");
  }
  if (name === formatLabel) {
    throw new Error(`“${formatLabel}”是接入格式标签，请填写实际的 Agent 显示名称`);
  }
  const prefix = concreteIdentityValue(profile?.software_key) || client;
  const suffix = globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return normalizedSoftwareIdentity({
    softwareKey: `${prefix}-${suffix}`,
    softwareName: name,
    softwareClient: client,
  });
}

function parseExistingSoftwareIdentity(input) {
  const value = String(input || "").trim();
  if (!value) return null;
  const found = {};
  for (const [key, header] of Object.entries(SOFTWARE_IDENTITY_HEADER_NAMES)) {
    const escaped = header.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const patterns = [
      new RegExp(`"${escaped}"\\s*:\\s*"([^"]*)"`, "g"),
      new RegExp(`(?:^|\\n)\\s*${escaped}\\s*=\\s*"([^"]*)"`, "g"),
    ];
    const values = new Set();
    for (const pattern of patterns) {
      for (const match of value.matchAll(pattern)) {
        if (match[1].trim()) values.add(decodeHttpIdentityHeaderValue(match[1]));
      }
    }
    if (values.size > 1) throw new Error(`已有配置包含冲突的 ${header}`);
    found[key] = values.size ? [...values][0] : "";
  }
  return normalizedSoftwareIdentity(found);
}

function sameSoftwareIdentity(left, right) {
  return ["softwareKey", "softwareName", "softwareClient"].every(
    (key) => left[key] === right[key]
  );
}

function mergeProjectCredentials(existing, added) {
  const incoming = {name: String(added.name || "").trim(), token: String(added.token || "").trim()};
  const merged = existing.filter((item) => item.name.toLocaleLowerCase() !== incoming.name.toLocaleLowerCase());
  merged.push(incoming);
  return validateProjectCredentials(merged);
}

function issuedHttpPrompt(profile, setup) {
  const projectCredentials = validateProjectCredentials(setup.projectCredentials);
  const config = issuedHttpConfig(profile, projectCredentials, setup.softwareIdentity);
  const mode = setup.mode === "add_project" ? "add_project" : "first_setup";
  const bootstrapProjectName = String(setup.projectName || "").trim();
  const modeLabel = mode === "add_project" ? "已配置软件，加入本项目" : "首次配置软件";
  const mapping = projectCredentials.flatMap((item, index) => [
    `project_name_${index + 1}=${JSON.stringify(item.name)}`,
    `project_token_${index + 1}=${JSON.stringify(item.token)}`,
  ]).join("\n");
  const template = profile.streamable_http_config_text || "";
  let instructions = profile?.onboarding_modes?.[mode]?.http
    || profile?.onboarding_prompts?.http
    || "按目标 Project 名称完成 room_bootstrap，并核对返回的 Project。";
  if (template && instructions.includes(template)) {
    instructions = instructions.replace(
      template,
      "（含真实凭据的完整 MCP 配置已在上方给出，请勿再使用占位配置。）",
    );
  }
  return [
    "请为当前客户端完成 AgentChatRoom HTTP MCP 接入。以下内容含真实测试凭据。",
    `接入场景：${modeLabel}`,
    `目标 Project：${bootstrapProjectName}`,
    `本工作区固定 bootstrap 参数：project_name=${JSON.stringify(bootstrapProjectName)}`,
    "MCP Server 标准名称：agentchatroom",
    "",
    "Project 与 Token 对应关系（配置时必须按名称保留，不得混用）：",
    mapping,
    "",
    "可直接使用的 HTTP MCP 参考配置：",
    config,
    "",
    "客户端配置格式如与参考格式不同，只转换语法；必须保留 URL、Authorization、软件身份字段和标准服务器名 agentchatroom。不要命名为 agentchatroom-stdio。",
    "",
    "完成配置后执行以下场景指令：",
    instructions,
    "",
    "不要把上述 Token 发布到 Room、日志或仓库。",
  ].join("\n");
}

function incrementalHttpPrompt(profile, setup) {
  const credential = validateProjectCredentials(setup.projectCredentials)[0];
  const projectName = String(setup.projectName || credential.name).trim();
  const configHint = profile.config_path_hint || "客户端 MCP 配置文件";
  const bootstrapCall = `room_bootstrap(project_name=${JSON.stringify(projectName)})`;
  const linkedIdentity = setup.member
    ? normalizedSoftwareIdentity(setup.softwareIdentity)
    : null;
  const identityInstruction = linkedIdentity
    ? `本次 Token 已关联成员 ${JSON.stringify(setup.member.name)}，服务端会直接从已关联 Token 解析软件身份：X-AgentChatRoom-Software-Key=${JSON.stringify(linkedIdentity.softwareKey)}，X-AgentChatRoom-Software-Name=${JSON.stringify(linkedIdentity.softwareName)}，X-AgentChatRoom-Software-Client=${JSON.stringify(linkedIdentity.softwareClient)}。现有配置可以没有这三个 Header；若已经配置 Header，则三字段必须与上述身份完全一致，冲突时停止且不要覆盖。凭据包中的其他已关联 Token 也必须属于同一软件身份，否则服务端会拒绝整个连接。`
    : "本次 Token 未关联成员。必须原样保留客户端现有的软件身份三字段；目标 Project 第一次成功连接时会按该身份自动登记成员。";
  return [
    "该客户端已经配置过 agentchatroom HTTP MCP。请把下面签发的新 Project 凭据增量合并进客户端现有配置；不要新建第二个 agentchatroom 连接器或同名变体，也不要重新执行首次安装。",
    "",
    "接入场景：已配置软件，加入本项目（增量合并）",
    `目标 Project：${projectName}`,
    `本工作区固定 bootstrap 参数：project_name=${JSON.stringify(projectName)}`,
    "MCP Server 标准名称：agentchatroom（全局唯一，保持不变）",
    "",
    "本次签发的新凭据（只用于写入客户端本地 MCP 配置）：",
    `project_name_1=${JSON.stringify(credential.name)}`,
    `project_token_1=${JSON.stringify(credential.token)}`,
    "",
    identityInstruction,
    "",
    "操作步骤：",
    `1. 先检查客户端本地是否已存在名为 agentchatroom 的 HTTP MCP 配置（配置位置参考：${configHint}）。找到后保留它的 url、Authorization 中的全部旧 Project 凭据，以及当前确实存在的软件身份三字段（X-AgentChatRoom-Software-Key / X-AgentChatRoom-Software-Name / X-AgentChatRoom-Software-Client）；缺少这些 Header 时不要据此判定失败，服务端可从已关联 Token 解析身份。只追加或替换本条目。`,
    "2. Authorization 合并方法：现值为 `Bearer acrb.v1.<base64url>` 时，把 `acrb.v1.` 之后的 base64url 解码为 JSON {\"projects\":[{\"name\":...,\"token\":...}]}，按 Project 名称把 {\"name\":" + JSON.stringify(credential.name) + ",\"token\":" + JSON.stringify(credential.token) + "} 追加或替换进去，用相同编码重新打包为 `Bearer acrb.v1.<新值>` 写回。现值为单个 `Bearer acr.*` Token 时，先保持旧配置不变，用现有连接调用零参数 `room_bootstrap()` 读取并记录旧 Project 的精确名称；然后把旧名称与旧 Token、新名称与新 Token 一并打成上述 `acrb.v1` 凭据包。无法取得旧 Project 名称时停止，不得猜测。",
    "3. 只写回同一个 agentchatroom 条目，不改动其他 Project 的凭据；保存后重载客户端 MCP，为本项目新建独立 MCP Session，调用 " + bootstrapCall + "，核对返回的 Project 名称与 root_path 与当前工作区一致后才允许写操作。",
    "4. 无法读取或找不到现有 agentchatroom 配置时：停止合并，向用户报告失败原因和实际检查过的配置文件位置，请用户在 Web 签发弹窗底部展开「高级 · 故障恢复」手动粘贴完整 `acrb.v1` 配置；旧配置只有单个 `acr.*` Token 时，恢复区必须填写明确的 `旧Project名称=旧Token`，不能只粘贴无法识别项目名的单 Token。不要新建同名或变体 MCP，不要凭空重建配置，不要重试超过一次。",
    "",
    "不要把上述 Token 发布到 Room、日志或仓库；合并完成后不要在回复中复述 Token 内容。",
  ].join("\n");
}

function issuedHttpConfig(profile, projectCredentials, softwareIdentity) {
  let config = profile.streamable_http_config_text || "";
  const bundle = encodeProjectCredentialBundle(projectCredentials);
  const identity = normalizedSoftwareIdentity(softwareIdentity);
  if (!identity) throw new Error("HTTP 接入配置缺少软件身份");
  // Only the one-time result dialog receives this text. It is never stored in
  // shared integration state, browser storage, Room messages, or logs.
  if (profile.format === "json") {
    const parsed = JSON.parse(config);
    for (const server of Object.values(parsed.mcpServers || {})) {
      server.headers.Authorization = `Bearer ${bundle}`;
      server.headers["X-AgentChatRoom-Software-Key"] = encodeHttpIdentityHeaderValue(identity.softwareKey);
      server.headers["X-AgentChatRoom-Software-Name"] = encodeHttpIdentityHeaderValue(identity.softwareName);
      server.headers["X-AgentChatRoom-Software-Client"] = encodeHttpIdentityHeaderValue(identity.softwareClient);
    }
    return JSON.stringify(parsed, null, 2);
  }
  const authorization = `Authorization = ${JSON.stringify(`Bearer ${bundle}`)}`;
  const identityHeaders = {"X-AgentChatRoom-Software-Key": encodeHttpIdentityHeaderValue(identity.softwareKey),
    "X-AgentChatRoom-Software-Name": encodeHttpIdentityHeaderValue(identity.softwareName),
    "X-AgentChatRoom-Software-Client": encodeHttpIdentityHeaderValue(identity.softwareClient)};
  for (const [key, value] of Object.entries(identityHeaders)) {
      const line = `${key} = ${JSON.stringify(value)}`;
      const existing = new RegExp(`^${key} = .*$`, "m");
      if (existing.test(config)) {
        config = config.replace(existing, () => line);
      } else {
        config = config.replace(
          /(\[mcp_servers\.[^\]\n]+\.(?:http_headers|headers)\])/,
          `$1\n${line}`,
        );
      }
  }
  config = config.replace(/^bearer_token_env_var = .*\r?\n/gm, "");
  if (/^Authorization = /m.test(config)) {
    config = config.replace(/^Authorization = .*$/m, () => authorization);
  } else {
    config = config.replace(/(\[mcp_servers\.[^\]\n]+\.(?:http_headers|headers)\])/, `$1\n${authorization}`);
  }
  return config;
}

function handleError(error) {
  showToast(error.message || "发生未知错误", "error");
}

function renderMessageTaskOptions(tasks) {
  const selected = elements["message-task"].value;
  const candidates = tasks.filter((task) => taskNotFinished(task));
  elements["message-task"].innerHTML = [
    '<option value="">不关联任务</option>',
    ...candidates.map((task) => `<option value="${escapeHtml(task.id)}">任务 #${task.task_number} · ${escapeHtml(task.title)}</option>`),
  ].join("");
  if (candidates.some((task) => task.id === selected)) elements["message-task"].value = selected;
}

async function loadTaskEvents(projectId, taskId) {
  return loadEventWindow(
    (after, limit) => `/api/v1/projects/${projectId}/audit?after=${after}&limit=${limit}&task_id=${encodeURIComponent(taskId)}`,
    auditPageSize(),
    { tailJump: false },
  );
}

async function refreshTaskIntakeTargets() {
  if (!state.projectId) return;
  const result = await api(`/api/v1/projects/${state.projectId}/task-intakes/targets`);
  state.taskIntakeTargets = result.targets || [];
  const options = state.taskIntakeTargets
    .map((target) => {
      const connection = target.connection_status === "connected" ? "已连接" : "未连接";
      return `<option value="${escapeHtml(target.member_id)}">${escapeHtml(target.name)} · ${escapeHtml(target.client || target.software_key || "Agent")} · ${connection}</option>`;
    });
  elements["task-target-agent-input"].innerHTML = options.length
    ? '<option value="">请选择受理 Agent</option>' + options.join("")
    : '<option value="">暂无已接入且未吊销的 Agent</option>';
  elements["task-target-agent-input"].disabled = !options.length;
  elements["task-target-agent-empty"].textContent = options.length
    ? "可选择所有已接入且未吊销的 Agent；当前未连接的 Agent 会在重新接入后受理任务。"
    : "当前没有已接入且未吊销的 Agent。";
  elements["task-target-agent-empty"].classList.toggle("is-hidden", false);
  elements["task-intake-submit"].disabled = !options.length;
  return options.length > 0;
}

let taskIntakeRefreshSequence = 0;
async function refreshTaskIntakeData() {
  if (!state.projectId) return;
  const requestId = ++taskIntakeRefreshSequence;
  const projectId = state.projectId;
  const [targets, intakes] = await Promise.all([
    api(`/api/v1/projects/${projectId}/task-intakes/targets`),
    api(`/api/v1/projects/${projectId}/task-intakes`),
  ]);
  // 迟到的旧响应或切换项目后到达的数据一律丢弃；任务行只允许经过
  // applySnapshotIfCurrent 的项目 + 游标单调保护提交，防止旧列表覆盖新快照。
  if (requestId !== taskIntakeRefreshSequence || projectId !== state.projectId) return;
  state.taskIntakeTargets = targets.targets || [];
  state.taskIntakes = intakes.intakes || [];
  const snapshot = await refreshSnapshot(projectId).catch(() => null);
  if (requestId !== taskIntakeRefreshSequence || projectId !== state.projectId) return;
  if (snapshot && applySnapshotIfCurrent(projectId, snapshot)) {
    renderTasks(state.snapshot.tasks);
    renderMessageTaskOptions(state.snapshot.tasks);
  }
  renderTaskIntakes();
}

function sessionName(sessionId) {
  if (!sessionId) return "系统";
  const session = (state.snapshot?.agents || []).find((agent) => agent.id === sessionId);
  return session?.name || shortId(sessionId);
}

function assignmentTargetOffline(assignment) {
  if (assignment.status !== "pending" || !assignment.assigned_to_session_id) return false;
  const session = (state.snapshot?.agents || []).find((agent) => agent.id === assignment.assigned_to_session_id);
  return Boolean(session && session.status === "offline");
}

function renderTaskAssignments(task) {
  const assignments = task.assignments || [];
  elements["task-assignment-list"].innerHTML = assignments.length
    ? assignments.slice().reverse().map((assignment) => {
      const target = assignment.assigned_to_session_id
        ? sessionName(assignment.assigned_to_session_id)
        : assignment.target_role
          ? `角色 ${assignment.target_role}`
          : `能力 ${assignment.required_capability}`;
      const delayedNotice = assignmentTargetOffline(assignment)
        ? `<p class="secondary-text">目标 Agent 当前离线；重新接入后可受理该指派。</p>`
        : "";
      return `<article class="management-item">
        <div>
          <h4>${escapeHtml(target)} <span class="status-badge ${escapeHtml(assignment.status)}">${escapeHtml(assignmentStatus(assignment))}</span></h4>
          <p>${assignment.note ? escapeHtml(assignment.note) : "未填写说明"}${assignment.response_note ? ` · 回复：${escapeHtml(assignment.response_note)}` : ""}</p>
          ${assignment.status === "pending" ? '<p class="secondary-text">等待目标 Agent 确认；确认后才会产生任务认领事实。</p>' : ""}
          ${delayedNotice}
        </div>
        <span class="secondary-text">${escapeHtml(formatTime(assignment.created_at))}</span>
      </article>`;
    }).join("")
    : '<div class="empty-state">尚未派发</div>';
}

function historyActorLabel(actor) {
  if (!actor) return "unknown";
  const parts = [actor.name || "unknown", actor.client || "", actor.role || ""].filter(Boolean);
  return parts.join(" · ");
}

function renderHistoryEvidence(item) {
  return (item.evidence_sections || []).map((section, index) => {
    const sectionId = `history-${item.event_id}-sec-${index}`;
    if (section.kind === "message") {
      const expanded = state.expandedEvents.has(item.event_id);
      const body = String((section.items || [])[0] || "");
      const lines = body.split("\n");
      const canToggle = section.expandable;
      const shown = expanded || !canToggle ? lines : lines.slice(0, 8);
      return `<div class="history-evidence">
        <button type="button" class="expand-toggle" data-expand-history="${item.event_id}" aria-expanded="${expanded ? "true" : "false"}" aria-controls="${sectionId}">${expanded || !canToggle ? "正文" : "展开正文"}</button>
        <div id="${sectionId}" class="event-body ${!expanded && canToggle ? "is-collapsed" : ""}">${renderMessageLines(shown)}</div>
      </div>`;
    }
    if (section.kind === "criteria") {
      return `<div class="history-evidence"><h4>验收记录</h4><ul class="criteria-list">${(section.items || []).map((criterion) => {
        const status = criterion.status || "unknown";
        const failed = status !== "passed";
        return `<li class="criterion ${escapeHtml(status)}${failed ? " is-failed" : ""}"><strong>${escapeHtml(status)}</strong> · ${escapeHtml(criterion.criterion || "")}${criterion.evidence ? `<p>${escapeHtml(criterion.evidence)}</p>` : ""}</li>`;
      }).join("")}</ul></div>`;
    }
    if (section.kind === "tests") {
      return `<div class="history-evidence"><h4>测试证据</h4><ul class="readonly-list">${(section.items || []).map((test) => {
        const failed = Number(test.exit_code) !== 0;
        return `<li class="${failed ? "test-failed" : "test-passed"}">${failed ? "失败" : "通过"} · ${escapeHtml(test.command || "")}${test.notes ? ` · ${escapeHtml(test.notes)}` : ""}</li>`;
      }).join("")}</ul></div>`;
    }
    if (section.kind === "state") {
      return `<div class="history-evidence"><h4>状态迁移</h4><ul class="readonly-list">${(section.items || []).map((change) => `<li><code>${escapeHtml(change.field)}</code> ${escapeHtml(String(change.before))} → ${escapeHtml(String(change.after))}</li>`).join("")}</ul></div>`;
    }
    if (section.kind === "git") {
      const git = section.items?.[0] || {};
      const origin = git.branch || (git.head ? "集成提交" : "detached");
      return `<div class="history-evidence"><h4>Git 证据</h4><p>${escapeHtml(origin)} · ${escapeHtml(git.head || git.commit_hash || "unknown")}${git.captured ? "" : ` · ${escapeHtml(git.reason || "未采集")}`}</p></div>`;
    }
    const text = (section.items || []).map((entry) => {
      if (typeof entry === "string") return escapeHtml(entry);
      if (entry && entry.label) return `${escapeHtml(entry.label)}：${escapeHtml(JSON.stringify(entry.value))}`;
      return escapeHtml(JSON.stringify(entry));
    }).join("<br>");
    return `<div class="history-evidence"><h4>${escapeHtml(section.title || section.kind)}</h4><p>${text}</p></div>`;
  }).join("");
}

function renderHistoryAcknowledgements(item) {
  if (!item.requires_ack && !(item.acknowledgements || []).length) return "";
  if (!item.acknowledgements?.length) return `<p class="secondary-text">需要确认 · 尚未确认</p>`;
  return `<p class="secondary-text">已确认 ${item.acknowledgements.length} 次：${item.acknowledgements.map((ack) => `${escapeHtml(ack.actor?.name || "unknown")} · ${escapeHtml(formatTime(ack.created_at))}`).join("；")}</p>`;
}

function renderTaskTimeline(task) {
  const history = state.taskHistory;
  const items = history?.taskId === task.id ? (history.items || []) : [];
  elements["task-history-load-earlier"].disabled = !history?.has_more_before;
  elements["task-history-load-later"].disabled = !history?.has_more_after;
  elements["task-timeline"].innerHTML = items.length
    ? `<ol class="timeline-list">${items.map((item) => `
      <li class="timeline-item${state.focusEventId === item.event_id ? " is-focused" : ""}" id="history-event-${item.event_id}">
        <span class="timeline-marker" aria-hidden="true"></span>
        <div class="timeline-content">
          <div class="timeline-heading">
            <strong>${escapeHtml(eventLabel(item.event_type))}</strong>
            <span class="secondary-text">${escapeHtml(historyActorLabel(item.actor))} · ${escapeHtml(formatTime(item.occurred_at))}</span>
          </div>
          ${item.model_display_name ? `<p class="secondary-text">模型 · ${escapeHtml(item.model_display_name)}</p>` : ""}
          ${item.verdict ? `<p><span class="status-badge ${escapeHtml(item.verdict)}">${escapeHtml(item.verdict)}</span> ${escapeHtml(item.summary || "")}</p>` : `<p>${escapeHtml(item.summary || "")}</p>`}
          ${item.result ? `<p>集成结果：${escapeHtml(item.result)}。验证通过不等于最终完成。</p>` : ""}
          ${renderHistoryEvidence(item)}
          ${renderHistoryAcknowledgements(item)}
          <small class="secondary-text">${eventIdBadge(item.project_seq, item.event_id)}${item.task_number ? ` · 任务 #${escapeHtml(item.task_number)}` : ""} <button type="button" class="link-button" data-copy-event="${item.event_id}" data-copy-seq="${item.project_seq || ""}" data-task-number="${item.task_number || ""}">复制引用</button></small>
        </div>
      </li>`).join("")}</ol>`
    : '<div class="empty-state">尚无协作事件</div>';
  if (state.focusEventId) {
    const focused = document.getElementById(`history-event-${state.focusEventId}`);
    if (focused) focused.scrollIntoView({ block: "center" });
  }
}

async function loadTaskHistory(taskId, { direction } = {}) {
  if (!state.projectId || !taskId) return;
  const eventType = elements["task-history-filter"]?.value || state.taskHistory?.eventType || "";
  const params = new URLSearchParams({ limit: "50" });
  if (eventType) params.set("event_type", eventType);
  if (direction === "earlier" && state.taskHistory?.next_before) {
    params.set("before", String(state.taskHistory.next_before));
  } else if (direction === "later" && state.taskHistory?.next_after) {
    params.set("after", String(state.taskHistory.next_after));
  }
  const page = await api(`/api/v1/projects/${state.projectId}/tasks/${taskId}/history?${params}`);
  const incoming = page.items || [];
  const existing = state.taskHistory?.taskId === taskId ? (state.taskHistory.items || []) : [];
  let items = incoming;
  if (direction === "earlier") items = [...incoming, ...existing];
  if (direction === "later") items = [...existing, ...incoming];
  const seen = new Set();
  items = items.filter((item) => {
    if (seen.has(item.event_id)) return false;
    seen.add(item.event_id);
    return true;
  }).sort((left, right) => left.event_id - right.event_id);
  state.taskHistory = {
    taskId,
    items,
    total: page.total,
    next_after: items[items.length - 1]?.event_id || page.next_after,
    next_before: items[0]?.event_id || page.next_before,
    has_more_after: direction === "later" ? Boolean(page.has_more_after) : direction === "earlier" ? true : Boolean(page.has_more_after),
    has_more_before: direction === "earlier" ? Boolean(page.has_more_before) : direction === "later" ? true : Boolean(page.has_more_before),
    eventType,
  };
}

function formatSpecReceipt(receipt) {
  const entries = Object.entries(receipt || {});
  if (!entries.length) return "";
  const titles = Object.fromEntries(
    (state.snapshot?.documents || []).map((doc) => [doc.doc_key, doc.title]),
  );
  return entries
    .map(([docKey, version]) => `${titles[docKey] || docKey} v${version}`)
    .join(" · ");
}

function renderTaskContract(task) {
  const dependencies = task.dependency_details || [];
  const criteria = task.acceptance_criteria || [];
  const view = taskView(task);
  const phaseSummary = [
    `阶段：${viewLabel(view.phase)}`,
    view.group !== view.phase ? `分组：${viewGroupLabel(view.group)}` : "",
    view.needs_attention ? "已进入需要处理收件箱" : "",
  ].filter(Boolean).join(" · ");
  const specReceipt = formatSpecReceipt(task.spec_receipt);
  elements["task-detail-heading"].textContent = `任务 #${task.task_number} · ${task.title}`;
  elements["task-detail-contract"].innerHTML = `
    <div class="task-contract-header">
      <div><span class="task-number" title="内部 ID：${escapeHtml(task.id)}">任务 #${task.task_number}</span></div>
      <div class="task-meta"><span class="priority p${task.priority}">P${task.priority}</span>${taskViewBadgeHtml(view)}</div>
    </div>
    <dl class="task-contract-grid">
      <div><dt>当前阶段</dt><dd>${escapeHtml(phaseSummary)}</dd></div>
      <div><dt>执行状态</dt><dd title="${escapeHtml(view.execution_status)}">${escapeHtml(executionFaceLabel(view.execution_status))}</dd></div>
      <div><dt>验收状态</dt><dd title="${escapeHtml(view.verification_status)}">${escapeHtml(verificationFaceLabel(view.verification_status))}</dd></div>
      <div><dt>集成状态</dt><dd title="${escapeHtml(view.integration_status)}">${escapeHtml(integrationFaceLabel(view.integration_status))}</dd></div>
      <div><dt>完成度</dt><dd>${escapeHtml(`${task.progress_percent}%`)}</dd></div>
      <div><dt>当前步骤</dt><dd>${escapeHtml(task.current_step || "暂无")}</dd></div>
      <div><dt>下一步</dt><dd>${escapeHtml(task.next_step || "暂无")}</dd></div>
      ${specReceipt ? `<div class="task-contract-wide"><dt>适用规范（认领时版本回执）</dt><dd>${escapeHtml(specReceipt)}</dd></div>` : ""}
      ${task.blocker_reason ? `<div class="task-contract-wide"><dt>阻塞原因</dt><dd>${escapeHtml(task.blocker_reason)}</dd></div>` : ""}
      <div class="task-contract-wide"><dt>正式说明</dt><dd class="task-contract-description">${escapeHtml(task.description || "暂无正式说明")}</dd></div>
      <div class="task-contract-wide"><dt>验收条件</dt><dd>${criteria.length ? `<ul class="readonly-list">${criteria.map((criterion) => `<li>${escapeHtml(criterion)}</li>`).join("")}</ul>` : "暂无验收条件"}</dd></div>
      ${dependencies.length ? `<div class="task-contract-wide task-dependencies"><dt>前置依赖</dt><dd><ul class="readonly-list">${dependencies.map((dependency) => `<li><span><strong>任务 #${dependency.task_number}</strong> · ${escapeHtml(dependency.title)} · ${escapeHtml(taskPhaseLabel(dependency))}</span><button type="button" class="link-button" data-related-task-id="${escapeHtml(dependency.id)}">查看</button></li>`).join("")}</ul></dd></div>` : ""}
    </dl>`;
}

async function openTaskDetails(taskId, options = {}) {
  const task = state.snapshot?.tasks.find((item) => item.id === taskId);
  if (!task || !state.projectId) return;
  state.editingTaskId = taskId;
  state.focusEventId = Number(options.focusEventId || 0);
  if (options.eventType !== undefined) {
    elements["task-history-filter"].value = options.eventType || "";
  }
  state.taskHistory = {
    taskId,
    items: [],
    eventType: elements["task-history-filter"].value || "",
    has_more_after: false,
    has_more_before: false,
  };
  renderTaskContract(task);
  renderTaskAssignments(task);
  renderTaskTimeline(task);
  elements["task-assign-button"].disabled = ["done", "cancelled"].includes(taskView(task).phase);
  elements["task-release-button"].classList.toggle("is-hidden", !taskReleaseVisible(task));
  elements["task-cancel-button"].classList.toggle("is-hidden", !taskCancelVisible(task));
  elements["task-edit-dialog"].showModal();
  try {
    await loadTaskHistory(taskId);
    if (state.focusEventId && !(state.taskHistory.items || []).some((item) => item.event_id === state.focusEventId)) {
      await loadTaskHistoryUntil(taskId, state.focusEventId);
    }
    const currentTask = state.snapshot?.tasks.find((item) => item.id === taskId) || task;
    renderTaskTimeline(currentTask);
  } catch (error) {
    showToast("任务历史暂时无法加载，已显示当前缓存", "error");
  }
}

async function loadTaskHistoryUntil(taskId, eventId) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    if ((state.taskHistory.items || []).some((item) => item.event_id === eventId)) return;
    if (!state.taskHistory.has_more_before && !state.taskHistory.has_more_after) return;
    if (state.taskHistory.next_before && eventId < (state.taskHistory.items[0]?.event_id || eventId + 1)) {
      await loadTaskHistory(taskId, { direction: "earlier" });
    } else if (state.taskHistory.has_more_after) {
      await loadTaskHistory(taskId, { direction: "later" });
    } else {
      await loadTaskHistory(taskId, { direction: "earlier" });
    }
  }
}

async function copyEventReference(eventId, taskNumber, projectSeq) {
  // 用户可读编号是项目内序号；全局 ID 保留在深链里用于内部定位。
  const seq = Number(projectSeq);
  const label = Number.isFinite(seq) && seq > 0 ? `事件 #${seq}` : `事件 #${eventId}`;
  const text = taskNumber ? `任务 #${taskNumber} / ${label}` : label;
  location.hash = `event-${eventId}`;
  try {
    await navigator.clipboard.writeText(text);
    showToast("已复制事件引用");
  } catch (error) {
    showToast(text);
  }
}

function eventFromCaches(eventId) {
  return (state.events || []).find((event) => Number(event.id) === Number(eventId))
    || (state.taskHistory?.items || []).find((item) => Number(item.event_id) === Number(eventId));
}

async function openEventReference(eventId) {
  const event = eventFromCaches(eventId);
  const taskId = event?.task_id || state.editingTaskId;
  if (!taskId) {
    showToast("当前缓存中没有该事件的任务关联", "error");
    return;
  }
  await openTaskDetails(taskId, { focusEventId: Number(eventId) });
}

async function openTaskAssignmentDialog() {
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task || !state.projectId) return;
  await refreshTaskIntakeTargets();
  // 候选来自持久化的非吊销成员：在线成员用当前 Session 立即派发，
  // 离线成员用持久身份登记延迟指派，重新接入后可受理。
  const options = state.taskIntakeTargets.map((target) => {
    const liveSession = (target.active_session_ids || [])[0];
    const connection = liveSession ? "已连接" : "已接入 · 当前离线";
    const value = liveSession ? `session:${liveSession}` : `member:${target.member_id}`;
    return `<option value="${escapeHtml(value)}">${escapeHtml(target.name)} · ${escapeHtml(target.client || target.software_key || "Agent")} · ${connection}</option>`;
  });
  elements["task-assign-title"].textContent = `指定/改派：任务 #${task.task_number}`;
  elements["task-assign-agent"].innerHTML = options.length ? '<option value="">请选择目标 Agent</option>' + options.join("") : '<option value="">暂无已接入的 Agent</option>';
  elements["task-assign-agent"].disabled = !options.length;
  elements["task-assign-agent-empty"].textContent = options.length
    ? "在线 Agent 立即派发；标注「已接入 · 当前离线」的 Agent 会在重新接入后受理该指派。"
    : "当前没有已接入且未吊销的 Agent。";
  elements["task-assign-submit"].disabled = !options.length;
  elements["task-assign-note"].value = "";
  elements["task-assign-dialog"].showModal();
}

async function downloadProjectExport() {
  if (!state.projectId) return;
  const payload = await api(`/api/v1/projects/${state.projectId}/export`);
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `agentchatroom-${state.projectId}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  showToast("项目数据已导出");
}

function renderIntegrationTabs() {
  const container = document.querySelector(".integration-tabs");
  if (!container || !state.integration?.profiles) return;
  // 配置助手只保留通用标准 MCP 接入；具名客户端预设仅保留在后端 CLI 里。
  const profileIds = Object.keys(state.integration.profiles).filter((id) => id === "generic");
  if (!profileIds.length) return;
  if (!profileIds.includes(state.integrationFormat)) {
    state.integrationFormat = profileIds[0];
  }
  container.innerHTML = profileIds.map((profileId) => `
    <button type="button" ${profileId === state.integrationFormat ? 'class="is-active" ' : ""}data-integration-format="${escapeHtml(profileId)}">${escapeHtml(state.integration.profiles[profileId].label || profileId)}</button>`).join("");
}

function localMcpAssistantSupported() {
  const profile = state.integration?.profiles?.[state.integrationFormat];
  return state.integrationOnboardingMode === "first_setup"
    && state.integrationTransport === "local" && Boolean(profile?.local_config);
}

function localMcpIdentity() {
  const profile = state.integration?.profiles?.[state.integrationFormat] || {};
  const softwareKey = profile.software_key || "";
  const softwareClient = profile.software_client || "";
  return (state.snapshot?.agent_identities || []).find((agent) => (
    (softwareKey && agent.software_key === softwareKey)
    || (softwareClient && agent.client === softwareClient)
    || (profile.label && agent.name === profile.label)
  )) || null;
}

function localMcpPresence() {
  return localMcpIdentity()?.connection_status === "connected";
}

function renderLocalMcpFacts() {
  const plan = state.integrationLocalPlan;
  const identity = localMcpIdentity();
  const softwareConfigured = plan?.state === "current";
  const processConnected = identity?.connection_status === "connected";
  const roomSession = Number(identity?.active_session_count || 0) > 0;
  const projectName = state.integration?.project?.name || "";
  const facts = [
    {
      label: "工作区绑定",
      value: projectName ? `针对 ${projectName}` : "针对当前项目",
      state: projectName ? "ready" : "pending",
      note: "配置只针对当前工作区生成，不能跨工作区复用",
    },
    {
      label: "软件配置",
      value: !plan ? "正在检测" : softwareConfigured ? "已配置" : "未就绪",
      state: !plan ? "loading" : softwareConfigured ? "ready" : "pending",
      note: softwareConfigured
        ? "配置存在不代表客户端当前已经连接"
        : "配置助手只负责首次安装或明确缺失配置",
    },
    {
      label: "进程连接",
      value: processConnected ? "MCP Presence 已连接" : "当前 Room 尚未连接",
      state: processConnected ? "ready" : "pending",
      note: "Presence 不是当前模型对话同步",
    },
    {
      label: "Room Session",
      value: roomSession ? "已有活动 Session" : "无活动 Session",
      state: roomSession ? "ready" : "pending",
      note: "自动上线不能代替新对话的 room_bootstrap",
    },
    {
      label: "当前对话同步",
      value: "浏览器无法观察",
      state: "unknown",
      note: "新对话请调用一次 room_bootstrap",
    },
  ];
  elements["integration-local-facts"].innerHTML = facts.map((fact) => `
    <div class="local-mcp-fact" data-state="${escapeHtml(fact.state)}">
      <dt>${escapeHtml(fact.label)}</dt>
      <dd><strong>${escapeHtml(fact.value)}</strong><span>${escapeHtml(fact.note)}</span></dd>
    </div>`).join("");
}

function renderLocalMcpPlan() {
  const section = elements["integration-local-assistant"];
  const supported = localMcpAssistantSupported();
  section.hidden = !supported;
  if (!supported) return;

  const profile = state.integration.profiles[state.integrationFormat];
  const plan = state.integrationLocalPlan;
  renderLocalMcpFacts();
  elements["integration-local-backup"].textContent = state.integrationLocalApplyResult?.backup_path
    ? `本次备份：${state.integrationLocalApplyResult.backup_path}`
    : "";
  elements["integration-local-refresh"].disabled = !plan;
  elements["integration-local-apply"].disabled = true;

  if (!plan) {
    elements["integration-local-state"].textContent = "正在检测";
    elements["integration-local-state"].dataset.state = "loading";
    elements["integration-local-mode"].textContent = "读取客户端现有 MCP 配置";
    elements["integration-local-path"].textContent = profile.config_path_hint || "";
    elements["integration-local-message"].textContent = "只检测，不会在打开页面时自动写入。";
    elements["integration-local-changes"].textContent = "";
    elements["integration-local-reload"].textContent = "";
    return;
  }

  const stateLabels = {
    current: "配置已是最新",
    unconfigured: "可以添加配置",
    outdated: "可以更新配置",
    missing: "未发现配置文件",
    invalid: "配置文件 JSON 无效",
    unreadable: "配置文件不可读取",
    unwritable: "配置文件不可写",
    unavailable: "当前部署不可代写",
  };
  const modeLabels = {
    managed_write: "一键配置",
    assisted: "辅助配置",
    manual: "手动配置",
  };
  const messages = {
    current: "agentchatroom 配置内容已匹配。配置存在不代表客户端当前已经连接。",
    unconfigured: "检测到有效配置文件，可在确认后只新增 agentchatroom Server。",
    outdated: "检测到旧的 agentchatroom 配置，可在确认后只更新这一项。",
    missing: "没有找到经过验证且实际存在的配置文件，请在客户端 MCP 界面手动添加。",
    invalid: "现有文件不是有效 JSON，为避免破坏客户端配置，已停止自动处理。",
    unreadable: "服务无法读取该文件，请检查权限后重试或在客户端内手动配置。",
    unwritable: "服务不会自动提权，请在客户端内手动配置或修复文件权限。",
    unavailable: "LAN/服务器部署不能修改 Agent 电脑上的配置，请复制下方配置手动添加。",
  };
  elements["integration-local-state"].textContent = stateLabels[plan.state] || plan.state;
  elements["integration-local-state"].dataset.state = plan.state;
  elements["integration-local-mode"].textContent = `${modeLabels[plan.mode] || plan.mode}${plan.detected_profile ? ` · ${plan.detected_profile}` : ""}`;
  elements["integration-local-path"].textContent = plan.config_path
    || plan.candidate_paths?.[0]
    || profile.config_path_hint
    || "未检测到配置路径";
  elements["integration-local-message"].textContent = messages[plan.state] || plan.message;
  elements["integration-local-changes"].textContent = plan.changed_fields?.length
    ? `将变更：${plan.changed_fields.join("、")}`
    : "不会改动其他 MCP Server。";
  elements["integration-local-reload"].textContent = plan.reload_instruction || "";
  elements["integration-local-refresh"].disabled = false;
  elements["integration-local-apply"].disabled = !plan.managed_apply_available;
}

async function refreshLocalMcpPlan(announce = false) {
  if (!state.snapshot || !localMcpAssistantSupported()) {
    state.integrationLocalPlan = null;
    renderLocalMcpPlan();
    return;
  }
  const projectId = state.snapshot.project.id;
  const profileId = state.integrationFormat;
  const requestId = ++state.integrationLocalRequest;
  state.integrationLocalPlan = null;
  renderLocalMcpPlan();
  try {
    const plan = await api(`/api/v1/projects/${projectId}/integrations/mcp/local/${encodeURIComponent(profileId)}/plan`);
    if (requestId !== state.integrationLocalRequest || profileId !== state.integrationFormat || state.integrationTransport !== "local") return;
    state.integrationLocalPlan = plan;
    renderLocalMcpPlan();
    if (announce) showToast("已重新检测客户端 MCP 配置");
  } catch (error) {
    if (requestId !== state.integrationLocalRequest) return;
    elements["integration-local-state"].textContent = "检测失败";
    elements["integration-local-state"].dataset.state = "invalid";
    elements["integration-local-message"].textContent = "未修改任何客户端配置；可重新检测或使用下方手动配置。";
    elements["integration-local-refresh"].disabled = false;
    handleError(error);
  }
}

async function applyLocalMcpPlan() {
  if (!localMcpAssistantSupported()) return;
  const plan = state.integrationLocalPlan;
  if (!state.snapshot || !plan?.managed_apply_available) return;
  const confirmed = window.confirm(
    `将先备份 ${plan.config_path}，然后只新增或更新 mcpServers.agentchatroom。是否继续？`
  );
  if (!confirmed) return;
  const button = elements["integration-local-apply"];
  button.disabled = true;
  button.textContent = "正在应用...";
  try {
    const projectId = state.snapshot.project.id;
    const profileId = state.integrationFormat;
    const result = await api(
      `/api/v1/projects/${projectId}/integrations/mcp/local/${encodeURIComponent(profileId)}/apply`,
      {
        method: "POST",
        body: JSON.stringify({ expected_current_sha256: plan.current_sha256 }),
      }
    );
    state.integrationLocalApplyResult = result;
    state.integrationLocalPlan = result.plan;
    renderLocalMcpPlan();
    showToast("配置已备份并写入；请重启或新开会话，等待当前 Room 显示已连接");
  } catch (error) {
    handleError(error);
    await refreshLocalMcpPlan();
  } finally {
    button.textContent = "应用配置";
    button.disabled = !localMcpAssistantSupported() || !state.integrationLocalPlan?.managed_apply_available;
  }
}

async function openIntegrationDialog() {
  if (!state.snapshot) return;
  const project = state.snapshot.project;
  state.integration = await api(`/api/v1/projects/${project.id}/integrations/mcp`);
  state.integrationTransport = "http";
  elements["integration-data-dir"].textContent = state.integration.runtime.data_dir;
  elements["integration-log-path"].textContent = state.integration.runtime.log_path;
  elements["integration-cli-code"].textContent = [
    "agentchatroom",
    "--url", JSON.stringify(window.location.origin),
    "room-join", JSON.stringify(project.id),
    "--software-key", "SOFTWARE_CODE",
    "--name", "SOFTWARE_NAME",
    "--client", "SOFTWARE_CLIENT",
    "--model", "MODEL_CODE_OR_UNKNOWN",
  ].join(" ");
  renderIntegrationTabs();
  document.querySelectorAll("[data-integration-transport]").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.integrationTransport === state.integrationTransport);
  });
  renderOnboardingPrompt();
  renderIntegrationConfig();
  renderIntegrationJoin();
  renderProjectInstructions();
  state.integrationLocalPlan = null;
  state.integrationLocalApplyResult = null;
  renderLocalMcpPlan();
  elements["integration-dialog"].showModal();
  void refreshLocalMcpPlan();
}

function integrationTransportKey() {
  if (state.integrationTransport === "http") return "http";
  if (state.integrationTransport === "remote") return "remote";
  return "local";
}

function integrationTransportLabel() {
  if (state.integrationTransport === "http") return "HTTP 直连";
  if (state.integrationTransport === "remote") return "远程 Bridge";
  return "本机 stdio";
}

function renderOnboardingPrompt() {
  if (!state.integration) return;
  const profile = state.integration.profiles?.[state.integrationFormat];
  const mode = state.integrationOnboardingMode;
  const transport = integrationTransportKey();
  elements["integration-onboarding-prompt"].textContent = profile?.onboarding_modes?.[mode]?.[transport]
    || (mode === "first_setup" ? profile?.onboarding_prompts?.[transport] || (transport === "local" ? state.integration.onboarding_prompt : "") : "")
    || "当前场景的接入指令尚未生成，请更新服务后重新打开。不要套用首次配置指令。";
}

function renderHttpTokenGuide() {
  const guide = elements["integration-http-token-guide"];
  if (!guide) return;
  guide.hidden = state.integrationTransport !== "http";
  const mode = state.integrationOnboardingMode;
  const projectName = state.snapshot?.project?.name || "当前 Project";
  elements["integration-target-project"].textContent = projectName;
  if (mode === "reconnect") {
    elements["integration-http-action-title"].textContent = "生成恢复连接提示词";
    elements["integration-http-action-description"].textContent = "不签发新 Token，不新增 MCP；生成一份让 Agent 使用现有 HTTP 配置恢复当前 Project 连接的提示词。";
    elements["integration-open-token-button"].textContent = "生成恢复提示词";
    elements["integration-http-action-steps"].innerHTML = `
      <li>确认客户端已经保存名为 <code>agentchatroom</code> 的 HTTP MCP。</li>
      <li>复制恢复提示词交给 Agent，要求它重载连接并调用指定 Project 的 <code>room_bootstrap</code>。</li>
      <li>提示词会要求核对 Project 名称与路径；不重新签发或替换已有 Token。</li>`;
    return;
  }
  const addProject = mode === "add_project";
  elements["integration-http-action-title"].textContent = addProject
    ? "签发本项目 Token 并生成增量提示词"
    : "签发并生成首次接入提示词";
  elements["integration-http-action-description"].textContent = addProject
    ? "无需粘贴现有配置。签发当前 Project Token，生成一段交给已配置 Agent 的增量提示词：由 Agent 读取客户端本地现有配置，保留原有 Project 凭据，仅合并本项目。"
    : "签发当前 Project 的 Token，生成一份包含可读 Project↔Token 映射、真实 MCP 配置和首次接入指令的完整提示词。";
  elements["integration-open-token-button"].textContent = addProject
    ? "签发并生成增量提示词"
    : "签发并生成接入提示词";
  elements["integration-http-action-steps"].innerHTML = addProject ? `
      <li>只需确认客户端、Token 名称、有效期、可选成员与权限，不需要粘贴任何现有配置。</li>
      <li>签发后生成一段增量提示词：Agent 自行检查客户端本地的 <code>agentchatroom</code> HTTP 配置，保留原有 URL、软件身份和旧 Project 凭据，仅合并新凭据并写回同一条目。</li>
      <li>若 Agent 无法读取本地配置，改用签发弹窗底部「高级 · 故障恢复」手动粘贴合并。</li>` : `
      <li>新数据库可保持“不关联”；首次连接成功时自动创建该软件成员。只有需要沿用已有身份时才关联旧成员。</li>
      <li>签发结果只生成一个可复制区：Project↔Token 明文映射、含凭据包的 MCP 配置和首次接入指令都在其中。</li>
      <li>把整段内容交给 Agent；Agent 按客户端格式写入名为 <code>agentchatroom</code> 的 MCP，并调用指定 Project 的 <code>room_bootstrap</code>。</li>`;
}

function renderIntegrationConfig() {
  if (!state.integration) return;
  const profile = state.integration.profiles?.[state.integrationFormat];
  const transport = integrationTransportKey();
  const configText = transport === "http"
    ? (profile?.streamable_http_config_text || state.integration.streamable_http_json_text)
    : transport === "remote"
      ? (profile?.remote_bridge_config_text || state.integration.remote_bridge_config_text)
      : (profile?.local_config_text || state.integration.generic_json_text);
  elements["integration-config-code"].textContent = configText || "";
  if (elements["integration-config-path"]) {
    const target = profile?.config_path_hint
      ? `建议配置文件：${profile.config_path_hint}`
      : "标准 MCP 配置片段";
    elements["integration-config-path"].textContent = `${target} · ${integrationTransportLabel()}`;
  }
  renderHttpTokenGuide();
}

function renderProjectInstructions() {
  if (!state.integration) return;
  const profile = state.integration.profiles?.[state.integrationFormat];
  const pathHint = profile?.project_instruction_path_hint;
  elements["integration-project-rules-path"].textContent = pathHint
    ? `项目文件：${pathHint}；全局 SOUL 只保留通用判断原则`
    : "放入该客户端实际读取的项目级指令或记忆文件";
  elements["integration-project-rules-code"].textContent = profile?.project_instructions_text
    || state.integration.project_instructions_text
    || "当前项目尚未生成协作规则。";
}

function renderIntegrationJoin() {
  if (!state.integration || !state.snapshot) return;
  const project = state.snapshot.project;
  const payload = {
    project_path: project.root_path,
    model: "<actual model or unknown>",
    role: "executor",
    branch: "<git branch>",
    worktree: project.root_path,
  };
  elements["integration-join-code"].textContent = JSON.stringify(payload, null, 2);
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch (_error) {
      // Some HTTP or embedded browser contexts expose Clipboard without granting writes.
    }
  }
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("浏览器未允许复制，请手动选择文本");
}

function populateDomainOptions() {
  const kinds = state.config?.domain?.message_kinds || ["message", "decision", "blocker"];
  elements["message-kind"].innerHTML = kinds.map((kind) => `<option value="${escapeHtml(kind)}">${escapeHtml(messageKind(kind))}</option>`).join("");
  if (kinds.includes("message")) elements["message-kind"].value = "message";
  const channels = state.config?.domain?.message_channels || ["public", "task", "review", "system"];
  elements["message-channel"].innerHTML = channels.map((channel) => `<option value="${escapeHtml(channel)}">${escapeHtml(messageChannel(channel))}频道</option>`).join("");
  if (channels.includes("public")) elements["message-channel"].value = "public";
}

function resolveAppearanceTheme(preference, configuredTheme, systemDark) {
  const selected = ["light", "dark"].includes(preference) ? preference : configuredTheme;
  return selected === "system" ? (systemDark ? "dark" : "light")
    : selected === "dark" ? "dark" : "light";
}

function applyAppearanceTheme() {
  document.documentElement.dataset.theme = resolveAppearanceTheme(
    document.getElementById("appearance-theme").value, state.config?.default_theme,
    window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
}

function initializeAppearance() {
  const control = document.getElementById("appearance-theme");
  try {
    const saved = localStorage.getItem("agentchatroom.appearance.theme");
    control.value = ["light", "dark"].includes(saved) ? saved : "default";
  } catch (_error) { /* Restricted WebViews can still change the current window. */ }
  control.addEventListener("change", () => {
    try { localStorage.setItem("agentchatroom.appearance.theme", control.value); }
    catch (_error) { /* The visible preference works without persistent storage. */ }
    applyAppearanceTheme();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyAppearanceTheme);
  applyAppearanceTheme();
}

function applyPublicConfig() {
  applyAppearanceTheme();
  populateDomainOptions();
  elements["product-name"].textContent = state.config.product_name;
  document.title = state.config.product_name;
  const folderPickerEnabled = Boolean(state.config.capabilities?.local_folder_picker);
  elements["project-folder-picker-button"].disabled = !folderPickerEnabled;
  elements["project-folder-picker-button"].title = folderPickerEnabled
    ? "打开系统文件夹选择器"
    : "当前部署模式请手动输入服务器上的项目路径";
}

async function loadAuthenticatedApp() {
  state.config = await api("/api/v1/config/public");
  applyPublicConfig();
  await loadProjects();
  state.authenticated = true;
  elements["logout-button"].classList.toggle("is-hidden", !state.authRequired);
  setConnection("online", state.projectId ? "浏览器已连接" : "服务在线");
}

window.addEventListener("beforeunload", () => {
  closeEventSource();
  stopPresenceRefresh();
});

async function bootstrap() {
  try {
    const auth = await api("/api/v1/auth/status");
    state.authRequired = Boolean(auth.required);
    state.authenticated = Boolean(auth.authenticated);
    if (!state.authenticated) {
      showLoginDialog();
      return;
    }
    await loadAuthenticatedApp();
  } catch (error) {
    if (error.code === "management_auth_required") return;
    setConnection("offline", "服务不可用");
    handleError(error);
    renderEmptyRoom();
  }
}

initializeAppearance();
initializePanelLayout();
document.querySelectorAll(".tab").forEach((item) => {
  item.tabIndex = item.classList.contains("is-active") ? 0 : -1;
});
bootstrap();
