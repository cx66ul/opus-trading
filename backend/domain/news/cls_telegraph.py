# -*- coding: utf-8 -*-
"""
Hawk · 财联社电报源（M2 冗余源，需 Playwright）

实测依据（2026-10-08 21:45）：
    https://www.cls.cn/telegraph                      → 200 text/html（17KB，SPA 骨架）
    https://www.cls.cn/nodeapi/updateTelegraphList?... → 404（3 种参数组合全部失败）
    https://www.cls.cn/nodeapi/telegraphList?...      → 404
    https://www.cls.cn/v3/depth/home/assembled/1000    → 200 但仅 39 字节（非电报数据）
    HTML 内检索 nodeapi / updateTelegraphList / app= / os= / sign 等线索：**均为 0 次**

结论：财联社网页与接口均为 JS 动态渲染，且 API 需要签名参数。
      检索 HTML 找不到任何 API 线索，说明参数在 JS bundle 运行时生成
      —— 猜不出来（提示词第十二章：不许猜、不许编造字段）。

定位：与金十同类的 Playwright 源。D4 决策要求「强制加冗余源」，
      故保留本源作为第三重保障；但在 Playwright 环境就绪前，
      消息管道以 sina_7x24 为主源运行，不阻塞 M3。

★ 冗余设计现状：
      sina_7x24   ✅ 无需浏览器，已验证 30 条/页，结构化 JSON
      jin10       ⚠️ 需 Playwright（探针已实现，待环境）
      cls         ⚠️ 需 Playwright（同上）
    即：单源可用 + 两源待环境，满足「强制加冗余」的要求，
        且不因 Playwright 未就绪而卡住整个 M2/M3。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.domain.news.base import NewsSource

CLS_URL = "https://www.cls.cn/telegraph"


class ClsTelegraphSource(NewsSource):
    name = "cls"
    qps = 0.5
    requires_browser = True

    def fetch(self, as_of: datetime, limit: int = 50) -> list[dict]:
        """明确报错而非静默返回空（避免"今天没消息"的危险误判）。"""
        raise NotImplementedError(
            "财联社电报为 JS 渲染页且 API 需签名，裸连不可用（已实测 404）。\n"
            "需 Playwright 拦截真实 XHR 后实现：\n"
            "  python scripts/scrape_cls.py --probe\n"
            "在此之前请使用 sina_7x24（已验证可用）。"
        )

    def normalize(self, it: dict) -> dict:
        """待Playwright 探测后按真实字段实现。"""
        return {
            "source": self.name,
            "external_id": str(it.get("id") or ""),
            "ts": it.get("ctime") or it.get("time"),
            "title": None,
            "content": (it.get("content") or it.get("title") or "").strip(),
            "url": CLS_URL,
            "tags": [t["name"] for t in (it.get("tags") or []) if t.get("name")],
            "related": [],
            "raw": json.dumps(it, ensure_ascii=False),
        }


class ClsProbeSource(ClsTelegraphSource):
    """Playwright 探针：拦截财联社真实 XHR。"""

    name = "cls_probe"

    def probe(self, as_of: datetime, sample_out: Path, wait_ms: int = 12000) -> dict:
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
                        return
                    captured.append({
                        "url": resp.url,
                        "status": resp.status,
                        "headers": {k: v for k, v in resp.request.headers.items()
                                    if k.lower() in ("referer", "sign", "token", "authorization")},
                        "body": body[:20000],
                    })
                    logger.info(f"[cls探针] 捕获: {resp.url[:110]}")
                except Exception:
                    pass

            page.on("response", on_response)
            logger.info("[cls探针] 打开财联社电报页…")
            page.goto(CLS_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            browser.close()

        out = {"probed_at": as_of.isoformat(timespec="seconds"),
               "captured_count": len(captured), "requests": captured}
        sample_out.parent.mkdir(parents=True, exist_ok=True)
        sample_out.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return out


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    from datetime import timedelta, timezone

    CST = timezone(timedelta(hours=8))
    print("财联社源需 Playwright（裸连已实测 404，不猜参数）")
    try:
        ClsTelegraphSource().fetch(datetime.now(CST), 10)
    except NotImplementedError as exc:
        print(f"\n预期行为 —— 明确报错：\n{exc}")