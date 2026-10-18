'use strict';
const {_electron}=require(process.env.HAWK_PLAYWRIGHT||'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const out=path.resolve(__dirname,'../../docs/intraday-marker-fix-20261012');
fs.mkdirSync(out,{recursive:true});
const env={...process.env,HAWK_PORT:'8870',OPUS_MAIL_DISABLE:'1',OPUS_NEWS_AUTOCOLLECT:'0',OPUS_INTRADAY_DISABLE:'1'};
delete env.ELECTRON_RUN_AS_NODE;
(async()=>{
  const app=await _electron.launch({executablePath:process.env.HAWK_TEST_EXE,
    args:['--user-data-dir='+fs.mkdtempSync(path.join(os.tmpdir(),'opus-marker-density-'))],env});
  const errors=[],results=[];
  try {
    const page=await app.firstWindow();page.on('pageerror',e=>errors.push(e.message));
    await page.waitForURL('http://127.0.0.1:8870/',{timeout:30000});
    assert.equal((await page.evaluate(()=>window.hawk.backendStatus())).pid,null);
    await page.locator('nav button[data-p="stock"]').click();
    for (const code of ['600152','603232','603248']) {
      await page.locator('#s-q').fill(code);
      await page.locator('#s-period').selectOption('line');
      await page.locator('#s-go').click();
      await page.locator('#i-marker-count').waitFor();
      const data=await page.evaluate(async code=>(await fetch('/api/intraday/'+code)).json(),code);
      const signals=data.signals.filter(s=>s.available_at.slice(0,10)===data.trade_date);
      const detailRows=await page.locator('#s-body table tbody tr').count();
      const compact=await page.locator('[data-marker]').count();
      const oldVisible=signals.filter(s=>s.active).length;
      assert.ok(compact<oldVisible);
      assert.equal(detailRows,signals.length);
      assert.equal(await page.locator('#i-invalidated').isDisabled(),true);
      if(code==='600152') await page.locator('#s-body > .card').first().screenshot({path:path.join(out,'compact-minute-chart.png')});
      await page.locator('#i-detailed').check();
      const detailed=await page.locator('[data-marker]').count();
      assert.ok(detailed>compact);
      await page.locator('#i-invalidated').check();
      const all=await page.locator('[data-marker]').count();
      assert.ok(all>=detailed);
      assert.equal(await page.locator('#s-body table tbody tr').count(),detailRows);
      await page.locator('#i-detailed').uncheck();
      assert.equal(await page.locator('[data-marker]').count(),compact);
      // Automatic redraw must preserve the user's selected mode.
      await page.evaluate(()=>queryStock(true));
      assert.equal(await page.locator('[data-marker]').count(),compact);
      results.push({code,trade_date:data.trade_date,records:signals.length,oldVisible,compact,detailed,all,detailRows});
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'desktop-acceptance.json'),JSON.stringify({results,errors},null,2));
    console.log(JSON.stringify({results,errors}));
  } finally {await app.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
