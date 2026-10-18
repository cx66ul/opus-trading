"""Browser acceptance against the installed Opus API (8870). No external writes."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs'/'intraday-implementation-20261011'


async def main():
    OUT.mkdir(exist_ok=True)
    errors=[]
    counts=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='chromium',headless=True)
        page=await browser.new_page(viewport=dict(width=1440,height=1100),device_scale_factor=1)
        page.on('pageerror',lambda err:errors.append(str(err)))
        page.on('request',lambda r:counts.append(r.url) if '/api/intraday/' in r.url else None)
        await page.goto('http://127.0.0.1:8870',wait_until='networkidle')
        await page.click('nav button[data-p="stock"]')
        await page.fill('#s-q','600152')
        results=[]
        for period in ('line','m1','m5','m30','m60','daily'):
            await page.select_option('#s-period',period)
            selector='#kchart' if period=='daily' else '#i-chart svg'
            await page.wait_for_selector(selector)
            await page.wait_for_timeout(200)
            assert not await page.locator('#s-err .err').count()
            if period!='daily':
                assert '600152' in await page.locator('#s-body h2').first.inner_text()
                assert '非交易时段' in await page.locator('#i-state').inner_text()
            await page.screenshot(path=str(OUT/f'chart-{period}.png'))
            results.append(period)
        await page.select_option('#s-period','m1')
        await page.wait_for_selector('#i-chart svg')
        await page.set_viewport_size(dict(width=1100,height=900))
        await page.wait_for_timeout(300)
        before=len(counts)
        await page.wait_for_timeout(16000)
        assert len(counts)>before, 'visible chart did not auto refresh'

        async def delay_old(route):
            response=await route.fetch()
            if '/600152?' in route.request.url:
                await asyncio.sleep(.7)
            await route.fulfill(response=response)
        await page.route('**/api/intraday/**',delay_old)
        await page.evaluate("""() => {
          document.querySelector('#s-q').value='600152';queryStock();
          document.querySelector('#s-q').value='603232';queryStock();
        }""")
        await page.wait_for_timeout(1800)
        assert '603232' in await page.locator('#s-body h2').first.inner_text(), 'late response overwrote selected stock'
        await page.unroute('**/api/intraday/**',delay_old)
        # Simulated stale state only in this test browser; production cache is untouched.
        async def stale(route):
            response=await route.fetch()
            data=await response.json()
            data.update(state='stale',fresh=False,new_signals_allowed=False,error='测试：数据源超时')
            await route.fulfill(json=data)
        await page.route('**/api/intraday/**',stale)
        await page.click('#s-go')
        await page.wait_for_selector('#i-chart svg')
        assert '暂停新增盘中提示' in await page.locator('#i-state').inner_text()
        await page.screenshot(path=str(OUT/'simulated-stale.png'))
        await page.unroute('**/api/intraday/**',stale)
        await page.fill('#s-q','600152')
        await page.select_option('#s-period','line')
        await page.wait_for_selector('#i-chart svg')
        assert not errors, errors
        await browser.close()
    async with httpx.AsyncClient() as client:
        mail=(await client.get('http://127.0.0.1:8870/api/mail/status')).json()
        news=(await client.get('http://127.0.0.1:8870/api/news/sources')).json()
        minute=(await client.get('http://127.0.0.1:8870/api/intraday/600152')).json()
    report=dict(periods=results,auto_refresh=True,response_race=True,stale_warning=True,page_errors=errors,
        intraday={k:minute[k] for k in ('running','state','latest','heartbeat','error','service_error')},
        mail={k:mail.get(k) for k in ('enabled','running','error')},news=news)
    (OUT/'ui-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='news'},ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
