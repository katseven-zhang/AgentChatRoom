const state = {
  config: null,
  integration: null,
  integrationFormat: "workbuddy",
  integrationTransport: "local",
  authRequired: false,
  authenticated: false,
  projects: [],
  projectId: localStorage.getItem("agentchatroom.projectId") || null,
  projectKey: localStorage.getItem("agentchatroom.projectKey") || null,
  snapshot: null,
  events: [],
  eventIds: new Set(),
  eventSource: null,
  presenceTimer: null,
  presenceRefreshInFlight: false,
  taskFilter: "",
  eventFilter: "all",
  editingTaskId: null,
  editingMemberId: null,
  members: [],
  credentials: [],
  workspaces: [],
  auditEvents: [],
  runtime: null,
  expandedEvents: new Set(),
  collapsedGroups: new Set(),
  lastRenderedEventId: 0,
};

const elements = Object.fromEntries(
  [
    "app-shell", "left-panel-resizer", "right-panel-resizer",
    "product-name", "connection-state", "connection-label", "project-count",
    "project-list", "agent-count", "agent-list", "room-name", "room-path",
    "create-task-button", "archive-project-button", "project-settings-button", "export-project-button",
    "connect-agent-button", "logout-button",
    "metric-agents", "metric-active", "metric-leases",
    "metric-reviews", "active-task-list", "recent-event-list", "task-table",
    "lease-list", "review-list", "chat-subtitle", "chat-stream", "event-filter",
    "message-form", "message-input", "message-kind", "message-channel", "message-task", "message-priority",
    "message-requires-ack", "send-message-button", "onboarding", "new-message-notice",
    "project-dialog", "project-form", "project-name-input", "project-path-input",
    "project-logical-path-input", "project-key-input",
    "task-dialog", "task-form", "task-title-input", "task-description-input",
    "task-criteria-input", "task-priority-input", "task-dependency-options",
    "task-edit-dialog", "task-edit-form", "task-edit-id", "task-edit-title",
    "task-edit-description", "task-edit-criteria", "task-edit-dependencies",
    "task-edit-status", "task-edit-priority", "task-edit-progress",
    "task-edit-current-step", "task-edit-blocker", "task-edit-next-step", "task-edit-owner",
    "settings-dialog", "settings-form", "settings-project-name", "settings-lease-policy", "settings-roles",
    "archive-dialog", "archive-form", "archive-project-name", "permanent-delete-input",
    "remove-project-hint", "remove-project-submit",
    "integration-dialog", "integration-data-dir", "integration-log-path",
    "integration-config-path", "integration-config-code", "integration-join-code", "integration-cli-code",
    "integration-project-rules-path", "integration-project-rules-code", "integration-onboarding-prompt", "toast-region",
    "create-member-button", "member-list", "refresh-audit-button", "audit-event-filter",
    "create-token-button", "register-workspace-button", "token-list", "workspace-list", "audit-list",
    "refresh-runtime-button", "runtime-status", "runtime-config", "runtime-log",
    "login-dialog", "login-form", "login-token", "login-error",
    "member-dialog", "member-form", "member-dialog-title", "member-id", "member-key", "member-name",
    "member-kind", "member-role", "member-status", "member-metadata", "member-submit",
    "token-dialog", "token-form", "token-name", "token-member", "token-days", "token-permissions",
    "token-secret-dialog", "token-secret-value", "token-secret-close",
    "workspace-dialog", "workspace-form", "workspace-host-name", "workspace-host-key",
    "workspace-local-path", "workspace-branch", "workspace-worktree", "workspace-git-remote",
    "task-assign-button", "task-assignment-list", "task-assign-dialog", "task-assign-form",
    "task-assign-title", "task-assign-agent", "task-assign-role", "task-assign-capability", "task-assign-note",
    "task-handoff-button", "task-handoff-list", "task-handoff-dialog", "task-handoff-form",
    "task-handoff-title", "task-handoff-agent", "task-handoff-summary", "task-handoff-completed",
    "task-handoff-pending", "task-handoff-files", "task-handoff-risks", "task-handoff-next-step",
    "task-integration-button", "task-integration-list", "task-integration-dialog", "task-integration-form",
    "task-integration-title", "task-integration-result", "task-integration-summary",
    "task-integration-files", "task-integration-commit", "task-integration-tests",
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
    online: "在线", idle: "空闲", working: "工作中", blocked: "阻塞", offline: "离线", registered: "已接入",
  }[status] || status;
}

function projectSource(project) {
  return project.git_remote ? "Git" : "本地路径";
}

function connectedAgentCount(agentIdentities) {
  return agentIdentities.filter((agent) => agent.connection_status === "connected").length;
}

function taskStatus(status) {
  return {
    todo: "待认领", claimed: "已认领", in_progress: "进行中", blocked: "阻塞",
    awaiting_review: "待验证", verified: "已验证", done: "已完成", cancelled: "已取消",
  }[status] || status;
}

function executionStatus(status) {
  return {
    todo: "待开始", claimed: "已认领", in_progress: "执行中", blocked: "阻塞",
    completed: "执行完成", cancelled: "已取消",
  }[status] || status;
}

function verificationStatus(status) {
  return {
    not_required: "无需验证", pending: "待验证", changes_requested: "需修改", approved: "已通过",
  }[status] || status;
}

function integrationStatus(status) {
  return { pending: "待集成", done: "已集成", failed: "集成失败" }[status] || status;
}

