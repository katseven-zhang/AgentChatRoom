// Evaluate the actual activity projection without a browser or live backend.
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/agentchatroom/web/app.js', 'utf8');
const start = source.indexOf('const LIFECYCLE_ACTIVITY_TYPES');
const end = source.indexOf('function lifecycleActivitySummary', start);
const renderStart = source.indexOf('if (item.merged && item.count >= 3)');
const renderEnd = source.indexOf('const modelBadge', renderStart);
const sandbox = {escapeHtml: String, lifecycleActivitySummary: () => '', eventIdBadge: () => '', formatTime: () => ''};
vm.createContext(sandbox);
vm.runInContext(source.slice(start, end), sandbox);
for (const count of [1, 2, 3]) {
  sandbox.events = Array.from({length: count}, (_, i) => ({id:i+1, event_type:'agent.joined', actor:{name:'Test'}, payload:{}}));
  try {
    vm.runInContext(`mergeLifecycleActivity(events).map(item => { ${source.slice(renderStart, renderEnd)} return isMessage; })`, sandbox);
    console.log(JSON.stringify({count, result:'ok'}));
  } catch (error) {
    console.log(JSON.stringify({count, result:error.name, message:error.message}));
  }
}
