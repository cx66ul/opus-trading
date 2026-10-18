'use strict';
const { _electron } = require(process.env.HAWK_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const out = path.resolve(__dirname, '../../docs/jin10-integration-20261011');
fs.mkdirSync(out, { recursive: true });
const env = { ...process.env, HAWK_PORT: '8877' };
delete env.ELECTRON_RUN_AS_NODE;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-jin10-'));
(async () => {
  const start = Date.now();
  const app = await _electron.launch({ executablePath: process.env.HAWK_TEST_EXE,
    args: ['--user-data-dir=' + profile], env, timeout: 30000 });
  const errors = [];
  try {
    const page = await app.firstWindow();
    page.on('pageerror', e => errors.push(e.message));
    const windowMs = Date.now() - start;
    await page.waitForURL('http://127.0.0.1:8877/', { timeout: 45000 });
    const pageMs = Date.now() - start;
    await page.locator('nav button[data-p="health"]').click();
    await page.waitForFunction(() => document.querySelector('#h-news')?.innerText.includes('jin10'), { timeout: 30000 });
    const readStatus = () => page.evaluate(async () => (await fetch('/api/news/sources')).json());
    let first;
    for (let i = 0; i < 40; i++) {
      first = await readStatus();
      if (first.sources.some(s => s.source === 'jin10' && Date.parse(s.attempted_at) >= start - 1000)) break;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    console.log('FIRST', JSON.stringify(first));
    assert.ok(first.sources.some(s => s.source === 'jin10' && Date.parse(s.attempted_at) >= start - 1000));
    assert.equal(first.running, true);
    assert.equal(first.sources.find(s => s.source === 'jin10').state, 'ok');
    assert.ok(first.sources.find(s => s.source === 'jin10').raw_total > 0);
    await page.screenshot({ path: path.join(out, 'sources.png'), fullPage: true });
    await page.locator('nav button[data-p="watch"]').click();
    await page.waitForFunction(() => document.querySelector('#w-news')?.innerText.includes('重点跟踪'), { timeout: 15000 });
    const tracked = await page.evaluate(async () => (await fetch('/api/news/tracked')).json());
    assert.ok(tracked.stocks.length >= 3);
    assert.ok(tracked.stocks.every(s => s.held || s.watched));
    await page.screenshot({ path: path.join(out, 'tracked-news.png'), fullPage: true });
    const previous = first.sources.find(s => s.source === 'jin10').attempted_at;
    let second;
    for (let i = 0; i < 95; i++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      second = await readStatus();
      if (second.sources.some(s => s.source === 'jin10' && s.attempted_at > previous)) break;
    }
    console.log('SECOND', JSON.stringify(second));
    assert.ok(second.sources.some(s => s.source === 'jin10' && s.attempted_at > previous));
    assert.equal(second.sources.find(s => s.source === 'jin10').state, 'ok');
    assert.ok(second.sources.find(s => s.source === 'jin10').duplicates > 0);
    assert.deepEqual(errors, []);
    const report = { windowMs, pageMs, first, second, tracked, rendererErrors: errors };
    fs.writeFileSync(path.join(out, 'desktop-live.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally { await app.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