function assignmentStatus(status) {
  return {
    pending: "待确认", accepted: "已接受", declined: "已拒绝", blocked: "受阻", cancelled: "已取消",
  }[status] || status;
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
    "task.unblocked": "解除了任务阻塞", "task.released": "释放了任务",
    "task.cancelled": "取消了任务", "lease.acquired": "占用了文件范围",
    "lease.released": "释放了文件范围", "lease.conflict": "检测到文件冲突",
    "work.reported": "提交了工作证据", "review.submitted": "提交了验证结论",
    "task.integration_completed": "完成了最终集成", "task.integration_failed": "记录了集成失败",
    "message.acknowledged": "确认了消息",
    "message.message": "发布了消息", "message.decision": "发布了决策",
    "message.blocker": "发布了阻塞", "message.system": "发布了系统消息",
    "credential.issued": "签发了 Agent Token", "credential.rotated": "轮换了 Agent Token",
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

function eventIdBadge(eventId) {
  const value = Number(eventId);
  if (!Number.isFinite(value)) return "";
  return `<span class="event-id" title="事件 ID ${value}" aria-label="事件 ID ${value}">#${value}</span>`;
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

function renderMessageLines(lines) {
  return lines.map((line) => {
    const trimmed = line.trim();
    const heading = trimmed.match(/^【(.+)】/);
    if (heading) {
      return `<div class="msg-heading">${escapeHtml(trimmed)}</div>`;
    }
    if (/^(✓|✔|✅)/.test(trimmed)) {
      return `<div class="msg-line msg-pass">${escapeHtml(line)}</div>`;
    }
    if (/^(✗|✘|❌)/.test(trimmed)) {
      return `<div class="msg-line msg-fail">${escapeHtml(line)}</div>`;
    }
    if (/^[-•·]\s+/.test(trimmed)) {
      return `<div class="msg-line msg-list-item">${escapeHtml(line)}</div>`;
    }
    const keyValue = trimmed.match(/^([A-Z][A-Z_]{2,}|[\u4e00-\u9fa5A-Za-z]{2,12})(\s*[:：]\s*)(\S.*)$/);
    if (keyValue && !trimmed.startsWith("http")) {
      return `<div class="msg-line"><span class="msg-key">${escapeHtml(keyValue[1])}${escapeHtml(keyValue[2])}</span>${escapeHtml(keyValue[3])}</div>`;
    }
    return `<div class="msg-line">${escapeHtml(line)}</div>`;
  }).join("");
}

function renderMessageBody(eventId, body) {
  const lines = String(body || "").split("\n");
  const expanded = state.expandedEvents.has(eventId);
  if (lines.length <= MESSAGE_COLLAPSE_LINES || expanded) {
    const toggle = lines.length > MESSAGE_COLLAPSE_LINES
      ? `<button class="expand-toggle" type="button" data-collapse-event="${eventId}">收起</button>`
      : "";
    return `<div class="event-body">${renderMessageLines(lines)}</div>${toggle}`;
  }
  return `<div class="event-body is-collapsed">${renderMessageLines(lines.slice(0, MESSAGE_PREVIEW_LINES))}</div>
    <button class="expand-toggle" type="button" data-expand-event="${eventId}">展开全部（共 ${lines.length} 行）</button>`;
}

function showToast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  elements["toast-region"].append(item);
  setTimeout(() => item.remove(), 3600);
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

async function loadProjects() {
  const result = await api("/api/v1/projects");
  state.projects = result.projects;
  let selected = state.projects.find((project) => project.id === state.projectId);
  if (!selected && state.projectKey) {
    selected = state.projects.find((project) => project.project_key === state.projectKey);
  }
  if (!selected) selected = state.projects[0] || null;
  state.projectId = selected?.id || null;
  state.projectKey = selected?.project_key || null;
  if (selected) {
    localStorage.setItem("agentchatroom.projectId", selected.id);
    localStorage.setItem("agentchatroom.projectKey", selected.project_key);
  } else {
    localStorage.removeItem("agentchatroom.projectId");
    localStorage.removeItem("agentchatroom.projectKey");
  }
  renderProjects();
  if (state.projectId) {
    await selectProject(state.projectId);
  } else {
    renderEmptyRoom();
  }
}

async function selectProject(projectId) {
  const selected = state.projects.find((project) => project.id === projectId);
  state.projectId = projectId;
  state.projectKey = selected?.project_key || null;
  localStorage.setItem("agentchatroom.projectId", projectId);
  if (state.projectKey) localStorage.setItem("agentchatroom.projectKey", state.projectKey);
  state.events = [];
  state.eventIds = new Set();
  renderProjects();
  closeEventSource();
  const [snapshot, eventPage, members, credentials, workspaces, audit, runtime] = await Promise.all([
    api(`/api/v1/projects/${projectId}/snapshot`),
    api(`/api/v1/projects/${projectId}/events?after=0&limit=500`),
    api(`/api/v1/projects/${projectId}/members`),
    api(`/api/v1/projects/${projectId}/agent-tokens`),
    api(`/api/v1/projects/${projectId}/workspaces`),
    api(`/api/v1/projects/${projectId}/audit?after=0&limit=100`),
    api("/api/v1/admin/runtime?lines=80"),
  ]);
  state.snapshot = snapshot;
  state.members = members.members;
  state.credentials = credentials.credentials;
  state.workspaces = workspaces.workspaces;
  state.auditEvents = audit.events;
  state.runtime = runtime;
  mergeEvents(eventPage.events);
  renderAll();
  connectEvents(snapshot.cursor);
  startPresenceRefresh();
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
  state.presenceRefreshInFlight = true;
  try {
    const snapshot = await api(`/api/v1/projects/${state.projectId}/snapshot`);
    if (snapshot.project.id !== state.projectId) return;
    state.snapshot = snapshot;
    state.members = snapshot.members || state.members;
    const agentIdentities = snapshot.agent_identities || [];
    renderAgents(agentIdentities);
    renderMetrics(agentIdentities, snapshot.tasks, snapshot.leases);
    renderLeases(snapshot.leases, snapshot.agents);
    elements["chat-subtitle"].textContent = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent / 累计 ${snapshot.agents.length} 次接入 · 游标 ${snapshot.cursor}`;
  } catch (error) {
    setConnection("offline", "浏览器正在重连");
  } finally {
    state.presenceRefreshInFlight = false;
  }
}

function connectEvents(after) {
  if (!state.projectId) return;
  setConnection("connecting", "浏览器正在连接");
  const source = new EventSource(`/api/v1/projects/${state.projectId}/events/stream?after=${after}`);
  state.eventSource = source;
  source.onopen = () => setConnection("online", "浏览器已连接");
  source.onerror = () => setConnection("offline", "浏览器正在重连");
  source.addEventListener("room_event", async (message) => {
    const event = JSON.parse(message.data);
    mergeEvents([event]);
    try {
      state.snapshot = await api(`/api/v1/projects/${state.projectId}/snapshot`);
      renderAll();
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
    elements["project-list"].innerHTML = '<div class="empty-state">还没有项目</div>';
    return;
  }
  const groups = new Map();
  for (const project of state.projects) {
    const key = projectGroupKey(project);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(project);
  }
  const showHeaders = groups.size > 1 || [...groups.values()].some((projects) => projects.length > 1);
  elements["project-list"].innerHTML = [...groups.entries()].map(([groupKey, projects]) => {
    const collapsed = showHeaders && state.collapsedGroups.has(groupKey);
    const items = collapsed ? "" : projects.map((project) => `
      <button class="project-item ${project.id === state.projectId ? "is-active" : ""}" data-project-id="${escapeHtml(project.id)}" type="button"
        title="${escapeHtml(`${project.name}\n${project.root_path}\nKey: ${project.project_key}\nID: ${project.id}\n来源: ${projectSource(project)}`)}">
        <span class="project-indicator"></span>
        <span class="project-copy">
          <strong>${escapeHtml(project.name)}</strong>
          <small class="project-identity">${escapeHtml(project.project_key)} · ${escapeHtml(shortId(project.id))} · ${escapeHtml(projectSource(project))}</small>
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
}

function renderEmptyRoom() {
  stopPresenceRefresh();
  state.snapshot = null;
  state.events = [];
  state.members = [];
  state.credentials = [];
  state.workspaces = [];
  state.auditEvents = [];
  elements["room-name"].textContent = "尚未添加项目";
  elements["room-path"].textContent = "添加本地项目后即可开始协作";
  elements["chat-subtitle"].textContent = "等待选择项目";
  elements["onboarding"].classList.remove("is-hidden");
  ["create-task-button", "archive-project-button", "project-settings-button", "export-project-button", "connect-agent-button",
    "create-member-button", "create-token-button", "register-workspace-button", "refresh-audit-button", "audit-event-filter",
    "event-filter", "message-input", "message-kind", "message-channel", "message-task", "message-priority",
    "message-requires-ack", "send-message-button"]
    .forEach((id) => { elements[id].disabled = true; });
  elements["agent-count"].textContent = "0";
  elements["agent-list"].innerHTML = '<div class="empty-state">Agent 通过「接入 Agent」里的配置加入后，会显示在这里</div>';
  elements["chat-stream"].innerHTML = '<div class="empty-state">Room 动态会实时显示在这里：Agent 加入、任务进展和消息按时间排列</div>';
  elements["task-table"].innerHTML = '<div class="empty-state">还没有任务。任务是把工作交给 Agent 的最小单元：点右上角「+ 新建任务」，写清要做什么和验收条件。</div>';
  elements["token-list"].innerHTML = '<div class="empty-state">选择项目后管理 Agent Token</div>';
  elements["member-list"].innerHTML = '<div class="empty-state">选择项目后管理项目成员</div>';
  elements["workspace-list"].innerHTML = '<div class="empty-state">选择项目后查看 Workspace</div>';
  elements["audit-list"].innerHTML = '<div class="empty-state">选择项目后查看审计历史</div>';
}

function renderAll() {
  if (!state.snapshot) return renderEmptyRoom();
  const { project, agents, tasks, leases } = state.snapshot;
  const agentIdentities = state.snapshot.agent_identities || [];
  state.members = state.snapshot.members || state.members;
  state.projects = state.projects.map((item) => item.id === project.id ? project : item);
  renderProjects();
  elements["room-name"].textContent = project.name;
  elements["room-path"].textContent = project.root_path;
  elements["chat-subtitle"].textContent = `${connectedAgentCount(agentIdentities)} 当前连接 / ${agentIdentities.length} 个 Agent / 累计 ${agents.length} 次接入 · 游标 ${state.snapshot.cursor}`;
  elements["onboarding"].classList.add("is-hidden");
  ["create-task-button", "archive-project-button", "project-settings-button", "export-project-button", "connect-agent-button",
    "create-member-button", "create-token-button", "register-workspace-button", "refresh-audit-button", "audit-event-filter",
    "event-filter", "message-input", "message-kind", "message-channel", "message-task", "message-priority",
    "message-requires-ack", "send-message-button"]
    .forEach((id) => { elements[id].disabled = false; });
  renderAgents(agentIdentities);
  renderMetrics(agentIdentities, tasks, leases);
  renderTasks(tasks);
  renderLeases(leases, agents);
  renderReviews(tasks, agents);
  renderEvents(agents, tasks);
  renderMessageTaskOptions(tasks);
  renderManagement();
}

function renderAgents(agents) {
  elements["agent-count"].textContent = agents.length;
  const ordered = [...agents].sort((left, right) => {
    const leftDisconnected = left.connection_status === "disconnected" ? 1 : 0;
    const rightDisconnected = right.connection_status === "disconnected" ? 1 : 0;
    return leftDisconnected - rightDisconnected;
  });
  elements["agent-list"].innerHTML = ordered.length
    ? ordered.map((agent) => {
      const connected = agent.connection_status === "connected";
      const heartbeat = formatRelativeTime(agent.last_heartbeat);
      const activity = formatRelativeTime(agent.last_activity_at);
      const connectionSummary = connected
        ? `${agent.active_session_count} 当前连接 · 累计接入 ${agent.session_count} 次`
        : `已接入 · 未连接 · 累计接入 ${agent.session_count} 次`;
      const presenceStatus = connected ? agent.status : "disconnected";
      const presenceLabel = connected ? agentStatus(agent.status) : "未连接";
      const details = `${agent.name}\nAgent key: ${agent.agent_key}\n客户端: ${agent.client}\n角色: ${agent.role}\n${connectionSummary}\n最后心跳: ${heartbeat}\n最后活动: ${activity}`;
      return `
      <div class="agent-item ${connected ? "" : "is-disconnected"}" title="${escapeHtml(details)}">
        <span class="agent-avatar ${avatarColorClass(agent.id)}">${escapeHtml(initials(agent.name))}</span>
        <span class="agent-copy">
          <strong>${escapeHtml(agent.name)}${agent.unread_count ? ` <span class="unread-count" title="未读事件数：该 Agent 最前沿 Session 的已读游标之后、尚未同步的 Room 事件数">${agent.unread_count}</span>` : ""}</strong>
          <small>${escapeHtml(agent.client)} · ${escapeHtml(agent.role)}</small>
          <small>${escapeHtml(connectionSummary)}</small>
          <small>心跳 ${escapeHtml(heartbeat)} · 活动 ${escapeHtml(activity)}</small>
        </span>
        <span class="agent-presence ${escapeHtml(presenceStatus)}"><span class="status-dot ${escapeHtml(presenceStatus)}"></span>${escapeHtml(presenceLabel)}</span>
      </div>`;
    }).join("")
    : '<div class="empty-state">等待 Agent 通过 MCP 或 CLI 加入</div>';
}

function renderMetrics(agents, tasks, leases) {
  elements["metric-agents"].textContent = agents.length;
  elements["metric-active"].textContent = tasks.filter((task) => ["claimed", "in_progress", "blocked"].includes(task.status)).length;
  elements["metric-leases"].textContent = leases.length;
  elements["metric-reviews"].textContent = tasks.filter((task) => task.status === "awaiting_review").length;

  const active = tasks.filter((task) => !["todo", "done"].includes(task.status)).slice(0, 6);
  elements["active-task-list"].innerHTML = active.length
    ? active.map((task) => `
      <div class="compact-item">
        <div class="task-meta"><span class="priority p${task.priority}">P${task.priority}</span><span class="status-badge ${escapeHtml(task.status)}">${escapeHtml(taskStatus(task.status))}</span></div>
        <p><strong>${escapeHtml(task.title)}</strong></p>
      </div>`).join("")
    : '<div class="empty-state">当前没有进行中的工作。点「+ 新建任务」把第一件事派出去，Agent 的进展会显示在这里。</div>';

  const recent = state.events.slice(-6).reverse();
  elements["recent-event-list"].innerHTML = recent.length
    ? recent.map((event) => {
      const isMessage = event.event_type.startsWith("message.") && event.payload?.body !== undefined;
      const modelBadge = isMessage ? messageModelBadge(event) : "";
      const preview = isMessage
        ? renderMessageLines(String(event.payload.body).split("\n").slice(0, 3))
        : `<div class="msg-line">${escapeHtml(event.payload?.title || event.payload?.path_pattern || formatTime(event.created_at))}</div>`;
      return `
      <div class="compact-item ${isMessage ? `kind-${escapeHtml(event.event_type.split(".")[1])}` : ""}">
        <div class="compact-heading"><span><strong>${escapeHtml(eventLabel(event.event_type))}</strong>${modelBadge}</span>${eventIdBadge(event.id)}</div>
        <div class="compact-body">${preview}</div>
      </div>`;
    }).join("")
    : '<div class="empty-state">还没有动态。Agent 加入、认领任务、提交报告都会按时间显示在这里。</div>';
}

function renderTasks(tasks) {
  const filtered = state.taskFilter ? tasks.filter((task) => task.status === state.taskFilter) : tasks;
  const names = Object.fromEntries((state.snapshot?.agents || []).map((agent) => [agent.id, agent.name]));
  elements["task-table"].innerHTML = filtered.length
    ? filtered.map((task) => `
      <button class="task-row" type="button" data-task-id="${escapeHtml(task.id)}">
        <span class="priority p${task.priority}">P${task.priority}</span>
        <div>
          <h3>${escapeHtml(task.title)}</h3>
          <p>${escapeHtml(task.description || task.acceptance_criteria.join(" · ") || "尚未填写说明")}${task.current_step ? ` · 当前：${escapeHtml(task.current_step)}` : ""}${task.blocker_reason ? ` · 阻塞：${escapeHtml(task.blocker_reason)}` : ""}</p>
        </div>
        <div class="task-meta">
          <span class="status-badge ${escapeHtml(task.execution_status)}">${escapeHtml(executionStatus(task.execution_status))}</span>
          <span class="status-badge ${escapeHtml(task.verification_status)}">${escapeHtml(verificationStatus(task.verification_status))}</span>
          <span class="status-badge ${escapeHtml(task.integration_status)}">${escapeHtml(integrationStatus(task.integration_status))}</span>
          <span class="secondary-text">${task.progress_percent}%${task.owner_session_id ? ` · ${escapeHtml(names[task.owner_session_id] || shortId(task.owner_session_id))}` : ""}${task.depends_on?.length ? ` · 依赖 ${task.depends_on.length} 项` : ""}</span>
        </div>
      </button>`).join("")
    : '<div class="empty-state">当前筛选下没有任务。点「+ 新建任务」写清要做什么和验收条件，Agent 可以认领或等派发。</div>';
}

function renderLeases(leases, agents) {
  const names = Object.fromEntries(agents.map((agent) => [agent.id, agent.name]));
  elements["lease-list"].innerHTML = leases.length
    ? leases.map((lease) => `
      <article class="lease-item">
        <div class="lease-meta">
          <span class="type-badge">${escapeHtml(leaseMode(lease.mode))}</span>
          <strong>${escapeHtml(lease.path_pattern)}</strong>
        </div>
        <p>${escapeHtml(names[lease.session_id] || shortId(lease.session_id))} · ${escapeHtml(leaseMode(lease.mode))} · TTL ${lease.ttl_seconds}s · 到期 ${escapeHtml(formatTime(lease.expires_at))}${lease.reason ? ` · ${escapeHtml(lease.reason)}` : ""}</p>
      </article>`).join("")
    : '<div class="empty-state">当前没有活跃文件占用。Agent 编辑文件前会在这里声明路径范围，避免两个人同时改同一文件。</div>';
}

function renderReviews(tasks, agents) {
  const names = Object.fromEntries(agents.map((agent) => [agent.id, agent.name]));
  const awaiting = tasks.filter((task) => task.status === "awaiting_review");
  const completed = state.snapshot.reviews.slice(0, 8);
  const items = [
    ...awaiting.map((task) => ({ type: "awaiting", task })),
    ...completed.map((review) => ({ type: "review", review, task: tasks.find((task) => task.id === review.task_id) })),
  ];
  elements["review-list"].innerHTML = items.length
    ? items.map((item) => item.type === "awaiting" ? `
      <article class="review-item">
        <div class="task-meta"><span class="status-badge awaiting_review">待验证</span><strong>${escapeHtml(item.task.title)}</strong></div>
        <p>等待独立 Agent 检查 ${item.task.acceptance_criteria.length} 条验收条件</p>
        ${renderReportEvidence(item.task.id)}
      </article>` : `
      <article class="review-item">
        <div class="task-meta"><span class="status-badge ${item.review.verdict === "approved" ? "verified" : "blocked"}">${item.review.verdict === "approved" ? "通过" : "退回"}</span><strong>${escapeHtml(item.task?.title || shortId(item.review.task_id))}</strong></div>
        <p>${escapeHtml(names[item.review.reviewer_session_id] || shortId(item.review.reviewer_session_id))} · ${escapeHtml(item.review.notes || `${item.review.criteria.length} 条验收记录`)}</p>
        <div class="criteria-list">${item.review.criteria.map((criterion) => `<span class="criterion ${escapeHtml(criterion.status)}">${escapeHtml(criterion.status)} · ${escapeHtml(criterion.criterion)}</span>`).join("")}</div>
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
    '<option value="">不关联成员</option>',
    ...activeMembers.map((member) => `<option value="${escapeHtml(member.id)}">${escapeHtml(member.name)} · ${escapeHtml(member.member_key)}</option>`),
  ].join("");
  if (activeMembers.some((member) => member.id === selected)) {
    elements["token-member"].value = selected;
  }
}

function renderManagement() {
  renderRuntime();
  renderMembers();
  renderCredentials();
  renderWorkspaces();
  renderAudit();
}

function renderRuntime() {
  const runtime = state.runtime;
  if (!runtime) {
    elements["runtime-status"].innerHTML = '<div class="empty-state">暂无运行状态</div>';
    elements["runtime-config"].textContent = "";
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
  elements["runtime-config"].textContent = JSON.stringify({
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
          <button type="button" class="secondary-button" data-member-action="edit" data-member-id="${escapeHtml(member.id)}">编辑</button>
          ${member.status !== "revoked" ? `<button type="button" class="danger-button" data-member-action="revoke" data-member-id="${escapeHtml(member.id)}">吊销</button>` : ""}
        </div>
      </article>`).join("")
    : '<div class="empty-state">尚未登记项目成员</div>';
  renderTokenMemberOptions(elements["token-member"].value);
}

function renderCredentials() {
  const memberNames = Object.fromEntries(state.members.map((member) => [member.id, member.name]));
  elements["token-list"].innerHTML = state.credentials.length
    ? state.credentials.map((credential) => `
      <article class="management-item">
        <div>
          <h4>${escapeHtml(credential.name)} <span class="status-badge ${credential.active ? "verified" : "cancelled"}">${credential.active ? "有效" : "已失效"}</span></h4>
          <p>${credential.member_id ? `成员 ${escapeHtml(memberNames[credential.member_id] || shortId(credential.member_id))} · ` : ""}${escapeHtml(credential.permissions.map(permissionLabel).join(" · "))}</p>
          <p>到期 ${escapeHtml(formatTime(credential.expires_at))}${credential.last_used_at ? ` · 最近使用 ${escapeHtml(formatTime(credential.last_used_at))}` : " · 尚未使用"}</p>
        </div>
        <div class="management-actions">
          ${credential.active ? `<button type="button" class="secondary-button" data-token-action="rotate" data-credential-id="${escapeHtml(credential.id)}">轮换</button>
          <button type="button" class="danger-button" data-token-action="revoke" data-credential-id="${escapeHtml(credential.id)}">吊销</button>` : ""}
        </div>
      </article>`).join("")
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

function renderAudit() {
  elements["audit-list"].innerHTML = state.auditEvents.length
    ? state.auditEvents.slice().reverse().map((event) => `
      <article class="management-item">
        <time class="audit-time">${escapeHtml(formatTime(event.created_at))}</time>
        <div>
          <h4>${escapeHtml(eventLabel(event.event_type))} ${eventIdBadge(event.id)}</h4>
          <p>${escapeHtml(event.task_id ? `任务 ${shortId(event.task_id)}` : event.actor_session_id ? `接入 ${shortId(event.actor_session_id)}` : "管理主体")}</p>
        </div>
      </article>`).join("")
    : '<div class="empty-state">当前筛选下没有审计事件</div>';
}

async function refreshManagement() {
  if (!state.projectId) return;
  const eventType = elements["audit-event-filter"].value;
  const [members, credentials, workspaces, audit, runtime] = await Promise.all([
    api(`/api/v1/projects/${state.projectId}/members`),
    api(`/api/v1/projects/${state.projectId}/agent-tokens`),
    api(`/api/v1/projects/${state.projectId}/workspaces`),
    api(`/api/v1/projects/${state.projectId}/audit?after=0&limit=100${eventType ? `&event_type=${encodeURIComponent(eventType)}` : ""}`),
    api("/api/v1/admin/runtime?lines=80"),
  ]);
  state.members = members.members;
  state.credentials = credentials.credentials;
  state.workspaces = workspaces.workspaces;
  state.auditEvents = audit.events;
  state.runtime = runtime;
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

function renderEvents(agents, tasks) {
  const agentMap = Object.fromEntries(agents.map((agent) => [agent.id, agent]));
  const taskMap = Object.fromEntries(tasks.map((task) => [task.id, task]));
  let events = [...state.events];
  if (state.eventFilter === "messages") events = events.filter((event) => event.event_type.startsWith("message.") && event.payload?.body !== undefined);
  if (state.eventFilter === "decisions") events = events.filter((event) => ["message.decision", "message.blocker"].includes(event.event_type));
  events = events.slice(-120);
  const stream = elements["chat-stream"];
  const distanceFromBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
  const wasAtBottom = distanceFromBottom < 40;
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
            <div class="event-meta"><span class="event-author"><strong>${escapeHtml(agent?.name || "用户")}</strong>${messageModelBadge(event)}${kind !== "message" ? ` <span class="type-badge ${escapeHtml(kind)}">${escapeHtml(messageKind(kind))}</span>` : ""}${event.payload?.priority <= 1 ? ` <span class="type-badge urgent">${event.payload.priority === 0 ? "紧急" : "高优先级"}</span>` : ""}</span><span class="event-stamp">${eventIdBadge(event.id)}<time title="${escapeHtml(event.created_at)}">${escapeHtml(formatTime(event.created_at))}</time></span></div>
            ${renderMessageBody(event.id, event.payload?.body || "")}
            <p class="secondary-text">${escapeHtml(messageChannel(event.channel))}频道${event.payload?.requires_ack ? ` · 需要确认 · 已确认 ${ackCount}` : ""}${event.payload?.mentions?.length ? ` · @${escapeHtml(event.payload.mentions.join("、"))}` : ""}</p>
            ${event.payload?.files?.length ? `<p class="secondary-text">关联文件：${escapeHtml(event.payload.files.join("、"))}</p>` : ""}
            ${task ? `<p class="secondary-text">关联任务：${escapeHtml(task.title)}</p>` : ""}
          </div>
        </article>`;
      }
      const conflict = event.event_type === "lease.conflict"
        ? ` · ${escapeHtml(event.payload?.path_pattern || "")} 与 ${escapeHtml((event.payload?.conflicts || []).map((item) => `${item.agent_name}:${item.path_pattern}`).join("、"))} 冲突`
        : "";
      return `<div class="system-event">${eventIdBadge(event.id)} <strong>${escapeHtml(agent?.name || "系统")}</strong> ${escapeHtml(eventLabel(event.event_type))}${task ? ` · ${escapeHtml(task.title)}` : ""}${conflict} <span>· ${escapeHtml(formatTime(event.created_at))}</span></div>`;
    }).join("")
    : '<div class="empty-state">当前筛选下没有动态</div>';
  const notice = elements["new-message-notice"];
  if (wasAtBottom) {
    stream.scrollTop = stream.scrollHeight;
    notice.classList.add("is-hidden");
  } else {
    stream.scrollTop = previousScrollTop;
    if (hasNewEvents) notice.classList.remove("is-hidden");
  }
  state.lastRenderedEventId = latestEventId;
}

document.getElementById("add-project-button").addEventListener("click", () => elements["project-dialog"].showModal());
document.getElementById("create-task-button").addEventListener("click", () => {
  renderDependencyOptions();
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
  elements["settings-dialog"].showModal();
});
document.getElementById("export-project-button").addEventListener("click", () => downloadProjectExport().catch(handleError));
document.getElementById("refresh-button").addEventListener("click", () => state.projectId && selectProject(state.projectId).catch(handleError));
elements["connect-agent-button"].addEventListener("click", () => openIntegrationDialog().catch(handleError));
elements["logout-button"].addEventListener("click", async () => {
  try {
    await api("/api/v1/auth/logout", { method: "POST", body: "{}" });
  } finally {
    showLoginDialog();
  }
});
elements["create-member-button"].addEventListener("click", () => {
  state.editingMemberId = null;
  elements["member-form"].reset();
  elements["member-id"].value = "";
  elements["member-key"].disabled = false;
  elements["member-status"].value = "active";
  elements["member-metadata"].value = "{}";
  elements["member-dialog-title"].textContent = "添加项目成员";
  elements["member-submit"].textContent = "保存";
  elements["member-dialog"].showModal();
});
elements["create-token-button"].addEventListener("click", () => {
  const permissions = state.config?.domain?.agent_permissions || [];
  const defaults = new Set(permissions.filter((permission) => permission !== "audit:read"));
  elements["token-permissions"].innerHTML = permissions.map((permission) => `
    <label class="check-control">
      <input type="checkbox" value="${escapeHtml(permission)}" ${defaults.has(permission) ? "checked" : ""}>
      <span>${escapeHtml(permissionLabel(permission))}</span>
    </label>`).join("");
  elements["token-form"].reset();
  renderTokenMemberOptions();
  elements["token-days"].value = "30";
  elements["token-dialog"].showModal();
});
elements["register-workspace-button"].addEventListener("click", () => {
  elements["workspace-form"].reset();
  elements["workspace-dialog"].showModal();
});
elements["refresh-audit-button"].addEventListener("click", () => refreshManagement().catch(handleError));
elements["audit-event-filter"].addEventListener("change", () => refreshManagement().catch(handleError));
elements["refresh-runtime-button"].addEventListener("click", () => refreshManagement().catch(handleError));
elements["task-assign-button"].addEventListener("click", openTaskAssignmentDialog);
elements["task-handoff-button"].addEventListener("click", openTaskHandoffDialog);
elements["task-integration-button"].addEventListener("click", openTaskIntegrationDialog);
elements["token-secret-close"].addEventListener("click", () => {
  elements["token-secret-value"].textContent = "";
  elements["token-secret-dialog"].close();
});

document.querySelectorAll(".dialog-close").forEach((button) => {
  button.addEventListener("click", () => button.closest("dialog").close());
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
});

document.querySelector(".integration-transport-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-integration-transport]");
  if (!button) return;
  state.integrationTransport = button.dataset.integrationTransport;
  document.querySelectorAll("[data-integration-transport]").forEach((item) => {
    item.classList.toggle("is-active", item === button);
  });
  renderIntegrationConfig();
  renderIntegrationJoin();
  renderOnboardingPrompt();
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
  if (button) selectProject(button.dataset.projectId).catch(handleError);
});

elements["chat-stream"].addEventListener("click", (event) => {
  const expand = event.target.closest("[data-expand-event]");
  if (expand) {
    state.expandedEvents.add(Number(expand.dataset.expandEvent));
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
});

elements["task-table"].addEventListener("click", (event) => {
  const button = event.target.closest("[data-task-id]");
  if (button) openTaskEditor(button.dataset.taskId);
});

elements["token-list"].addEventListener("click", async (event) => {
  const button = event.target.closest("[data-token-action]");
  if (!button || !state.projectId) return;
  const credentialId = button.dataset.credentialId;
  try {
    if (button.dataset.tokenAction === "rotate") {
      if (!window.confirm("轮换后旧 Token 会立即失效，确定继续吗？")) return;
      const result = await api(`/api/v1/projects/${state.projectId}/agent-tokens/${credentialId}/rotate`, {
        method: "POST",
        body: JSON.stringify({ expires_in_seconds: 30 * 24 * 60 * 60 }),
      });
      showTokenSecret(result.token);
      showToast("Token 已轮换");
    } else if (button.dataset.tokenAction === "revoke") {
      if (!window.confirm("吊销后使用该 Token 的新连接会被拒绝，确定继续吗？")) return;
      await api(`/api/v1/projects/${state.projectId}/agent-tokens/${credentialId}`, { method: "DELETE" });
      showToast("Token 已吊销");
    }
    await refreshManagement();
  } catch (error) {
    handleError(error);
  }
});

elements["member-list"].addEventListener("click", async (event) => {
  const button = event.target.closest("[data-member-action]");
  if (!button || !state.projectId) return;
  const member = state.members.find((item) => item.id === button.dataset.memberId);
  if (!member) return;
  try {
    if (button.dataset.memberAction === "edit") {
      state.editingMemberId = member.id;
      elements["member-id"].value = member.id;
      elements["member-key"].value = member.member_key;
      elements["member-key"].disabled = true;
      elements["member-name"].value = member.name;
      elements["member-kind"].value = member.kind;
      elements["member-role"].value = member.role || "";
      elements["member-status"].value = member.status;
      elements["member-metadata"].value = JSON.stringify(member.metadata || {}, null, 2);
      elements["member-dialog-title"].textContent = "编辑项目成员";
      elements["member-submit"].textContent = "保存修改";
      elements["member-dialog"].showModal();
      return;
    }
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

document.querySelector(".tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tab]");
  if (!button) return;
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("is-active", item === button));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("is-active", panel.id === `panel-${button.dataset.tab}`));
});

document.getElementById("task-filter").addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state.taskFilter = button.dataset.filter;
  document.querySelectorAll("#task-filter button").forEach((item) => item.classList.toggle("is-active", item === button));
  if (state.snapshot) renderTasks(state.snapshot.tasks);
});

elements["event-filter"].addEventListener("change", () => {
  state.eventFilter = elements["event-filter"].value;
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
    const project = await api("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify({
        name: elements["project-name-input"].value.trim() || null,
        root_path: elements["project-path-input"].value.trim(),
        logical_path: elements["project-logical-path-input"].value.trim(),
        project_key: elements["project-key-input"].value.trim() || null,
      }),
    });
    elements["project-dialog"].close();
    elements["project-form"].reset();
    state.projectId = project.id;
    await loadProjects();
    showToast("项目已添加");
  } catch (error) {
    handleError(error);
  }
});

elements["task-edit-status"].addEventListener("change", () => {
  elements["task-edit-blocker"].required = elements["task-edit-status"].value === "blocked";
});

elements["task-edit-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  const contractLocked = ["awaiting_review", "verified", "done"].includes(task.status);
  const body = {
    status: elements["task-edit-status"].value,
    description: elements["task-edit-description"].value.trim(),
    priority: Number(elements["task-edit-priority"].value),
    progress_percent: Number(elements["task-edit-progress"].value),
    current_step: elements["task-edit-current-step"].value.trim(),
    blocker_reason: elements["task-edit-blocker"].value.trim(),
    next_step: elements["task-edit-next-step"].value.trim(),
  };
  if (!contractLocked) {
    body.title = elements["task-edit-title"].value.trim();
    body.acceptance_criteria = elements["task-edit-criteria"].value.split("\n").map((item) => item.trim()).filter(Boolean);
    body.depends_on = [...elements["task-edit-dependencies"].querySelectorAll("input:checked")].map((input) => input.value);
  }
  try {
    await api(`/api/v1/projects/${state.projectId}/tasks/${task.id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    elements["task-edit-dialog"].close();
    showToast("任务已更新");
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
        },
      }),
    });
    elements["settings-dialog"].close();
    showToast("项目设置已保存");
  } catch (error) {
    handleError(error);
  }
});

