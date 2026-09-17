const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appJs = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/app.js'), 'utf8');
const appCss = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/app.css'), 'utf8');

// #167 CSS verification: dialog toast region and animation must be declared in app.css
assert.ok(appCss.includes('dialog > .toast-region'), 'app.css must style dialog > .toast-region');
assert.ok(appCss.includes('.dialog-toast-region'), 'app.css must declare .dialog-toast-region');
assert.ok(appCss.includes('@keyframes dialog-toast-in'), 'app.css must declare dialog-toast-in animation');

function extractFunction(name) {
  for (const prefix of ['async function', 'function']) {
    const start = appJs.indexOf(`${prefix} ${name}(`);
    if (start >= 0) {
      const end = appJs.indexOf('\n}', start);
      return appJs.slice(start, end + 2);
    }
  }
  assert.fail(`app.js must declare ${name}()`);
}

// Build a mock DOM node
function createMockElement(tagName, id = '', className = '') {
  const children = [];
  const attributes = {};
  return {
    tagName: tagName.toUpperCase(),
    id,
    className,
    open: false,
    isConnected: true,
    children,
    get firstChild() { return children[0] || null; },
    appendChild(child) {
      if (child.parentElement) {
        child.parentElement.removeChild(child);
      }
      child.parentElement = this;
      children.push(child);
      return child;
    },
    append(...nodes) {
      for (const node of nodes) this.appendChild(node);
    },
    removeChild(child) {
      const idx = children.indexOf(child);
      if (idx >= 0) {
        children.splice(idx, 1);
        child.parentElement = null;
      }
      return child;
    },
    remove() {
      if (this.parentElement) {
        this.parentElement.removeChild(this);
      }
      this.isConnected = false;
    },
    setAttribute(key, val) { attributes[key] = String(val); },
    getAttribute(key) { return attributes[key] ?? null; },
    querySelector(sel) {
      if (sel === ':scope > .toast-region') {
        return children.find(c => c.className && c.className.includes('toast-region')) || null;
      }
      return null;
    }
  };
}

const baseToastRegion = createMockElement('div', 'toast-region', 'toast-region');
const dialogSecret = createMockElement('dialog', 'token-secret-dialog');
const dialogIntegration = createMockElement('dialog', 'integration-dialog');
const allDialogs = [dialogSecret, dialogIntegration];

const context = {
  elements: { 'toast-region': baseToastRegion },
  document: {
    createElement: (tag) => createMockElement(tag),
    getElementById: (id) => (id === 'toast-region' ? baseToastRegion : null),
    querySelectorAll: (sel) => {
      if (sel === 'dialog[open]') {
        return allDialogs.filter(d => d.open);
      }
      if (sel === 'dialog') return allDialogs;
      return [];
    }
  },
  setTimeout: (fn, ms) => { /* no-op in sync unit test */ },
};

vm.createContext(context);
vm.runInContext(
  extractFunction('getActiveModalDialog') + '\n'
  + extractFunction('getToastRegion') + '\n'
  + extractFunction('adoptDialogToasts') + '\n'
  + extractFunction('showToast'),
  context,
);

// 1. When no dialog is open, toasts must go to base toast region
dialogSecret.open = false;
dialogIntegration.open = false;
vm.runInContext('showToast("主页面提示", "success")', context);
assert.equal(baseToastRegion.children.length, 1, 'Base toast region must receive toast when no dialog open');
assert.equal(baseToastRegion.children[0].textContent, '主页面提示');
assert.equal(baseToastRegion.children[0].className, 'toast success');

// 2. When token-secret-dialog is open, toast must be routed inside that dialog (Top Layer)
dialogSecret.open = true;
vm.runInContext('showToast("已复制到剪贴板", "success")', context);
const secretRegion = dialogSecret.querySelector(':scope > .toast-region');
assert.ok(secretRegion, 'Dialog must have an internal toast-region created');
assert.equal(secretRegion.children.length, 1, 'Dialog toast-region must receive the toast');
assert.equal(secretRegion.children[0].textContent, '已复制到剪贴板');
assert.equal(baseToastRegion.children.length, 1, 'Base toast region must NOT receive modal dialog toast');

// 3. When integration-dialog is open instead, toast goes into integration-dialog
dialogSecret.open = false;
dialogIntegration.open = true;
vm.runInContext('showToast("已复制接入指令", "success")', context);
const intRegion = dialogIntegration.querySelector(':scope > .toast-region');
assert.ok(intRegion, 'Integration dialog must receive internal toast-region');
assert.equal(intRegion.children.length, 1);
assert.equal(intRegion.children[0].textContent, '已复制接入指令');

// 4. When dialog closes with active toasts, adoptDialogToasts transfers them to base region
assert.equal(baseToastRegion.children.length, 1);
vm.runInContext('adoptDialogToasts(document.querySelectorAll("dialog")[1])', context);
assert.equal(intRegion.children.length, 0, 'Dialog toast region should be emptied after adoption');
assert.equal(baseToastRegion.children.length, 2, 'Base toast region should adopt the unexpired toast');
assert.equal(baseToastRegion.children[1].textContent, '已复制接入指令');

console.log('dialog toast top-layer regression test passed successfully');
