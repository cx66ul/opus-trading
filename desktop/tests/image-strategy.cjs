'use strict';
const { _electron } = require(process.env.HAWK_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const output = path.resolve(__dirname, '../../docs/image-strategy-20261011');
fs.mkdirSync(output, { recursive: true });
const env = { ...process.env, HAWK_PORT: '8877' };
delete env.ELECTRON_RUN_AS_NODE;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-image-strategy-'));
(async () => {
  const electron = await _electron.launch({ executablePath: process.env.HAWK_TEST_EXE,
    args: ['--user-data-dir=' + profile], env, timeout: 30000 });
  const errors = [];
  try {
    const page = await electron.firstWindow();
    page.on('pageerror', e => errors.push(e.message));
    await page.waitForURL('http://127.0.0.1:8877/', { timeout: 45000 });
    await page.locator('#ov-kpi .kpi').first().waitFor({ timeout: 30000 });
    await page.locator('nav button[data-p="recommend"]').click();
    await page.locator('#image-evidence').waitFor({ timeout: 60000 });
    const response = await page.evaluate(async () => {
      const r = await fetch('/api/recommend');
      return { status: r.status, data: await r.json() };
    });
    assert.equal(response.status, 200);
    assert.equal(response.data.image_policy.weight, .6);
    assert.ok(response.data.rows.length > 0);
    assert.ok(response.data.rows.every(r => r.image_strategy.enabled));
    assert.ok(await page.locator('#rc-body').innerText().then(t => t.includes('60%')));
    await page.screenshot({ path: path.join(output, 'daily-ranking.png'), fullPage: true });
    await page.locator('#image-evidence summary').click();
    await page.waitForFunction(() => document.querySelector('#image-evidence')?.dataset.loaded === '1');
    assert.ok((await page.locator('#image-evidence-body').innerText()).includes('锡华科技'));
    await page.screenshot({ path: path.join(output, 'source-evidence.png'), fullPage: true });
    const info = await page.evaluate(() => window.hawk.appInfo());
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'desktop-verification.json'), JSON.stringify({
      status: response.status, info, rows: response.data.rows.length,
      policy: response.data.image_policy, rendererErrors: errors, evidencePanelLoaded: true,
    }, null, 2));
    console.log(JSON.stringify({ status: response.status, rows: response.data.rows.length,
      weight: response.data.image_policy.weight, rendererErrors: errors }));
  } catch (e) {
    const page = await electron.firstWindow();
    console.error('UI', (await page.locator('body').innerText()).slice(-8000));
    console.error('APP', await page.evaluate(() => window.hawk.appInfo()));
    console.error('BACKEND', await page.evaluate(() => window.hawk.backendStatus()));
    console.error('ERRORS', errors);
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true });
    throw e;
  } finally { await electron.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