elements["task-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(`/api/v1/projects/${state.projectId}/tasks`, {
      method: "POST",
      body: JSON.stringify({
        title: elements["task-title-input"].value.trim(),
        description: elements["task-description-input"].value.trim(),
        acceptance_criteria: elements["task-criteria-input"].value.split("\n").map((item) => item.trim()).filter(Boolean),
        depends_on: [...elements["task-dependency-options"].querySelectorAll("input:checked")].map((input) => input.value),
        priority: Number(elements["task-priority-input"].value),
      }),
    });
    elements["task-dialog"].close();
    elements["task-form"].reset();
    showToast("任务已创建");
  } catch (error) {
    handleError(error);
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
    localStorage.removeItem("agentchatroom.projectKey");
    state.projectId = null;
    state.projectKey = null;
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
    await api("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ token: elements["login-token"].value }),
    });
    state.authenticated = true;
    elements["login-token"].value = "";
    elements["login-dialog"].close();
    elements["logout-button"].classList.remove("is-hidden");
    await loadAuthenticatedApp();
  } catch (error) {
    elements["login-error"].textContent = error.message || "登录失败";
  }
});

elements["member-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) return;
  let metadata;
  try {
    metadata = JSON.parse(elements["member-metadata"].value.trim() || "{}");
  } catch (_error) {
    showToast("元数据必须是合法 JSON", "error");
    return;
  }
  if (!metadata || Array.isArray(metadata) || typeof metadata !== "object") {
    showToast("元数据必须是 JSON 对象", "error");
    return;
  }
  const body = {
    name: elements["member-name"].value.trim(),
    kind: elements["member-kind"].value.trim(),
    role: elements["member-role"].value.trim(),
    status: elements["member-status"].value,
    metadata,
  };
  if (!state.editingMemberId) body.member_key = elements["member-key"].value.trim();
  try {
    await api(
      state.editingMemberId
        ? `/api/v1/projects/${state.projectId}/members/${state.editingMemberId}`
        : `/api/v1/projects/${state.projectId}/members`,
      {
        method: state.editingMemberId ? "PATCH" : "POST",
        body: JSON.stringify(body),
      },
    );
    elements["member-dialog"].close();
    showToast(state.editingMemberId ? "项目成员已更新" : "项目成员已创建");
    state.editingMemberId = null;
    await refreshManagement();
  } catch (error) {
    handleError(error);
  }
});

