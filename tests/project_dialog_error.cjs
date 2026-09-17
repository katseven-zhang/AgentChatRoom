const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/index.html'), 'utf8');

function extract(name) {
  for (const prefix of ['async function', 'function']) {
    const start = source.indexOf(`${prefix} ${name}(`);
    if (start >= 0) {
      const end = source.indexOf('\n}', start);
      return source.slice(start, end + 2);
    }
  }
  assert.fail(`app.js must declare ${name}()`);
}

// #163: the project dialog's inline error banner must exist with alert
// semantics and start hidden, mirroring the #142 token-dialog banner.
const bannerMatch = html.match(/<p class="form-error" id="project-form-error" role="alert" hidden><\/p>/);
assert.ok(bannerMatch, 'index.html must contain the project-form-error alert banner');

function buildContext({apiBehavior}) {
  const calls = {close: 0, reset: 0, toast: 0, handleError: 0, loadProjects: []};
  const elements = {
    'project-name-input': {value: '  示例项目 '},
    'project-path-input': {value: 'D:/definitely/missing/path'},
    'project-dialog': {close: () => { calls.close += 1; }},
    'project-form': {reset: () => { calls.reset += 1; }},
    'project-form-error': {textContent: '', hidden: true},
    'token-form-error': {textContent: '', hidden: true},
  };
  const context = {
    elements,
    state: {projectId: null, projects: []},
    withBusy: (work) => Promise.resolve().then(work),
    api: (url, options) => apiBehavior(url, options),
    loadProjects: (projectId) => { calls.loadProjects.push(projectId); return Promise.resolve(); },
    showToast: () => { calls.toast += 1; },
    handleError: () => { calls.handleError += 1; },
    calls,
  };
  vm.createContext(context);
  vm.runInContext(
    extract('setDialogFormError') + '\n'
    + extract('setTokenFormError') + '\n'
    + extract('setProjectFormError') + '\n'
    + extract('submitProjectForm'),
    context,
  );
  return context;
}

(async () => {
  // Failure: backend 4xx must render inside the dialog, keep it open, and
  // preserve the user's inputs; the page-level toast path must not be used.
  const failure = buildContext({
    apiBehavior: () => {
      const error = new Error('Project root must be an existing directory');
      error.code = 'project_path_not_found';
      error.status = 409;
      return Promise.reject(error);
    },
  });
  await vm.runInContext('submitProjectForm()', failure);
  const failureBanner = failure.elements['project-form-error'];
  assert.equal(failureBanner.hidden, false, 'failure banner must be visible');
  assert.equal(failureBanner.textContent, 'Project root must be an existing directory');
  assert.equal(failure.calls.close, 0, 'dialog must stay open after failure');
  assert.equal(failure.calls.reset, 0, 'form inputs must be preserved after failure');
  assert.equal(failure.calls.toast, 0, 'page toast must not be the failure surface');
  assert.equal(failure.calls.handleError, 0, 'handleError must not be used by the dialog form');
  assert.equal(failure.elements['project-path-input'].value, 'D:/definitely/missing/path');

  // Success: dialog closes, form resets, project list reloads to the new id.
  const success = buildContext({
    apiBehavior: (url, options) => {
      assert.equal(url, '/api/v1/projects');
      assert.equal(options.method, 'POST');
      const body = JSON.parse(options.body);
      assert.equal(body.root_path, 'D:/definitely/missing/path');
      return Promise.resolve({id: 'p_new'});
    },
  });
  await vm.runInContext('submitProjectForm()', success);
  assert.equal(success.calls.close, 1);
  assert.equal(success.calls.reset, 1);
  assert.deepEqual(success.calls.loadProjects, ['p_new']);
  assert.equal(success.calls.toast, 1);
  assert.equal(success.elements['project-form-error'].hidden, true);

  // #163 + #142 share one mechanism: clearing hides both banners.
  const shared = buildContext({apiBehavior: () => Promise.resolve({id: 'x'})});
  vm.runInContext('setProjectFormError("项目已存在"); setTokenFormError("成员身份不完整");', shared);
  assert.equal(shared.elements['project-form-error'].hidden, false);
  assert.equal(shared.elements['token-form-error'].hidden, false);
  vm.runInContext('setProjectFormError(""); setTokenFormError("");', shared);
  assert.equal(shared.elements['project-form-error'].hidden, true);
  assert.equal(shared.elements['project-form-error'].textContent, '');
  assert.equal(shared.elements['token-form-error'].hidden, true);

  console.log('project dialog inline error banner regression passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
