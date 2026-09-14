// Evaluate the actual recent-activity projection without a browser or live
// backend. #150: the overview card renders a flat reverse-chronological
// stream (no lifecycle merge), 5 rows per page with placeholders.
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/agentchatroom/web/app.js', 'utf8');
const start = source.indexOf('const RECENT_ACTIVITY_PAGE_SIZE');
const helpersEnd = source.indexOf('function auditQueryUrl(', start);
const renderStart = source.indexOf('function renderRecentActivity(', helpersEnd);
const renderEnd = source.indexOf('function renderMetrics(', renderStart);
const sandbox = {
  escapeHtml: String,
  formatTime: () => '12:00',
  eventIdBadge: (seq, id) => `#${seq || id}`,
  messageModelBadge: () => '',
  renderMessageLines: (lines) => lines.join('<br>'),
  eventLabel: (type) => type,
  snapshotAgentName: (sessionId) => ({'s-test': 'Test'})[sessionId] || '',
  gridPageSlice: (items, pageSize, page) => {
    const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    const current = Math.min(Math.max(1, page), totalPages);
    return { current, totalPages, slice: items.slice((current - 1) * pageSize, current * pageSize) };
  },
  gridPagerHtml: () => '',
  state: {
    snapshot: {project: {name: 'Demo'}, agents: [{id: 's-test', name: 'Test'}]},
    events: [],
  },
  elements: {
    'recent-activity-project': {textContent: ''},
    'recent-event-list': {innerHTML: ''},
  },
};
vm.createContext(sandbox);
vm.runInContext(source.slice(start, helpersEnd) + source.slice(renderStart, renderEnd), sandbox);
for (const count of [1, 5, 6, 12]) {
  sandbox.state.events = Array.from({length: count}, (_, i) => ({
    id: i + 1,
    project_seq: i + 1,
    event_type: 'agent.joined',
    actor_session_id: 's-test',
    payload: {},
  }));
  try {
    vm.runInContext('renderRecentActivity()', sandbox);
    const html = sandbox.elements['recent-event-list'].innerHTML;
    if (html.includes('undefined') || html.includes('×')) {
      throw new Error('unexpected placeholder text or merged count marker');
    }
    console.log(JSON.stringify({count, result: 'ok'}));
  } catch (error) {
    console.log(JSON.stringify({count, result: error.name, message: error.message}));
  }
}