elements["token-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) return;
  const permissions = [...elements["token-permissions"].querySelectorAll("input:checked")]
    .map((input) => input.value);
  if (!permissions.length) {
    showToast("至少选择一项权限", "error");
    return;
  }
  try {
    const result = await api(`/api/v1/projects/${state.projectId}/agent-tokens`, {
      method: "POST",
      body: JSON.stringify({
        name: elements["token-name"].value.trim(),
        member_id: elements["token-member"].value || null,
        permissions,
        expires_in_seconds: Number(elements["token-days"].value) * 24 * 60 * 60,
      }),
    });
    elements["token-dialog"].close();
    showTokenSecret(result.token);
    await refreshManagement();
  } catch (error) {
    handleError(error);
  }
});

elements["workspace-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.projectId) return;
  try {
    await api(`/api/v1/projects/${state.projectId}/workspaces`, {
      method: "POST",
      body: JSON.stringify({
        host_key: elements["workspace-host-key"].value.trim(),
        host_name: elements["workspace-host-name"].value.trim(),
        local_path: elements["workspace-local-path"].value.trim(),
        branch: elements["workspace-branch"].value.trim(),
        worktree: elements["workspace-worktree"].value.trim(),
        git_remote: elements["workspace-git-remote"].value.trim(),
      }),
    });
    elements["workspace-dialog"].close();
    await refreshManagement();
    showToast("Workspace 已登记");
  } catch (error) {
    handleError(error);
  }
});

