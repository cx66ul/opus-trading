import json
from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path(__file__).resolve().parents[1] / "samples/jin10-20261011"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel="chromium")
    page = browser.new_page(locale="zh-CN")
    requests = []
    page.on("request", lambda r: requests.append({"url": r.url, "headers": {k:v for k,v in r.headers.items() if k in ("x-app-id", "x-version")}}) if "get_flash" in r.url else None)
    page.goto("https://www.jin10.com/", wait_until="domcontentloaded", timeout=30000)
    page.locator(".jin-flash-item-container").first.wait_for(timeout=20000)
    page.screenshot(path=str(out / "live-page.png"))
    text = page.locator("#jin_flash_list").inner_text()
    (out / "visible.txt").write_text(text, encoding="utf-8")
    html = page.locator(".jin-flash-item-container").first.evaluate("e => e.outerHTML")
    (out / "first-item.html").write_text(html, encoding="utf-8")
    print(text[:3500])
    print('ITEM', html[:4500])
    print('HTTP', json.dumps(requests, ensure_ascii=False))
    browser.close()
