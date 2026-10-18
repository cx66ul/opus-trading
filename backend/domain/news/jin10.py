# -*- coding: utf-8 -*-
"""金十公开快讯源；公开 HTTP 协议见 jin10_http，保留浏览器探针供排障。"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from loguru import logger
from backend.domain.news.jin10_http import Jin10Source


class Jin10ProbeSource(Jin10Source):
    """Playwright 探针：拦截金十页面的真实 XHR，落地样本。

    这是「探测优先于硬编」的落地方式 —— 先看清接口长什么样，再写解析器。
    """

    name = "jin10_probe"

    def probe(self, as_of: datetime, sample_out: Path, wait_ms: int = 12000) -> dict:
        """打开金十页面，收集所有 XHR/fetch 请求与响应，落盘样本。

        需要： pip install playwright && playwright install chromium
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "未安装 Playwright。请执行：\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )

        captured: list[dict] = []

        with sync_playwright() as p:
            # ★必须用 channel="chromium"：实测 headless=True 会去启动
            # chromium_headless_shell，在本机报 spawn EFTYPE。
            # channel 指定用完整 chrome，绕过该问题。
            browser = p.chromium.launch(headless=True, channel="chromium")
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()

            def on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    body = resp.text()
                    if len(body) < 200:
                        return                    # 跳过心跳等噪声
                    captured.append({
                        "url": resp.url,
                        "status": resp.status,
                        "content_type": ct,
                        "headers": {k: v for k, v in resp.request.headers.items()
                                    if k.lower() in ("x-app-id", "x-version", "referer")},
                        "body": body[:20000],
                    })
                    logger.info(f"[jin10探针] 捕获 {len(captured)} 个 JSON 请求: {resp.url[:110]}")
                except Exception:
                    pass

            page.on("response", on_response)
            logger.info("[jin10探针] 打开金十页面…")
            page.goto("https://www.jin10.com/", wait_until="domcontentloaded",
                      timeout=30000)
            page.wait_for_timeout(wait_ms)
            page.screenshot(path=str(sample_out.parent / "jin10_page.png"), full_page=False)
            browser.close()

        out = {
            "probed_at": as_of.isoformat(timespec="seconds"),
            "captured_count": len(captured),
            "requests": captured,
        }
        sample_out.parent.mkdir(parents=True, exist_ok=True)
        sample_out.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return out