elements["task-assign-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  const assignedTo = elements["task-assign-agent"].value;
  const targetRole = elements["task-assign-role"].value.trim();
  const capability = elements["task-assign-capability"].value.trim();
  if (!assignedTo && !targetRole && !capability) {
    showToast("请指定 Agent、角色或能力", "error");
    return;
  }
  try {
    await api(`/api/v1/projects/${state.projectId}/tasks/${task.id}/assignments`, {
      method: "POST",
      body: JSON.stringify({
        assigned_to_session_id: assignedTo || null,
        target_role: targetRole,
        required_capability: capability,
        note: elements["task-assign-note"].value.trim(),
      }),
    });
    elements["task-assign-dialog"].close();
    state.snapshot = await api(`/api/v1/projects/${state.projectId}/snapshot`);
    renderAll();
    const updated = state.snapshot.tasks.find((item) => item.id === task.id);
    if (updated) renderTaskAssignments(updated);
    showToast("任务已派发");
  } catch (error) {
    handleError(error);
  }
});

elements["task-handoff-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  try {
    await api(`/api/v1/projects/${state.projectId}/tasks/${task.id}/handoffs`, {
      method: "POST",
      body: JSON.stringify({
        to_session_id: elements["task-handoff-agent"].value,
        summary: elements["task-handoff-summary"].value.trim(),
        completed_items: textLines(elements["task-handoff-completed"].value),
        pending_items: textLines(elements["task-handoff-pending"].value),
        files: textLines(elements["task-handoff-files"].value),
        risks: textLines(elements["task-handoff-risks"].value),
        next_step: elements["task-handoff-next-step"].value.trim(),
      }),
    });
    elements["task-handoff-dialog"].close();
    state.snapshot = await api(`/api/v1/projects/${state.projectId}/snapshot`);
    renderAll();
    const updated = state.snapshot.tasks.find((item) => item.id === task.id);
    if (updated) {
      renderTaskHandoffs(updated);
      elements["task-handoff-button"].disabled = true;
    }
    showToast("交接请求已发送");
  } catch (error) {
    handleError(error);
  }
});

