#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 金十快讯深探测（WebSocket + 滚动触发）

实测背景（2026-10-08 首轮探测）：
    页面初始加载捕获到 25 个 JSON 端点，但**没有一个是快讯正文**。
    已捕获的 `[签名]` 端点是：
      {随机子域名}.z3c.jin10.com/flash/hot          热门快讯（只有 id/n/t，无正文）
      {随机子域名}.z3c.jin10.com/classify          分类
      {随机子域名}.z3c.jin10.com/tv/index/list     视频列表
      search-open-api.jin10.com/search/getHotWords 热搜词
    ⇒ 快讯正文**走 WebSocket 推送**（与提示词附录 A.1 描述一致：
      「Playwright 拦截 XHR / WebSocket」）。XHR 抓不到是必然的。

本脚本做两件事：
    1. 监听 WebSocket 帧，解析出快讯正文端点与协议
    2. 滚动页面触发懒加载，看是否有快讯列表的 XHR 补充

★ 为什么要深挖：
    新浪 7×24 已可用（纯HTTP），金十的价值在于**双源交叉验证**（D4 决策）。
    如果金十成本过高，就保留新浪为主源，把金十列为「可选增强」。
    这符合「先跑通再优化」的执行约定，不为完整性牺牲可靠性。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

SAMPLES = ROOT / "samples"


def deep_probe(url: str = "https://www.jin10.com/", wait_ms: int = 18000) -> dict:
    from playwright.sync_api import sync_playwright

    xhr: list[dict] = []
    ws_frames: list[dict] = []
    ws_urls: set[str] = set()

    with sync_playwright() as p:
        # channel="chromium" 是必须的：headless_shell 在本机报 spawn EFTYPE
        browser = p.chromium.launch(
            headless=True, channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()

        # ---- WebSocket 监听 ----
        def on_ws_open(ws):
            ws_urls.add(ws.url)
            logger.info(f"[ws] 连接: {ws.url[:100]}")

        def on_ws_msg(msg):
            try:
                txt = msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
                ws_frames.append({"url": next(iter(ws_urls), ""), "payload": txt[:3000]})
                logger.info(f"[ws帧] {len(txt)} 字节")
            except Exception:
                pass

        page.on("websocket", on_ws_open)
        page.on("websocket", lambda ws: ws.on("framereceived", on_ws_msg))

        # ---- XHR 监听 ----
        def on_resp(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = resp.text()
                if len(body) < 200:
                    return
                xhr.append({
                    "url": resp.url,
                    "status": resp.status,
                    "headers": {k: v for k, v in resp.request.headers.items()
                                if k.lower() in ("x-app-id", "x-version", "referer")},
                    "body": body[:20000],
                })
            except Exception:
                pass

        page.on("response", on_resp)

        logger.info(f"打开 {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(6000)

        # ---- 滚动触发懒加载 ----
        logger.info("滚动页面触发懒加载…")
        for i in range(6):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1200)

        # ---- 尝试展开「快讯」标签 ----
        for kw in ("快讯", "全部", "最新"):
            try:
                el = page.get_by_text(kw, exact=False).first
                if el and el.is_visible():
                    el.click(timeout=3000)
                    logger.info(f"点击「{kw}」")
                    page.wait_for_timeout(2500)
                    break
            except Exception:
                continue

        page.wait_for_timeout(wait_ms)
        page.screenshot(path=str(SAMPLES / "jin10_deep.png"), full_page=False)
        browser.close()

    return {"xhr": xhr, "ws_urls": sorted(ws_urls), "ws_frames": ws_frames}


def analyze(res: dict) -> None:
    print("\n" + "=" * 74)
    print("=== 金十深探测分析 ===")
    print("=" * 74)
    print(f"XHR 端点: {len(res['xhr'])} 个")
    print(f"WebSocket 连接: {len(res['ws_urls'])} 个")
    for u in res["ws_urls"]:
        print(f"  {u}")
    print(f"WebSocket 帧: {len(res['ws_frames'])} 帧")

    # 新出现的端点（首轮没见过的）
    known = ("slide_", "iconify", "config.json", "data.json", "flir.json",
             "categories", "top_article", "ads3")
    print("\n=== 新出现的 XHR 端点 ===")
    found = False
    for r in res["xhr"]:
        if not any(k in r["url"] for k in known):
            print(f"  {r['url'][:110]}")
            print(f"    headers: {r.get('headers')}")
            print(f"    body: {r['body'][:250]}")
            found = True
    if not found:
        print("  （无新端点）")

    if res["ws_frames"]:
        print("\n=== WebSocket 帧样本（可能就是快讯正文）===")
        for f in res["ws_frames"][:5]:
            print(f"  {f['payload'][:500]}")
            print()
    else:
        print("\n=== WebSocket 未捕获到帧 ===")
        print("  可能原因：")
        print("   1. 页面需登录才推快讯")
        print("   2. 首屏不推送，需交互触发")
        print("   3. 快讯在 iframe 里")


def main() -> int:
    logger.info("开始金十深探测（WebSocket + 滚动）")
    try:
        res = deep_probe()
    except Exception as exc:
        logger.error(f"探测失败: {type(exc).__name__}: {exc}")
        return 1
    out = SAMPLES / "jin10_deep.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"结果已落盘: {out.name}")
    analyze(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())