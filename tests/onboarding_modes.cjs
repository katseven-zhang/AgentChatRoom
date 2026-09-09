const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/app.js'), 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  const end = source.indexOf('\n}', start);
  return source.slice(start, end + 2);
}
const profile = {
  local_config: {}, onboarding_prompts: {local: 'legacy install', http: 'http install'},
  onboarding_modes: {
    first_setup: {local: 'install', http: 'http-install'},
    add_project: {local: 'join', http: 'http-join'},
    reconnect: {local: 'recover', http: 'http-recover'},
    migrate_http: {http: 'http-migrate'},
  },
};
const context = {
  state: {integration: {profiles: {generic: profile}}, integrationFormat: 'generic', integrationTransport: 'local'},
  elements: {'integration-onboarding-prompt': {textContent: ''}},
};
vm.createContext(context);
vm.runInContext(
  extract('integrationTransportKey') + '\n' + extract('renderOnboardingPrompt') + '\n' + extract('localMcpAssistantSupported'),
  context,
);
for (const [mode, expected] of Object.entries({first_setup: 'install', add_project: 'join', reconnect: 'recover'})) {
  context.state.integrationOnboardingMode = mode;
  vm.runInContext('renderOnboardingPrompt()', context);
  assert.equal(context.elements['integration-onboarding-prompt'].textContent, expected);
  assert.equal(vm.runInContext('localMcpAssistantSupported()', context), mode === 'first_setup');
}
context.state.integrationTransport = 'http';
context.state.integrationOnboardingMode = 'migrate_http';
vm.runInContext('renderOnboardingPrompt()', context);
assert.equal(context.elements['integration-onboarding-prompt'].textContent, 'http-migrate');
delete profile.onboarding_modes;
context.state.integrationOnboardingMode = 'add_project';
vm.runInContext('renderOnboardingPrompt()', context);
assert.match(context.elements['integration-onboarding-prompt'].textContent, /不要套用首次配置指令/);
context.state.integrationTransport = 'local';
context.state.integrationOnboardingMode = 'first_setup';
vm.runInContext('renderOnboardingPrompt()', context);
assert.equal(context.elements['integration-onboarding-prompt'].textContent, 'legacy install');
context.state.integrationTransport = 'http';
profile.onboarding_modes = {
  first_setup: {local: 'install', http: 'http-install'},
  add_project: {local: 'join', http: 'http-join'},
  reconnect: {local: 'recover', http: 'http-recover'},
  migrate_http: {http: 'http-migrate'},
};
vm.runInContext('renderOnboardingPrompt()', context);
assert.equal(context.elements['integration-onboarding-prompt'].textContent, 'http-install');
console.log('onboarding mode selection and fail-closed fallback passed');