elements["task-integration-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  try {
    const tests = parseTestLines(elements["task-integration-tests"].value);
    await api(`/api/v1/projects/${state.projectId}/tasks/${task.id}/integrations`, {
      method: "POST",
      body: JSON.stringify({
        result: elements["task-integration-result"].value,
        summary: elements["task-integration-summary"].value.trim(),
        files: textLines(elements["task-integration-files"].value),
        tests,
        commit_hash: elements["task-integration-commit"].value.trim(),
      }),
    });
    elements["task-integration-dialog"].close();
    elements["task-edit-dialog"].close();
    state.snapshot = await api(`/api/v1/projects/${state.projectId}/snapshot`);
    renderAll();
    showToast("集成结果已记录");
  } catch (error) {
    handleError(error);
  }
});

function showTokenSecret(token) {
  elements["token-secret-value"].textContent = token;
  if (!elements["token-secret-dialog"].open) {
    elements["token-secret-dialog"].showModal();
  }
}

function handleError(error) {
  showToast(error.message || "发生未知错误", "error");
}

function renderDependencyOptions() {
  const candidates = (state.snapshot?.tasks || []).filter((task) => !["done", "cancelled"].includes(task.status));
  elements["task-dependency-options"].innerHTML = candidates.length
    ? candidates.map((task) => `
      <label class="check-control">
        <input type="checkbox" value="${escapeHtml(task.id)}">
        <span>${escapeHtml(task.title)} <span class="secondary-text">${escapeHtml(taskStatus(task.status))}</span></span>
      </label>`).join("")
    : '<span class="secondary-text">当前没有可选择的依赖任务</span>';
}

