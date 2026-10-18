#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · Playwright 探测器（金十 / 财联社）

作用（对应提示词第十二章「不许猜」）：
    JS 渲染页的 API 端点与签名参数在运行时生成，靠猜是猜不出来的
    （实测：财联社 nodeapi 三种参数组合全 404，HTML 内检索 API 线索 0 次）。
    本脚本用真实浏览器打开页面，**拦截 XHR/fetch 的真实请求与响应**，
    落盘到 samples/，人工比对确认后再写解析器 —— 这就是附录 A.5 的探测流程，
    只是这里必须用浏览器才能拿到样本。

产出：
    samples/jin10_xhr.json    金十页面捕获的 JSON 请求
    samples/jin10_page.png    金十页面截图（人工核对用）
    samples/cls_xhr.json      财联社捕获
    samples/cls_page.png

用法：
    python scripts/scrape_news_browser.py --source jin10
    python scripts/scrape_news_browser.py --source cls
    python scripts/scrape_news_browser.py --source all
    python scripts/scrape_news_browser.py --analyze      # 分析已捕获样本

⚠️ 合规（附录 A.3 第 7 条）：
    本脚本只打开公开页面、读取已渲染内容，不做批量抓取，
    低频访问（默认每源仅 1 次页面加载）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.news.cls_telegraph import CLS_URL, ClsProbeSource  # noqa: E402
from backend.domain.news.jin10 import Jin10ProbeSource                    # noqa: E402

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

SAMPLES = ROOT / "samples"

TARGETS = {
    "jin10": (Jin10ProbeSource, "https://www.jin10.com/", "jin10_xhr.json"),
    "cls": (ClsProbeSource, CLS_URL, "cls_xhr.json"),
}


def probe(name: str, wait_ms: int) -> dict:
    cls, url, out_name = TARGETS[name]
    src = cls()
    out = SAMPLES / out_name
    logger.info(f"[{name}] 开始探测 {url}")
    res = src.probe(datetime.now(CST), out, wait_ms=wait_ms)
    logger.info(f"[{name}] 捕获 {res['captured_count']} 个 JSON 请求 -> {out.name}")
    return res


def analyze(name: str) -> None:
    """分析已捕获样本，找出真正含快讯数据的端点。

    判定标准（不靠猜）：
      1. 响应 JSON 中含疑似快讯字段（id/content/title/time 等）
      2. 数据条数 >= 3
      3. 排除心跳/配置类接口
    """
    cls, url, out_name = TARGETS[name]
    path = SAMPLES / out_name
    if not path.exists():
        logger.error(f"样本不存在：{path}，先运行 --source {name}")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    reqs = data.get("requests", [])
    print(f"\n=== {name} 捕获分析（共 {len(reqs)} 个请求）===")

    scored = []
    for r in reqs:
        body = r.get("body", "")
        score = 0
        why = []
        for kw, pt in [("telegraph", 3), ("flash", 3), ("news", 2), ("lives", 2),
                       ("content", 2), ("title", 2), ("ctime", 2), ("rich_text", 3),
                       ("article", 1), ("list", 1)]:
            if kw in body:
                score += pt
                why.append(kw)
        try:
            obj = json.loads(body)
            # 数一下有多少条记录
            n = _count_records(obj)
            if n and n >= 3:
                score += min(n, 10)
                why.append(f"{n}条")
        except json.JSONDecodeError:
            pass
        scored.append((score, r, why, n if "n" in dir() else 0))

    scored.sort(key=lambda x: -x[0])
    print(f"\n按「像快讯数据」的可能性排序（前 6）：\n")
    for score, r, why, n in scored[:6]:
        flag = "★ 疑似" if score >= 12 else ("可能" if score >= 6 else "噪声")
        print(f"  [{flag:5s} 分数{score:>3d}] {r['url'][:88]}")
        print(f"         命中: {why[:6]}  响应 {len(r['body'])} 字节")
        print(f"         请求头: {list(r.get('headers', {}).keys())}")
        print()

    if scored and scored[0][0] >= 12:
        best = scored[0]
        print("=" * 70)
        print(f"最可能的快讯端点：\n  {best[1]['url']}")
        print(f"\n请求头（可能有签名，必须完整复现）：")
        for k, v in (best[1].get("headers") or {}).items():
            print(f"  {k}: {v[:80]}")
        print("\n响应前 800 字节：")
        print(best[1]["body"][:800])
        print("=" * 70)
        print("\n→ 请人工确认此端点，然后据实实现解析器（不要凭猜测字段）")


def _count_records(obj) -> int:
    """猜测 JSON 里的记录条数（只用于筛选，不作为字段依据）。"""
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        for k in ("data", "result", "items", "list", "news", "telegraph"):
            if k in obj:
                return _count_records(obj[k])
        for v in obj.values():
            if isinstance(v, list) and len(v) >= 3:
                return len(v)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="金十/财联社 Playwright 探测")
    p.add_argument("--source", default="all",
                   help="jin10 | cls | all | analyze")
    p.add_argument("--wait-ms", type=int, default=12000,
                   help="页面加载后等待 XHR 的毫秒数")
    p.add_argument("--analyze", action="store_true", help="只分析已捕获样本")
    a = p.parse_args()

    if a.analyze:
        for name in TARGETS:
            analyze(name)
        return 0

    names = list(TARGETS) if a.source == "all" else [a.source]
    for n in names:
        if n not in TARGETS:
            logger.error(f"未知源 {n}")
            return 2
        try:
            probe(n, a.wait_ms)
        except Exception as exc:
            logger.error(f"[{n}] 探测失败：{type(exc).__name__}: {exc}")
    for n in names:
        analyze(n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())