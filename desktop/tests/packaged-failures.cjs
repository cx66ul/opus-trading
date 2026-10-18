'use strict';
const { _electron } = require(process.env.HAWK_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const executablePath = process.env.HAWK_TEST_EXE;
const out = path.resolve(__dirname, '../../docs/desktop-repair-20261011');
const baseEnv = { ...process.env };
delete baseEnv.ELECTRON_RUN_AS_NODE;
delete baseEnv.HAWK_ROOT;
delete baseEnv.HAWK_PYTHON;
async function launchCase(name, options, expected) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-' + name + '-'));
  const electron = await _electron.launch({
    executablePath: options.exe || executablePath,
    args: [...(options.args || []), '--user-data-dir=' + profile],
    env: { ...baseEnv, HAWK_PORT: String(options.port || 8878), ...options.env }, timeout: 15000,
  });
  try {
    const page = await electron.firstWindow();
    await page.getByText(expected, { exact: false }).first().waitFor({ timeout: 15000 });
    assert.equal(await electron.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()), true);
    await page.screenshot({ path: path.join(out, name + '.png') });
    console.log(name + ': visible diagnostic passed');
  } finally { await electron.close(); }
}
(async () => {
  await launchCase('missing-python', { env: { HAWK_PYTHON: path.join(os.tmpdir(), 'opus-no-python.exe') } }, '找不到 Python');
  const server = http.createServer((_req, res) => { res.writeHead(404); res.end('not Opus'); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try { await launchCase('occupied-port', { port: server.address().port }, '端口'); }
  finally { await new Promise(resolve => server.close(resolve)); }
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-missing-shell-'));
  for (const name of ['main.js', 'backend.js', 'wizard.js', 'updater.js', 'preload.js', 'package.json']) {
    fs.copyFileSync(path.resolve(__dirname, '..', name), path.join(fixture, name));
  }
  fs.cpSync(path.resolve(__dirname, '../assets'), path.join(fixture, 'assets'), { recursive: true });
  try {
    await launchCase('missing-shell', {
      exe: process.env.HAWK_TEST_ELECTRON, args: [fixture],
      env: { HAWK_ROOT: path.resolve(__dirname, '../..'),
        HAWK_PYTHON: path.resolve(__dirname, '../../runtime-standalone/python.exe') },
    }, 'Opus 辅助交易未能加载界面');
  } finally { fs.rmSync(fixture, { recursive: true, force: true }); }
})().catch(e => { console.error(e); process.exitCode = 1; });