function renderMessageTaskOptions(tasks) {
  const selected = elements["message-task"].value;
  const candidates = tasks.filter((task) => !["done", "cancelled"].includes(task.status));
  elements["message-task"].innerHTML = [
    '<option value="">不关联任务</option>',
    ...candidates.map((task) => `<option value="${escapeHtml(task.id)}">${escapeHtml(task.title)}</option>`),
  ].join("");
  if (candidates.some((task) => task.id === selected)) elements["message-task"].value = selected;
}

function renderTaskEditDependencies(task, disabled) {
  const candidates = (state.snapshot?.tasks || []).filter((item) => item.id !== task.id && item.status !== "cancelled");
  elements["task-edit-dependencies"].innerHTML = candidates.length
    ? candidates.map((candidate) => `
      <label class="check-control">
        <input type="checkbox" value="${escapeHtml(candidate.id)}" ${task.depends_on.includes(candidate.id) ? "checked" : ""} ${disabled ? "disabled" : ""}>
        <span>${escapeHtml(candidate.title)} <span class="secondary-text">${escapeHtml(taskStatus(candidate.status))}</span></span>
      </label>`).join("")
    : '<span class="secondary-text">当前没有可选择的依赖任务</span>';
}

function renderTaskAssignments(task) {
  const agents = Object.fromEntries(
    (state.snapshot?.agents || []).map((agent) => [agent.id, agent.name])
  );
  elements["task-assignment-list"].innerHTML = task.assignments?.length
    ? task.assignments.slice().reverse().map((assignment) => {
      const target = assignment.assigned_to_session_id
        ? agents[assignment.assigned_to_session_id] || shortId(assignment.assigned_to_session_id)
        : assignment.target_role
          ? `角色 ${assignment.target_role}`
          : `能力 ${assignment.required_capability}`;
      return `<article class="management-item">
        <div>
          <h4>${escapeHtml(target)} <span class="status-badge ${escapeHtml(assignment.status)}">${escapeHtml(assignmentStatus(assignment.status))}</span></h4>
          <p>${escapeHtml(assignment.note || "无附加说明")}${assignment.response_note ? ` · 回复：${escapeHtml(assignment.response_note)}` : ""}</p>
        </div>
        <span class="secondary-text">${escapeHtml(formatTime(assignment.created_at))}</span>
      </article>`;
    }).join("")
    : '<div class="empty-state">尚未派发</div>';
}

function renderTaskHandoffs(task) {
  const agents = Object.fromEntries(
    (state.snapshot?.agents || []).map((agent) => [agent.id, agent.name])
  );
  elements["task-handoff-list"].innerHTML = task.handoffs?.length
    ? task.handoffs.slice().reverse().map((handoff) => `
      <article class="management-item">
        <div>
          <h4>${escapeHtml(agents[handoff.from_session_id] || shortId(handoff.from_session_id))} → ${escapeHtml(agents[handoff.to_session_id] || shortId(handoff.to_session_id))} <span class="status-badge ${escapeHtml(handoff.status)}">${escapeHtml(assignmentStatus(handoff.status))}</span></h4>
          <p>${escapeHtml(handoff.summary)} · 下一步：${escapeHtml(handoff.next_step)}${handoff.response_note ? ` · 回复：${escapeHtml(handoff.response_note)}` : ""}</p>
          ${handoff.pending_items?.length ? `<p class="secondary-text">待完成：${escapeHtml(handoff.pending_items.join("、"))}</p>` : ""}
          ${handoff.files?.length ? `<p class="secondary-text">文件：${escapeHtml(handoff.files.join("、"))}</p>` : ""}
          ${handoff.risks?.length ? `<p class="secondary-text">风险：${escapeHtml(handoff.risks.join("、"))}</p>` : ""}
        </div>
        <span class="secondary-text">${escapeHtml(formatTime(handoff.created_at))}</span>
      </article>`).join("")
    : '<div class="empty-state">尚无交接记录</div>';
}

function renderTaskIntegrations(task) {
  const agents = Object.fromEntries(
    (state.snapshot?.agents || []).map((agent) => [agent.id, agent.name])
  );
  elements["task-integration-list"].innerHTML = task.integrations?.length
    ? task.integrations.slice().reverse().map((integration) => `
      <article class="management-item">
        <div>
          <h4>${escapeHtml(integrationResult(integration.result))} <span class="status-badge ${escapeHtml(integration.result)}">${escapeHtml(integrationStatus(integration.result))}</span></h4>
          <p>${escapeHtml(integration.summary)} · ${escapeHtml(agents[integration.integrator_session_id] || (integration.integrator_session_id ? shortId(integration.integrator_session_id) : "管理端"))}</p>
          ${integration.tests?.length ? `<p class="secondary-text">测试：${escapeHtml(integration.tests.map((item) => `${item.exit_code === 0 ? "通过" : "失败"} · ${item.command}`).join("；"))}</p>` : ""}
          ${integration.commit_hash ? `<p class="secondary-text">Commit：${escapeHtml(shortId(integration.commit_hash))}</p>` : ""}
        </div>
        <span class="secondary-text">${escapeHtml(formatTime(integration.created_at))}</span>
      </article>`).join("")
    : '<div class="empty-state">尚无集成记录</div>';
}

function openTaskEditor(taskId) {
  const task = state.snapshot?.tasks.find((item) => item.id === taskId);
  if (!task) return;
  state.editingTaskId = taskId;
  const contractLocked = ["awaiting_review", "verified", "done"].includes(task.status);
  const transitions = (state.config?.domain?.task_transitions?.[task.status] || [task.status])
    .filter((status) => status === task.status || !["awaiting_review", "verified", "done"].includes(status));
  elements["task-edit-id"].value = task.id;
  elements["task-edit-title"].value = task.title;
  elements["task-edit-description"].value = task.description;
  elements["task-edit-criteria"].value = task.acceptance_criteria.join("\n");
  elements["task-edit-status"].innerHTML = transitions.map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(taskStatus(status))}</option>`).join("");
  elements["task-edit-status"].value = task.status;
  elements["task-edit-priority"].value = String(task.priority);
  elements["task-edit-progress"].value = String(task.progress_percent);
  elements["task-edit-current-step"].value = task.current_step;
  elements["task-edit-blocker"].value = task.blocker_reason;
  elements["task-edit-next-step"].value = task.next_step;
  elements["task-edit-blocker"].required = task.status === "blocked";
  ["task-edit-title", "task-edit-criteria"].forEach((id) => { elements[id].disabled = contractLocked; });
  renderTaskEditDependencies(task, contractLocked);
  const owner = state.snapshot.agents.find((agent) => agent.id === task.owner_session_id);
  elements["task-edit-owner"].textContent = `任务 ${shortId(task.id)} · ${owner ? `负责人 ${owner.name}` : "尚未分配"} · ${executionStatus(task.execution_status)} · ${verificationStatus(task.verification_status)} · ${integrationStatus(task.integration_status)}${contractLocked ? " · 验收契约已锁定" : ""}`;
  renderTaskAssignments(task);
  renderTaskHandoffs(task);
  renderTaskIntegrations(task);
  const handoffAvailable = Boolean(task.owner_session_id)
    && !["completed", "cancelled"].includes(task.execution_status)
    && !(task.handoffs || []).some((handoff) => handoff.status === "pending")
    && (state.snapshot?.agents || []).some((agent) => agent.id !== task.owner_session_id);
  elements["task-handoff-button"].disabled = !handoffAvailable;
  elements["task-integration-button"].disabled = !(
    task.execution_status === "completed"
    && task.verification_status === "approved"
    && task.integration_status !== "done"
  );
  elements["task-edit-dialog"].showModal();
}

function openTaskAssignmentDialog() {
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  elements["task-assign-title"].textContent = `派发：${task.title}`;
  elements["task-assign-agent"].innerHTML = [
    '<option value="">不指定 Agent</option>',
    ...(state.snapshot?.agents || []).map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name)} · ${escapeHtml(agent.role)}</option>`),
  ].join("");
  elements["task-assign-role"].value = "";
  elements["task-assign-capability"].value = "";
  elements["task-assign-note"].value = "";
  elements["task-assign-dialog"].showModal();
}

