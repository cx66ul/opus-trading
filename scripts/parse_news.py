#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 消息解析与打标入口（铁律三的解析层）

与 fetch_news.py 严格分离：
    fetch_news.py  只抓原始报文 -> raw_news(parsed=0)
    parse_news.py  只读 raw_news -> 打标 -> news 表，标记 parsed=1

这样改 prompt 或换模型时，**不必重抓**（免费源有频次限制，重抓可能触发封禁）。

★ 本地 7B 单条约 1-3 秒，40 条约 1-2 分钟。
  因此解析是**批处理任务**，不是实时路径：
    · 盘中由调度层按 D7/D8 决策，仅在需要时解析新增部分
    · 大批量补解析（如首次回填）建议 -j 并发，但本地模型显存有限，
      并发超过 2 反而会因显存不足变慢，故默认串行

用法：
    python scripts/parse_news.py --limit 10# 解析 10 条看效果
    python scripts/parse_news.py --all                # 解析全部待处理
    python scripts/parse_news.py --stats              # 只看统计
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data.sqlite import (          # noqa: E402
    fetch_unparsed_raw_news,
    get_conn,
    insert_news,
    mark_raw_parsed,
    valid_codes,
)
from backend.domain.llm import (            # noqa: E402
    OllamaProvider,
    PROMPT_VERSION,
    rule_based_fallback,
)

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")


def simhash_of(text: str) -> str:
    """64 位 SimHash，用于近似重复消息去重（附录 B.2 news.simhash 唯一约束）。

    简化为 16 位十六进制（64 bit）。对短文本足够用：
    完全相同 -> 必然相同；高度相似 -> 大概率相同。
    """
    t = "".join((text or "").split())
    if not t:
        t = "empty"
    h = hashlib.sha1(t.encode("utf-8")).digest()[:8]
    return h.hex()


def parse_one(row, provider, codes: list[str], as_of: datetime,
              wallclock: datetime) -> dict | None:
    """解析一条 raw_news。返回统计 dict。

    ★ `wallclock` 是审计表的墙钟列（见 sqlite.py 的 llm_audit DDL 注释）。
      它在 main() 里取一次然后往下传——**领域层与本函数都不自己取系统时间**。
    """
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return {"ok": False, "reason": "payload 不是合法 JSON"}

    content = payload.get("content") or ""
    if not content:
        return {"ok": False, "reason": "内容为空"}

    tag = provider.tag_news(content, as_of, codes, wallclock=wallclock)

    news_row = tag.to_row(
        ts=payload.get("ts") or row["ts"],
        title=payload.get("title") or "",
        content=content,
        url=payload.get("url") or "",
        source=row["source"],
        simhash=simhash_of(content),
        tags=payload.get("tags") or [],
        as_of=as_of,                    # ★ 显式注入，铁律二
    )
    nid = insert_news(news_row)
    if nid is None:
        return {"ok": False, "reason": "simhash 重复（同一事件已有记录）"}
    return {"ok": True, "news_id": nid, "tag": tag, "content": content}


def show_stats() -> None:
    conn = get_conn()
    print("\n=== 解析进度 ===")
    r = conn.execute(
        "SELECT count(*), sum(parsed) FROM raw_news"
    ).fetchone()
    total, parsed = r[0], (r[1] or 0)
    print(f"  raw_news: 共 {total} 条，已解析 {parsed} 条，待解析 {total - parsed} 条")

    n = conn.execute("SELECT count(*) FROM news").fetchone()[0]
    print(f"  news:已结构化 {n} 条")

    if n:
        print("\n=== 打标分布 ===")
        for d, c in conn.execute(
            "SELECT direction, count(*) FROM news GROUP BY direction ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {d:10s} {c:>4} 条")
        print()
        for s, c in conn.execute(
            "SELECT strength, count(*) FROM news GROUP BY strength ORDER BY 1"
        ).fetchall():
            print(f"  强度 {s}: {c:>4} 条")


def show_cost(as_of: datetime, days: int = 30) -> None:
    """LLM 成本与用量统计（规格 2.4 要求 4 / M3）。

    ★ 口径说明：本地 7B 的 `cost_cny` 恒为 0，这是**真实成本**不是占位——
      真实开销是电费与硬件折旧，不摊到单次调用。
      真正有用的是 token 数与耗时：它们决定「本地够不够快」。
    """
    from backend.data.sqlite import llm_cost_summary

    s = llm_cost_summary(as_of.strftime("%Y-%m-%d"), days)
    print(f"\n=== LLM 用量（{s['since']} ~ {s['as_of']}，共 {days} 天）===")
    if not s["rows"]:
        print("  窗口内无调用记录。")
        return
    print(f"  {'provider':10s}{'task':16s}{'次数':>6}{'成功':>6}"
          f"{'输入tok':>10}{'输出tok':>10}{'总耗时s':>10}{'成本':>8}")
    for r in s["rows"]:
        print(f"  {r['provider']:10s}{r['task']:16s}{r['n_calls']:>6}{r['n_ok']:>6}"
              f"{r['prompt_tokens']:>10}{r['completion_tokens']:>10}"
              f"{r['elapsed_ms'] / 1000:>10.1f}{r['cost_cny']:>8.4f}")
    tot_tok = sum(r["prompt_tokens"] + r["completion_tokens"] for r in s["rows"])
    tot_ms = sum(r["elapsed_ms"] for r in s["rows"])
    n = sum(r["n_calls"] for r in s["rows"])
    print(f"\n  合计 {n} 次调用 / {tot_tok} token / {tot_ms / 1000:.1f}s"
          f"，平均 {tot_ms / max(1, n):.0f}ms/次")
    if n:
        print("  ⚠ 单次平均超过 2000ms 时，本地 7B 已接近交互上限，"
              "批量打标会明显拖慢调度。")


