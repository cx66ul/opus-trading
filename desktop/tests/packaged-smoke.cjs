'use strict';
const { _electron } = require(process.env.HAWK_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { spawn } = require('node:child_process');
const { httpProbe } = require('../backend');
const executablePath = process.env.HAWK_TEST_EXE;
const out = path.resolve(__dirname, '../../docs/desktop-repair-20261011');
fs.mkdirSync(out, { recursive: true });
const env = { ...process.env, HAWK_PORT: '8877' };
delete env.ELECTRON_RUN_AS_NODE;
delete env.HAWK_ROOT;
delete env.HAWK_PYTHON;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-smoke-'));
(async () => {
  const start = Date.now();
  const electron = await _electron.launch({ executablePath,
    args: ['--user-data-dir=' + profile], env, timeout: 30000 });
  const errors = [];
  try {
    const page = await electron.firstWindow();
    page.on('pageerror', e => errors.push(e.message));
    const firstWindowMs = Date.now() - start;
    await page.waitForURL('http://127.0.0.1:8877/', { timeout: 45000 });
    await page.locator('h1').waitFor();
    const mainPageMs = Date.now() - start;
    await page.waitForFunction(() => document.querySelector('#ov-kpi')?.children.length > 0, { timeout: 30000 });
    const dataReadyMs = Date.now() - start;
    const info = await page.evaluate(() => window.hawk.appInfo());
    const backend = await page.evaluate(() => window.hawk.backendStatus());
    assert.equal(backend.state, 'running');
    assert.equal(info.version, '0.2.1');
    assert.match(info.python, /resources[\\/]runtime[\\/]python.exe$/);
    await page.screenshot({ path: path.join(out, 'main-window.png') });
    const text = await page.locator('body').innerText();
    assert.ok(text.includes('OPUS'));
    const overview = await page.evaluate(async () => {
      const r = await fetch('/api/overview');
      return { status: r.status, data: await r.text() };
    });
    assert.equal(overview.status, 200, JSON.stringify(overview.data));
    const pid = backend.pid;
    await page.reload();
    await page.waitForLoadState('domcontentloaded');
    assert.equal((await page.evaluate(() => window.hawk.backendStatus())).pid, pid);
    await electron.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
    assert.equal(await electron.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()), false);
    const secondStart = Date.now();
    const second = spawn(executablePath, ['--user-data-dir=' + profile], { env, windowsHide: true, stdio: 'ignore' });
    await Promise.race([
      new Promise(resolve => second.once('exit', resolve)),
      new Promise((_, reject) => setTimeout(() => reject(new Error('second instance did not exit')), 10000)),
    ]);
    assert.equal(await electron.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()), true);
    const reopenMs = Date.now() - secondStart;
    assert.equal((await page.evaluate(() => window.hawk.backendStatus())).pid, pid);
    await page.evaluate(() => { window.hawk.restartBackend().catch(() => {}); });
    await page.waitForURL(/shell\.html$/, { timeout: 15000 });
    await page.waitForURL('http://127.0.0.1:8877/', { timeout: 15000 });
    const restarted = await page.evaluate(() => window.hawk.backendStatus());
    assert.equal(restarted.state, 'running');
    assert.notEqual(restarted.pid, pid);
    assert.deepEqual(errors, []);
    const log = fs.readFileSync(path.join(profile, 'logs/startup.log'), 'utf8');
    fs.writeFileSync(path.join(out, 'startup-test.log'), log);
    fs.writeFileSync(path.join(out, 'smoke-results.json'), JSON.stringify({
      firstWindowMs, mainPageMs, dataReadyMs, reopenMs, info, initialPid: pid, restartedPid: restarted.pid,
      overviewStatus: overview.status, text: text.slice(0, 2500), rendererErrors: errors,
      reloadPreservedPid: true, trayReopenPreservedPid: true,
    }, null, 2));
    console.log(JSON.stringify({ firstWindowMs, mainPageMs, dataReadyMs, reopenMs, overviewStatus: overview.status, errors }));
  } catch (e) {
    try {
      const page = await electron.firstWindow();
      console.error('BACKEND DIAGNOSTIC', await page.evaluate(() => window.hawk.backendStatus()));
    } catch {}
    throw e;
  } finally { await electron.close(); }
  assert.equal(await httpProbe('http://127.0.0.1:8877/api/ready', 500), false, 'backend orphan after exit');
  console.log('Packaged Electron smoke passed; backend port released on exit.');
})().catch(e => { console.error(e); process.exitCode = 1; });