function openTaskHandoffDialog() {
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  const candidates = (state.snapshot?.agents || []).filter(
    (agent) => agent.id !== task.owner_session_id
  );
  elements["task-handoff-title"].textContent = `交接：${task.title}`;
  elements["task-handoff-agent"].innerHTML = candidates.map(
    (agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name)} · ${escapeHtml(agent.role)}</option>`
  ).join("");
  elements["task-handoff-summary"].value = task.current_step || "";
  elements["task-handoff-completed"].value = "";
  elements["task-handoff-pending"].value = task.next_step || "";
  elements["task-handoff-files"].value = "";
  elements["task-handoff-risks"].value = task.blocker_reason || "";
  elements["task-handoff-next-step"].value = task.next_step || "";
  elements["task-handoff-dialog"].showModal();
}

function openTaskIntegrationDialog() {
  const task = state.snapshot?.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  elements["task-integration-title"].textContent = `集成：${task.title}`;
  elements["task-integration-result"].value = "done";
  elements["task-integration-summary"].value = "";
  elements["task-integration-files"].value = "";
  elements["task-integration-commit"].value = "";
  elements["task-integration-tests"].value = "";
  elements["task-integration-dialog"].showModal();
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
  const profileIds = Object.keys(state.integration.profiles);
  if (!profileIds.length) return;
  if (!profileIds.includes(state.integrationFormat)) {
    state.integrationFormat = profileIds[0];
  }
  container.innerHTML = profileIds.map((profileId) => `
    <button type="button" ${profileId === state.integrationFormat ? 'class="is-active" ' : ""}data-integration-format="${escapeHtml(profileId)}">${escapeHtml(state.integration.profiles[profileId].label || profileId)}</button>`).join("");
}

async function openIntegrationDialog() {
  if (!state.snapshot) return;
  const project = state.snapshot.project;
  state.integration = await api(`/api/v1/projects/${project.id}/integrations/mcp`);
  elements["integration-data-dir"].textContent = state.integration.runtime.data_dir;
  elements["integration-log-path"].textContent = state.integration.runtime.log_path;
  const bridgeEnabled = Boolean(state.integration.transports?.remote_stdio_bridge?.enabled);
  const httpEnabled = Boolean(state.integration.transports?.streamable_http?.enabled);
  const remoteButton = document.querySelector('[data-integration-transport="remote"]');
  const httpButton = document.querySelector('[data-integration-transport="http"]');
  remoteButton.disabled = !bridgeEnabled;
  remoteButton.title = bridgeEnabled ? "通过本地 stdio Bridge 连接中心服务" : "服务端未启用 HTTP MCP";
  httpButton.disabled = !httpEnabled;
  httpButton.title = httpEnabled ? "客户端直接连接中心 Streamable HTTP MCP" : "服务端未启用 HTTP MCP";
  if (!bridgeEnabled && state.integrationTransport === "remote") {
    state.integrationTransport = "local";
  }
  if (!httpEnabled && state.integrationTransport === "http") {
    state.integrationTransport = "local";
  }
  document.querySelectorAll("[data-integration-transport]").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.integrationTransport === state.integrationTransport);
  });
  elements["integration-cli-code"].textContent = [
    "agentchatroom",
    "--url", JSON.stringify(window.location.origin),
    "room-join", JSON.stringify(project.id),
    "--agent-key", "AGENT_KEY",
    "--name", "AGENT_NAME",
    "--client", "CLIENT_NAME",
    "--model", "MODEL_CODE_OR_UNKNOWN",
  ].join(" ");
  renderIntegrationTabs();
  renderOnboardingPrompt();
  renderIntegrationConfig();
  renderIntegrationJoin();
  renderProjectInstructions();
  elements["integration-dialog"].showModal();
}

function renderOnboardingPrompt() {
  if (!state.integration) return;
  const profile = state.integration.profiles?.[state.integrationFormat];
  elements["integration-onboarding-prompt"].textContent = profile?.onboarding_prompts?.[state.integrationTransport]
    || state.integration.onboarding_prompt
    || "当前接入配置尚未生成。";
}

function renderIntegrationConfig() {
  if (!state.integration) return;
  const profile = state.integration.profiles?.[state.integrationFormat];
  const remoteBridge = state.integrationTransport === "remote";
  const streamableHttp = state.integrationTransport === "http";
  const configKey = remoteBridge
    ? "remote_bridge_config_text"
    : streamableHttp
      ? "streamable_http_config_text"
      : "local_config_text";
  elements["integration-config-code"].textContent = profile?.[configKey]
    || (remoteBridge
      ? state.integration.remote_bridge_json_text
      : streamableHttp
        ? state.integration.streamable_http_json_text
        : state.integration.generic_json_text);
  if (elements["integration-config-path"]) {
    const target = profile?.config_path_hint
      ? `建议配置文件：${profile.config_path_hint}`
      : "标准 MCP 配置片段";
    const transport = remoteBridge
      ? `远程 Bridge · ${state.integration.runtime.mcp_http_url}`
      : streamableHttp
        ? `直接 Streamable HTTP · ${state.integration.runtime.mcp_http_url}`
        : "本机 stdio · 直接读写本机数据库（仅限这台电脑，含本机数据目录路径；跨机器请切到远程 Bridge / HTTP）";
    elements["integration-config-path"].textContent = `${target} · ${transport}`;
  }
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
    || "当前项目尚未配置稳定 project_key。";
}

function renderIntegrationJoin() {
  if (!state.integration || !state.snapshot) return;
  const project = state.snapshot.project;
  const remote = state.integrationTransport !== "local";
  const workspacePath = remote ? "<path-to-project-on-this-computer>" : project.root_path;
  const payload = {
    project_path: workspacePath,
    agent_key: "<stable project-scoped agent key>",
    agent_name: "<Agent name>",
    client: state.integration.profiles?.[state.integrationFormat]?.label || "<client name>",
    model: "<actual model or unknown>",
    role: "executor",
    branch: "<git branch>",
    worktree: workspacePath,
    project_key: project.project_key || "",
  };
  if (remote) {
    payload.host_key = "<stable-host-key>";
    payload.host_name = "<computer-name>";
    payload.git_remote = "<git-remote-url>";
  }
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
  const memberStatuses = state.config?.domain?.project_member_statuses || ["invited", "active", "suspended", "revoked"];
  elements["member-status"].innerHTML = memberStatuses.map((status) =>
    `<option value="${escapeHtml(status)}">${escapeHtml(memberStatusLabel(status))}</option>`
  ).join("");
}

function applyPublicConfig() {
  const configuredTheme = state.config.default_theme;
  const resolvedTheme = configuredTheme === "system"
    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : configuredTheme;
  document.documentElement.dataset.theme = resolvedTheme;
  populateDomainOptions();
  elements["product-name"].textContent = state.config.product_name;
  document.title = state.config.product_name;
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

initializePanelLayout();
bootstrap();
