#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 消息抓取入口（铁律三：抓取与解析分离）

职责边界（重要）：
  本脚本**只做抓取**，把原始报文落进 raw_news 表就结束。
  **不做任何解析、判断、打标** —— 那是 M3 的活儿。

  这样做的收益：
    · 改 prompt / 换模型 / 调规则时，只需重跑解析，历史报文不必重抓
    · 免费接口有频次限制，重抓成本高且可能触发封禁
    · 原始报文可审计：LLM 打出「宁德时代涨停」时，可回看原文到底是什么

用法：
    python scripts/fetch_news.py --source sina_7x24 --limit 50
    python scripts/fetch_news.py --all --limit 50
    python scripts/fetch_news.py --all --stats
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data.sqlite import get_conn
from backend.domain.news.base import bootstrap_sources, get_source, registered_sources

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")


def save(items: list[dict], as_of: datetime) -> dict:
    """跨次、跨进程去重；同一源 ID 的正文修订保留为新报文。"""
    from backend.services.news_collection import save_items
    return save_items(items, as_of)


def show_stats() -> None:
    conn = get_conn()
    print("\n=== raw_news 统计 ===")
    rows = conn.execute(
        "SELECT source, count(*), sum(parsed) FROM raw_news GROUP BY source"
    ).fetchall()
    if not rows:
        print("  （空）")
        return
    for src, n, parsed in rows:
        print(f"  {src:14s} 共 {n:>5} 条，已解析 {parsed or 0:>5} 条，"
              f"待解析 {n - (parsed or 0):>5} 条")
    r = conn.execute(
        "SELECT ts, source FROM raw_news ORDER BY ts DESC, id DESC LIMIT 3"
    ).fetchall()
    if r:
        print("\n  最新 3 条：")
        for ts, src in r:
            print(f"    [{ts}] {src}")


def main() -> int:
    p = argparse.ArgumentParser(description="Hawk 消息抓取（只落原始报文，不解析）")
    p.add_argument("--source", default="", help="单个源名；--all 时忽略")
    p.add_argument("--all", action="store_true", help="抓所有已注册源")
    p.add_argument("--limit", type=int, default=50, help="每源抓取条数")
    p.add_argument("--stats", action="store_true", help="只显示统计")
    a = p.parse_args()

    if a.limit <= 0:
        p.error("--limit 必须大于 0")
    bootstrap_sources()
    logger.info(f"已注册消息源: {registered_sources()}")

    if a.stats:
        show_stats()
        return 0

    targets = registered_sources() if a.all else (
        [a.source] if a.source else ["sina_7x24"]
    )
    as_of = datetime.now(CST)          # 抓取时刻的真实时间（HTTP 层允许取系统时间）

    from backend.services.news_collection import collect_source
    results = []
    for name in targets:
        result = collect_source(get_source(name), as_of, a.limit)
        results.append(result)
        logger.info(f"[{name}] {result}")
    show_stats()
    # --all 中尚未实现的源明确报告 unsupported，已实现源失败不能被其他源掩盖。
    failed = any(r["state"] in ("error", "degraded") for r in results)
    successful = any(r["state"] == "ok" for r in results)
    return 1 if failed or not successful else 0


if __name__ == "__main__":
    raise SystemExit(main())