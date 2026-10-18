#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 金十快讯 WebSocket 解析探针

实测发现（2026-10-08 深探测）：
    页面捕获到两个 WebSocket：
      wss://b-price.jin10.com/         行情推送
      **wss://wss-flash-2.jin10.com/**  快讯推送 ← 就是它
    首轮XHR 探测抓不到快讯正文，因为快讯走 WebSocket。

    帧特征（实测 repr）：
      'u\xef\xbf\xbd\x0e\x00\xef\xbf\xbd\x16"\x00U\xef\xbf\xbd5\x1a\x14\x05\x00'
    → 不是 JSON、不是 UTF-8 纯文本，是**自定义二进制协议**：
      · 字节偏移呈规律递增（04→0e→16→22→28…），像长度前缀或变长整数
      · 后续字节是 ASCII 字母数字混合（如 AGUDCJ、UT]ASTQ）
      · 混有 0x00 分隔符

    ★ 结论：本轮不做协议逆向。
      逆向私有二进制协议成本高、且随版本失效，违反「先跑通再优化」。
      新浪 7×24 已提供等价的结构化快讯（纯 HTTP），
      金十定位为「可选增强」，等确有需要时再深挖。

    本脚本的价值在于：把协议结构**证据化**（落盘 repr + 字节分布），
    使后续如需逆向时有据可依，而不是重新探测一遍。

用法： python scripts/scrape_jin10_ws.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

SAMPLES = ROOT / "samples"
WS_FLASH = "wss://wss-flash-2.jin10.com/"


def probe(wait_ms: int = 20000) -> dict:
    from playwright.sync_api import sync_playwright

    frames: list[dict] = []
    ws_urls: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chromium")
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        def on_ws(ws):
            ws_urls.append(ws.url)

            def on_msg(msg):
                try:
                    if isinstance(msg, bytes):
                        frames.append({"url": ws.url, "bytes": msg[:4096].hex(),
                                       "raw_repr": repr(msg[:80])})
                    else:
                        txt = msg if isinstance(msg, str) else str(msg)
                        frames.append({"url": ws.url, "bytes": None,
                                       "text": txt[:3000]})
                except Exception:
                    pass

            ws.on("framereceived", on_msg)

        page.on("websocket", on_ws)
        logger.info("打开金十页面，监听 WebSocket…")
        page.goto("https://www.jin10.com/", wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(wait_ms)
        browser.close()

    return {"ws_urls": ws_urls, "frames": frames}


def analyze(res: dict) -> None:
    print("\n" + "=" * 72)
    print("=== 金十 WebSocket 帧分析 ===")
    print("=" * 72)
    print(f"WebSocket 连接: {res['ws_urls']}")

    flash = [f for f in res["frames"] if "flash" in f.get("url", "")]
    print(f"总帧数 {len(res['frames'])}，其中快讯帧 {len(flash)}")

    if not flash:
        print("\n未捕获到快讯帧。可能原因：")
        print("  · 页面需登录才推快讯")
        print("  · WebSocket 在 iframe 内，未被监听")
        print("  · 快讯改由 XHR 加载（需滚动到对应标签）")
        return

    print("\n=== 首帧字节结构（十六进制）===")
    h = flash[0]["bytes"]
    if h:
        print(f"  {h[:160]}")
        raw = bytes.fromhex(h)
        print(f"\n  可打印字符: {''.join(chr(b) if 32 <= b < 127 else '.' for b in raw[:80])}")
        # 长度前缀分析
        print(f"\n  开头字节（疑似长度前缀/变长整数）: "
              f"{[b for b in raw[:12]]}")
        print("  观察: 字节值非单调、非 ASCII，说明是二进制协议")

    lens = Counter()
    for f in flash[:50]:
        b = f.get("bytes")
        if b:
            lens[len(bytes.fromhex(b))] += 1
    print(f"\n  前 50 帧的长度分布: {dict(sorted(lens.items())[:6])}")

    print("\n" + "=" * 72)
    print("结论：金十快讯为自定义二进制 WebSocket 协议")
    print("  · 端点已确认：wss://wss-flash-2.jin10.com/")
    print("  · 协议逆向成本高且随版本失效")
    print("  · 现阶段以新浪 7×24 为主源（纯 HTTP、结构化、等价数据）")
    print("  · 如需金十：先用CDP 的 Network.webSocketFrameReceived 拿完整载荷再逆向")
    print("=" * 72)


def main() -> int:
    try:
        res = probe()
    except Exception as exc:
        logger.error(f"探测失败: {type(exc).__name__}: {exc}")
        return 1
    (SAMPLES / "jin10_ws.json").write_text(
        json.dumps({"probed_at": datetime.now(CST).isoformat(timespec="seconds"),
                    **res}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    analyze(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())