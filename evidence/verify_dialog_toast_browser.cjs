const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const os = require('node:os');

const WEB_DIR = path.join(__dirname, '../src/agentchatroom/web');
const CHROME_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

if (!fs.existsSync(CHROME_PATH)) {
  console.log('Chrome not found, skipping visual browser verification');
  process.exit(0);
}

const server = http.createServer((req, res) => {
  const urlPath = req.url.split('?')[0];
  if (urlPath.startsWith('/api/')) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, required: false, authenticated: true }));
    return;
  }
  let relPath = urlPath === '/' ? 'index.html' : urlPath.replace(/^\/assets\//, '');
  let filePath = path.join(WEB_DIR, relPath);
  if (!fs.existsSync(filePath)) {
    res.writeHead(404);
    res.end();
    return;
  }

  const ext = path.extname(filePath);
  const mimeTypes = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.svg': 'image/svg+xml',
  };
  res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'text/plain' });
  res.end(fs.readFileSync(filePath));
});

server.listen(0, '127.0.0.1', async () => {
  const port = server.address().port;
  const cdpPort = 9335;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'chrome-test-'));

  const chrome = spawn(CHROME_PATH, [
    '--headless=new',
    `--remote-debugging-port=${cdpPort}`,
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    `--user-data-dir=${tmpDir}`,
    `http://127.0.0.1:${port}/`,
  ]);

  const cleanup = () => {
    try { chrome.kill(); } catch (e) {}
    try { server.close(); } catch (e) {}
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) {}
  };

  try {
    // Wait for CDP endpoint
    let wsUrl = null;
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 200));
      try {
        const resp = await fetch(`http://127.0.0.1:${cdpPort}/json`);
        const list = await resp.json();
        const pageTab = list.find((t) => t.type === 'page');
        if (pageTab && pageTab.webSocketDebuggerUrl) {
          wsUrl = pageTab.webSocketDebuggerUrl;
          break;
        }
      } catch (e) {}
    }

    if (!wsUrl) throw new Error('Failed to connect to Chrome DevTools port');

    const ws = new WebSocket(wsUrl);
    await new Promise((resolve) => ws.addEventListener('open', resolve));

    let msgId = 1;
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = msgId++;
      const handler = (event) => {
        const data = JSON.parse(event.data);
        if (data.id === id) {
          ws.removeEventListener('message', handler);
          if (data.error) reject(new Error(JSON.stringify(data.error)));
          else resolve(data.result);
        }
      };
      ws.addEventListener('message', handler);
      ws.send(JSON.stringify({ id, method, params }));
    });

    await send('Runtime.enable');
    await send('Page.enable');
    await send('DOM.enable');

    // Poll until document.getElementById("token-secret-dialog") is available
    let pageReady = false;
    for (let i = 0; i < 25; i++) {
      await new Promise((r) => setTimeout(r, 200));
      const check = await send('Runtime.evaluate', {
        expression: 'Boolean(document.getElementById("token-secret-dialog") && document.getElementById("token-config-copy"))',
        returnByValue: true
      });
      if (check.result?.value) {
        pageReady = true;
        break;
      }
    }
    if (!pageReady) throw new Error('Page elements not loaded in time');

    // Clear initial page load toasts and open token-secret-dialog
    const evalRaw = await send('Runtime.evaluate', {
      expression: `(() => {
        document.getElementById("toast-region").innerHTML = "";
        const dialog = document.getElementById("token-secret-dialog");
        document.getElementById("token-config-section").hidden = false;
        document.getElementById("token-config-value").textContent = "SAMPLE PROMPT CONTENT FOR AGENT CONNECTION";
        dialog.showModal();
        return {
          dialogOpen: dialog.open,
          dialogId: dialog.id
        };
      })()`,
      returnByValue: true
    });
    const evalResult = evalRaw.result?.value || evalRaw;
    console.log('Dialog opened:', evalResult);

    // Mock clipboard.writeText and simulate clicking "复制完整提示词"
    const copyRaw = await send('Runtime.evaluate', {
      expression: `(() => {
        if (!navigator.clipboard) navigator.clipboard = {};
        navigator.clipboard.writeText = () => Promise.resolve();
        const button = document.getElementById("token-config-copy");
        button.click();
        return { clicked: true };
      })()`,
      returnByValue: true
    });
    const copyResult = copyRaw.result?.value || copyRaw;
    console.log('Copy clicked:', copyResult);

    // Wait for toast DOM rendering and animation
    await new Promise((r) => setTimeout(r, 400));

    // Inspect toast element inside dialog
    const toastRaw = await send('Runtime.evaluate', {
      expression: `(() => {
        const dialog = document.getElementById("token-secret-dialog");
        const dialogToastRegion = dialog.querySelector(":scope > .toast-region");
        const toast = dialogToastRegion ? dialogToastRegion.querySelector(".toast") : null;
        const bodyRegion = document.getElementById("toast-region");
        const bodyToasts = bodyRegion ? bodyRegion.querySelectorAll(".toast").length : 0;
        
        let rect = null;
        let dialogRect = null;
        if (toast) {
          const r = toast.getBoundingClientRect();
          rect = { x: r.x, y: r.y, width: r.width, height: r.height };
        }
        if (dialog) {
          const dr = dialog.getBoundingClientRect();
          dialogRect = { x: dr.x, y: dr.y, width: dr.width, height: dr.height };
        }

        return {
          hasDialogToastRegion: !!dialogToastRegion,
          toastCountInDialog: dialogToastRegion ? dialogToastRegion.children.length : 0,
          toastText: toast ? toast.textContent : null,
          toastClass: toast ? toast.className : null,
          toastRect: rect,
          dialogRect: dialogRect,
          bodyToastCount: bodyToasts,
          copyButtonText: document.getElementById("token-config-copy").textContent
        };
      })()`,
      returnByValue: true
    });
    const toastCheck = toastRaw.result?.value || toastRaw;
    console.log('Toast verification result:', JSON.stringify(toastCheck, null, 2));

    // Capture screenshot of the dialog with the toast
    const screenshot = await send('Page.captureScreenshot', { format: 'png' });
    const artifactsDir = path.join(__dirname, '../tests');
    const screenshotPath = path.join(artifactsDir, 'dialog_toast_verification.png');
    fs.writeFileSync(screenshotPath, Buffer.from(screenshot.data, 'base64'));
    console.log('Screenshot saved to:', screenshotPath);

    // Validate assertions
    if (!toastCheck.hasDialogToastRegion) throw new Error('Dialog toast region was not created!');
    if (toastCheck.toastCountInDialog !== 1) throw new Error('Expected 1 toast in dialog toast region!');
    if (toastCheck.toastText !== '已复制到剪贴板') throw new Error(`Unexpected toast text: ${toastCheck.toastText}`);
    if (toastCheck.bodyToastCount !== 0) throw new Error('Body toast count should be 0 when dialog is active!');
    if (!toastCheck.copyButtonText.includes('已复制')) throw new Error('Copy button did not show inline feedback!');

    console.log('Visual and CDP verification PASSED 100%!');
    cleanup();
    process.exit(0);
  } catch (err) {
    console.error('CDP verification failed:', err);
    cleanup();
    process.exit(1);
  }
});