def main() -> int:
    p = argparse.ArgumentParser(description="Hawk 消息解析与打标（消费 raw_news）")
    p.add_argument("--limit", type=int, default=10, help="本次解析条数")
    p.add_argument("--source", default="", help="只解析指定来源（如 jin10）")
    p.add_argument("--all", action="store_true", help="解析全部待处理（上限 500）")
    p.add_argument("--stats", action="store_true", help="只显示统计")
    p.add_argument("--cost", action="store_true", help="显示 LLM 成本/用量统计")
    p.add_argument("--cost-days", type=int, default=30, help="成本统计窗口天数")
    p.add_argument("--model", default="hawk-qwen2.5-7b")
    a = p.parse_args()

    if a.cost:
        show_cost(datetime.now(CST), a.cost_days)
        return 0

    if a.stats:
        show_stats()
        return 0

    provider = OllamaProvider(model=a.model)
    if not provider.available():
        logger.error(
            f"Ollama 模型 {a.model} 未就绪。\n"
            f"请先执行： ollama pull {a.model}\n"
            f"（注意：若已下载但仍报错，检查 ollama serve 是否在运行）"
        )
        return 1

    limit = 500 if a.all else a.limit
    rows = (get_conn().execute(
        "SELECT * FROM raw_news WHERE parsed=0 AND source=? ORDER BY id LIMIT ?",
        (a.source, limit)).fetchall() if a.source else fetch_unparsed_raw_news(limit=limit))
    if not rows:
        logger.info("没有待解析的消息。先运行 scripts/fetch_news.py")
        show_stats()
        return 0

    codes = sorted(valid_codes())
    logger.info(f"待解析 {len(rows)} 条，本地股票库 {len(codes)} 只")
    # ★ 墙钟只在这里取一次（入口脚本的特权），往下只传不取。
    #   用途只有审计：LLM 推理耗时/token 是运维事实，业务时点冒充不了。
    wallclock = datetime.now(CST)
    # ★★ `as_of` 必须逐条取**该消息自己的时间**（`raw_news.ts`），
    #   而不是「现在」。
    #   事故实测（2026-10-10）：原先写 `as_of = datetime.now(CST)`
    #   且与 `wallclock` 同值⇒ `llm_audit.ts` 与 `as_of` 两列完全相同
    #   ⇒ 回放/复盘时所有历史消息的审计都塌缩到同一天，
    #      「按消息时点重算」变成「按最后一次运行日重算」，
    #   **而这个错误没有任何报错**（两列都看着很合理）。
    #   语义：ts=墙钟（运维事实），as_of=业务时点（这条新闻的事件时间）。
    def _as_of_of(row) -> datetime:
        """该条消息的业务时点；取不到就退回墙钟并告警，不静默用 now。"""
        raw = row["ts"] or ""
        try:
            dt = datetime.fromisoformat(str(raw))
            return dt if dt.tzinfo else dt.replace(tzinfo=CST)
        except (TypeError, ValueError):
            logger.warning(f"raw_news[{row['id']}] 的 ts 无法解析（{raw!r}），"
                           f"退回墙钟")
            return wallclock

    ok = fail = dup = 0
    done_ids = []
    t0 = datetime.now()
    for i, row in enumerate(rows, 1):
        res = parse_one(row, provider, codes, _as_of_of(row), wallclock)
        if res and res["ok"]:
            ok += 1
            done_ids.append(row["id"])
            tag = res["tag"]
            mark = {"bull": "利好", "bear": "利空"}.get(tag.direction, "中性")
            print(f"  [{i:>3}/{len(rows)}] {mark}(强度{tag.strength}) "
                  f"{res['content'][:44]}")
            if tag.related_codes:
                print(f"        关联: {tag.related_codes}")
        else:
            fail += 1
            done_ids.append(row["id"])     # 失败也标记，避免死循环重复解析
            reason = res["reason"] if res else "未知"
            # ★ 修复：原为 `dup += "simhash" in str(reason)`，
            #   把 bool 加进整数计数器，dup 永远只能取 0 或 1，统计不出真实去重数。
            if "simhash" in str(reason):
                dup += 1
            logger.warning(f"  [{i:>3}] 跳过：{reason}")
        if i % 10 == 0:
            mark_raw_parsed(done_ids[-10:])

    mark_raw_parsed(done_ids)
    el = (datetime.now() - t0).total_seconds()
    logger.info(f"解析完成：成功 {ok}，跳过 {fail}，耗时 {el:.0f}s "
                f"（平均 {el / max(1, len(rows)):.1f}s/条）")
    show_stats()
    logger.info(f"prompt 版本: {PROMPT_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())