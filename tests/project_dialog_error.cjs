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

function buildContext({apiBehavior, dialogOpen = true}) {
  const calls = {close: 0, reset: 0, toast: [], handleError: 0, loadProjects: []};
  const submitButton = {disabled: false};
  const elements = {
    'project-name-input': {value: '  示例项目 '},
    'project-path-input': {value: 'D:/definitely/missing/path'},
    // #321: submitProjectForm reads dialog.open to decide whether an inline
    // banner can be seen at all, and project-form.querySelector to disable the
    // submit button while the request is in flight. Both are stubbed here so
    // this harness keeps exercising the real function rather than a copy.
    'project-dialog': {open: dialogOpen, close: () => { calls.close += 1; }},
    'project-form': {
      reset: () => { calls.reset += 1; },
      querySelector: (selector) => (selector === '[type=submit]' ? submitButton : null),
    },
    'project-form-error': {textContent: '', hidden: true},
    'token-form-error': {textContent: '', hidden: true},
  };
  const context = {
    elements,
    state: {projectId: null, projects: []},
    withBusy: (work) => Promise.resolve().then(work),
    api: (url, options) => apiBehavior(url, options),
    loadProjects: (projectId) => { calls.loadProjects.push(projectId); return Promise.resolve(); },
    showToast: (message, tone) => { calls.toast.push({message, tone}); },
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
  assert.deepEqual(failure.calls.toast, [], 'page toast must not be the failure surface');
  assert.equal(failure.calls.handleError, 0, 'handleError must not be used by the dialog form');
  assert.equal(failure.elements['project-path-input'].value, 'D:/definitely/missing/path');
  // The submit button must be usable again, or the dialog becomes a dead end
  // after one failure.
  const failedSubmit = failure.elements['project-form'].querySelector('[type=submit]');
  assert.equal(failedSubmit.disabled, false, 'submit must be re-enabled after failure');

  // #321: once the dialog is closed the inline banner has no reader, so the
  // failure has to go to the page-level toast instead of being written into an
  // element nobody can see.
  const closed = buildContext({
    dialogOpen: false,
    apiBehavior: () => Promise.reject(new Error('boom after close')),
  });
  await vm.runInContext('submitProjectForm()', closed);
  assert.equal(closed.elements['project-form-error'].hidden, true,
    'a closed dialog must not be the failure surface');
  assert.equal(closed.calls.toast.length, 1, 'a closed dialog must surface the failure');
  assert.equal(closed.calls.toast[0].message, 'boom after close');
  assert.equal(closed.calls.toast[0].tone, 'error');

  // Success: dialog closes, form resets, project list reloads to the new id.
  const success = buildContext({
    apiBehavior: (url, options) => {
      assert.equal(url, '/api/v1/projects');
      assert.equal(options.method, 'POST');
      const body = JSON.parse(options.body);
      assert.equal(body.root_path, 'D:/definitely/missing/path');
      // The name is trimmed before it is sent.
      assert.equal(body.name, '示例项目');
      return Promise.resolve({id: 'p_new'});
    },
  });
  await vm.runInContext('submitProjectForm()', success);
  assert.equal(success.calls.close, 1);
  assert.equal(success.calls.reset, 1);
  assert.deepEqual(success.calls.loadProjects, ['p_new']);
  assert.equal(success.calls.toast.length, 1);
  assert.equal(success.elements['project-form-error'].hidden, true);

  // Selecting an already-known project must not steal the selection back from
  // whatever the user was looking at.
  const known = buildContext({
    apiBehavior: () => Promise.resolve({id: 'p_known'}),
  });
  known.state.projectId = 'p_current';
  known.state.projects = [{id: 'p_known'}];
  await vm.runInContext('submitProjectForm()', known);
  assert.deepEqual(known.calls.loadProjects, ['p_current']);

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
