'use strict';
const { _electron } = require(process.env.HAWK_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const out = path.resolve(__dirname, '../../docs/intraday-distillation-20261011');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'opus-intraday-images-'));
const env = { ...process.env, HAWK_PORT:'8870', OPUS_MAIL_DISABLE:'1',OPUS_NEWS_AUTOCOLLECT:'0',OPUS_INTRADAY_DISABLE:'1' };
delete env.ELECTRON_RUN_AS_NODE;
(async()=>{
  const app=await _electron.launch({executablePath:process.env.HAWK_TEST_EXE,args:['--user-data-dir='+profile],env,timeout:30000});
  const errors=[];
  try {
    const page=await app.firstWindow();page.on('pageerror',e=>errors.push(e.message));
    await page.waitForURL('http://127.0.0.1:8870/',{timeout:30000});
    assert.equal((await page.evaluate(()=>window.hawk.backendStatus())).pid,null);
    await page.locator('nav button[data-p="stock"]').click();
    await page.locator('#s-q').fill('600152');
    await page.locator('#s-period').selectOption('m5');
    await page.locator('#i-policy').waitFor();
    const note=await page.locator('#i-policy').innerText();
    assert.ok(note.includes('未证明提升') && note.includes('胜率不可估'));
    assert.equal(await page.locator('#s-err .err').count(),0);
    await page.locator('#s-body > .card').first().screenshot({path:path.join(out,'desktop-fused-chart.png')});
    const data=await page.evaluate(async()=> (await fetch('/api/intraday/600152?period=m5')).json());
    assert.ok(data.signals.some(s=>s.rule_id==='T03'||s.rule_id==='T06'||s.rule_id==='T07'));
    assert.ok(data.signals.filter(s=>s.kind.includes('buy')).every(s=>s.status==='candidate' && s.gate_reasons.length));
    assert.equal(data.image_policy.backtest.state,'no_improvement_demonstrated');
    for(const p of ['line','m1','m30','m60','daily']) {
      await page.locator('#s-period').selectOption(p);
      await page.locator(p==='daily'?'#kchart':'#i-chart svg').waitFor();
      assert.equal(await page.locator('#s-err .err').count(),0);
    }
    const mail=await page.evaluate(async()=>(await fetch('/api/mail/status')).json());
    const news=await page.evaluate(async()=>(await fetch('/api/news/sources')).json());
    assert.ok(mail.enabled && mail.running && news.running);
    assert.deepEqual(errors,[]);
    const report={policy:data.image_policy,observations:data.signals.length,rendererErrors:errors,
      allSixViews:true,unknownEnvironmentNeverConfirmsBuy:true,
      intradayRunning:data.running,mailRunning:mail.running,newsRunning:news.running};
    fs.writeFileSync(path.join(out,'desktop-acceptance.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify({observations:data.signals.length,rendererErrors:errors,allSixViews:true,
      mailRunning:mail.running,newsRunning:news.running}));
  } finally {await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
