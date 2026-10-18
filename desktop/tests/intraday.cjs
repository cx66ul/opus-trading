'use strict';
const { _electron } = require(process.env.HAWK_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const out = path.resolve(__dirname, '../../docs/intraday-implementation-20261011');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-intraday-'));
const env = { ...process.env, HAWK_PORT: '8870', OPUS_MAIL_DISABLE: '1', OPUS_NEWS_AUTOCOLLECT: '0', OPUS_INTRADAY_DISABLE: '1' };
delete env.ELECTRON_RUN_AS_NODE;
(async () => {
  const start = Date.now();
  const app = await _electron.launch({ executablePath: process.env.HAWK_TEST_EXE,
    args: ['--user-data-dir=' + profile], env, timeout: 30000 });
  const errors = [];
  try {
    const page = await app.firstWindow();
    page.on('pageerror', e => errors.push(e.message));
    const windowMs = Date.now() - start;
    await page.waitForURL('http://127.0.0.1:8870/', { timeout: 30000 });
    const pageMs = Date.now() - start;
    const backend = await page.evaluate(() => window.hawk.backendStatus());
    assert.equal(backend.pid, null, 'must reuse running backend, never create duplicate workers');
    await page.locator('nav button[data-p="stock"]').click();
    await page.locator('#s-q').fill('600152');
    for (const period of ['line','m1','m5','m30','m60']) {
      await page.locator('#s-period').selectOption(period);
      await page.locator('#i-chart svg').waitFor();
      assert.ok((await page.locator('#i-state').innerText()).includes('跟踪采集运行中'));
      assert.equal(await page.locator('#s-err .err').count(), 0);
    }
    await page.locator('#s-period').selectOption('line');
    await page.locator('#i-chart svg').waitFor();
    const activeCount = await page.locator('#i-chart svg path').count();
    await page.locator('#i-detailed').check();
    await page.locator('#i-invalidated').check();
    const allCount = await page.locator('#i-chart svg path').count();
    assert.ok(allCount > activeCount, 'invalidated markers must be separately selectable');
    await page.locator('#i-invalidated').uncheck();
    await page.screenshot({ path: path.join(out, 'desktop-minute-chart.png') });
    assert.deepEqual(errors, []);
    const result = { windowMs, pageMs, backendReused: true, activeCount, allCount, rendererErrors: errors };
    fs.writeFileSync(path.join(out, 'desktop-acceptance.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally { await app.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
