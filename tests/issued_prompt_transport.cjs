const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/app.js'), 'utf8');
const code = source.slice(source.indexOf('async function loadIssuedServerPrompt('), source.indexOf('const PROJECT_CREDENTIAL_BUNDLE_PREFIX'));
const elements = Object.fromEntries(['token-config-copy', 'token-config-format', 'token-config-value', 'token-secret-section', 'token-secret-copy', 'token-secret-value'].map(id => [id, {value: 'codex', disabled: false, hidden: true, textContent: ''}]));
const setup = {projectId: 'demo', projectName: 'Demo', projectCredentials: [{name: 'Demo', token: 'acr.test.disposable'}], softwareIdentity: {softwareKey: 'worker', softwareName: 'Worker', softwareClient: 'client'}};
let sent;
let fail = false;
let resolve;
const context = {
  elements, state: {projectId: 'demo', issuedPromptRequest: 0, issuedHttpSetup: setup},
  normalizedSoftwareIdentity: identity => identity,
  validateProjectCredentials: entries => entries,
  handleError: () => {},
  api: (_url, options) => {
    sent = JSON.parse(options.body);
    return new Promise((done, reject) => { resolve = () => fail ? reject(new Error('offline')) : done({prompt: 'Valid prompt', config_text: 'config'}); });
  },
};
vm.createContext(context);
vm.runInContext(code, context);
(async () => {
  let pending = context.loadIssuedServerPrompt(setup, 'codex', 'first_setup');
  assert.deepEqual(sent.software_identity, {software_key: 'worker', software_name: 'Worker', software_client: 'client'});
  assert.equal(elements['token-config-copy'].disabled, true);
  resolve(); await pending;
  assert.equal(elements['token-config-copy'].disabled, false);
  assert.equal(elements['token-config-value'].textContent, 'Valid prompt');

  setup.softwareIdentity = null;
  fail = true;
  pending = context.loadIssuedServerPrompt(setup, 'codex', 'add_project');
  assert.equal(sent.software_identity, null);
  resolve(); await pending;
  assert.equal(elements['token-config-copy'].disabled, true);
  assert.equal(elements['token-secret-section'].hidden, false);
  assert.equal(elements['token-secret-copy'].hidden, false);
  assert.equal(elements['token-secret-value'].textContent, setup.projectCredentials[0].token);
  assert.match(elements['token-config-value'].textContent, /不要重复签发/);

  fail = false;
  pending = context.loadIssuedServerPrompt(setup, 'codex', 'add_project');
  resolve(); await pending;
  assert.equal(elements['token-config-copy'].disabled, false);
  assert.equal(elements['token-secret-section'].hidden, true);
  assert.equal(elements['token-secret-copy'].hidden, true);

  pending = context.loadIssuedServerPrompt(setup, 'codex', 'add_project');
  context.state.issuedPromptRequest += 1;
  elements['token-config-value'].textContent = 'New dialog';
  resolve(); await pending;
  assert.equal(elements['token-config-value'].textContent, 'New dialog');
})().catch(error => { console.error(error); process.exitCode = 1; });
