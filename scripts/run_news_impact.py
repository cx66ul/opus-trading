# -*- coding: utf-8 -*-
"""M8③ 消息—行情分钟级联动统计（真实数据）。

★★ 这不是「实现完了跑一下」—— 先看三条硬约束，它们决定了本脚本能产出什么
------------------------------------------------------------------------
1. **m5 只覆盖 3 只标的**
   实测：m1/m5 各 1446 行 / 3 只（只同步了关注池+持仓+自选），
   m30/m60 各约 201 万行 / 4182 只（全市场）。
   ⇒ `news_impact.py` 原有的 `WINDOWS = {3m, 30m, 1d}` 全部映射到 m5，
     对97% 的消息都会返回「无数据」。**这不是实现问题，是数据覆盖问题。**
   ⇒ 本脚本按周期**重新定义窗口**（见 `WINDOWS_BY_PERIOD`），
     并在报告里明写「该窗口用了哪个周期、覆盖多少只」。

2. **3 分钟窗口在任何可用周期下都取不到**
   腾讯 m1 只有 2 天、m5 只有 11 天、m30 起才有 61 天覆盖。
   而本项目的消息全是**盘后**（2026-10-08 21:33 之后），对应**次日开盘**。
   ⇒ 严格「3 分钟」窗口需要 m1，而 m1 无覆盖 ⇒ **该窗口必须报 unavailable**，
     不能用 m5「近似」后当成真的 3 分钟统计。
     `news_impact.py` 的旧注释说「宁可粗一点」，但那会让报告把
     「5 分钟的涨跌」标成「3 分钟」—— 口径不符会误导判断，故本脚本改掉。

3. **样本量极小**
   实测 80 条消息中**只有 1 条**有关联代码（601298青岛港）。
   1 条样本无法做任何统计（胜率/分布都没有意义）。
   ⇒ 脚本必须输出「样本不足」并**拒绝**给出方向性结论。

★ 口径纪律
----------
- 无分钟线 → `unavailable`，**不判wrong**（沿用 `news_impact.judge`）
- 基准价取**消息之后第一根**分钟线的收盘（盘后消息 ⇒ 次日开盘价，这是可成交价）
- 涨红跌绿由展示层负责，本脚本只给数值
- 每次运行都报**覆盖率**，让「没数据」与「数据说没事」可区分

运行：
    python scripts/run_news_impact.py
    python scripts/run_news_impact.py --limit 200
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb                                   # noqa: E402

from backend.domain.news_impact import judge     # noqa: E402

CST = timezone(timedelta(hours=8))
DB = _ROOT / "data" / "hawk.sqlite"
DUCK = _ROOT / "data" / "hawk.duckdb"

# ★ 窗口 → (周期, 根数)。**根数必须匹配该周期的实际时长**
#   m30 每根 30 分钟：一个交易日 8 根
#   m60 每根 60 分钟：一个交易日 4 根
#   ★ 3 分钟窗口**不在此列** —— 可用周期里没有比30 分钟更细的覆盖，
#     强行用 m30 第 1 根当「3 分钟」会把 30 分钟的涨跌标成 3 分钟。
WINDOWS_BY_PERIOD = {
    "m30": {"30m": 1, "1d": 8},
    "m60": {"30m": 1, "1d": 4},
    # m5/m1 有覆盖时才启用（当前仅 3 只标的）
    "m5": {"3m": 1, "30m": 6, "1d": 48},
    "m1": {"3m": 3, "30m": 30, "1d": 240},
}

MIN_SAMPLE_FOR_STAT = 10


def period_coverage(con) -> dict[str, int]:
    rows = con.execute(
        "SELECT period, count(DISTINCT code) FROM kline_min "
        "GROUP BY period ORDER BY 2 DESC").fetchall()
    return {r[0]: int(r[1]) for r in rows}


def best_period(cov: dict[str, int], need_fine: bool = False) -> str | None:
    """按覆盖度选可用周期。`need_fine=True` 时要求 m5/m1（用于 3m 窗口）。"""
    order = (("m5", "m30", "m60") if need_fine
             else ("m30", "m60", "m5"))
    for p in order:
        if cov.get(p, 0) > 0:
            return p
    return None


def load_news(limit: int) -> list[dict]:
    """读有关联代码的消息（`related_codes` 是 JSON 数组字符串）。"""
    import json
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT id, ts, direction, strength, confidence,
                      content, related_codes, llm_reason
               FROM news
               WHERE related_codes IS NOT NULL
                 AND trim(related_codes) NOT IN ('', '[]', 'null')
               ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            codes = [c for c in json.loads(r[6] or "[]") if isinstance(c, str)]
        except (json.JSONDecodeError, TypeError):
            codes = []
        if not codes:
            continue
        out.append({"id": r[0], "ts": r[1], "direction": r[2] or "neutral",
                    "strength": r[3] or 1, "confidence": r[4],
                    "content": (r[5] or "")[:90],
                    "codes": codes, "reason": (r[7] or "")[:80]})
    return out


def eval_one(con, item: dict, code: str, period: str) -> dict:
    """评估单条消息在该标的上的各窗口走势。"""
    windows = WINDOWS_BY_PERIOD.get(period, {})
    out: dict[str, dict] = {}
    for label, need in windows.items():
        rows = con.execute(
            """SELECT ts, open, close, high, low FROM kline_min
               WHERE code = ? AND period = ?
                 AND CAST(ts AS VARCHAR) >= ?
               ORDER BY ts ASC LIMIT ?""",
            [code, period, item["ts"].replace("T", " ")[:19], need]).fetchall()
        if not rows:
            out[label] = {"available": False,
                          "note": f"无 {period} 数据"}
            continue
        # ★★ 基准必须是**第一根的 open**（索引 1），不是它的 close（索引 2）。
        #   分钟K线的 ts 是**区间结束时刻**（ts=10:00 那根 = 09:30~10:00），
        #   所以第一根的 close 已经是「消息后半小时」的价格，
        #   用它当基准会把消息后**第一个窗口的涨跌整个吞掉**
        #   （实测 601298：close 基准 ⇒ 30m +0.00%；open 基准 ⇒ 30m +0.90%，
        #     判定从 wrong 翻成 correct）。
        base = float(rows[0][1]) or float(rows[0][2])
        last = float(rows[-1][2])
        highs = [float(r[3]) for r in rows]
        lows = [float(r[4]) for r in rows]
        out[label] = {
            "available": True,
            "base": base,
            "ret_pct": round((last / base - 1) * 100, 3),
            "max_gain": round((max(highs) / base - 1) * 100, 3),
            "max_loss": round((min(lows) / base - 1) * 100, 3),
            "bars": len(rows),
            "first_ts": str(rows[0][0])[:16],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="M8③ 消息—行情联动统计")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    if not DUCK.exists():
        print(f"行情库不存在: {DUCK}")
        return 1

    con = duckdb.connect(str(DUCK), read_only=True)
    cov = period_coverage(con)
    print("=" * 84)
    print("M8③ 消息—行情分钟级联动统计")
    print("=" * 84)
    print(f"{'周期':>6s}{'覆盖标的':>10s}  可用窗口")
    for p, c in sorted(cov.items()):
        w = WINDOWS_BY_PERIOD.get(p, {})
        print(f"{p:>6s}{c:>10d}  {'/'.join(w) if w else '—'}")

    per = best_period(cov, need_fine=False)
    print()
    print(f"★ 主口径周期: {per}（覆盖最广）")
    if cov.get("m5", 0) == 0 and cov.get("m1", 0) == 0:
        print("  ⚠ m1/m5 无覆盖 ⇒ **「3 分钟」窗口不可用**")
        print("    （可用周期最细是 m30=30 分钟；用m30 第 1 根冒充「3 分钟」")
        print("     会把 30 分钟的涨跌标成 3 分钟，口径不符）")
    else:
        print(f"  ✓ 有 m5/m1 覆盖（m5={cov.get('m5',0)} 只），3 分钟窗口可用")

    news = load_news(args.limit)
    total_msgs = sqlite3.connect(f"file:{DB}?mode=ro", uri=True).execute(
        "SELECT count(*) FROM news").fetchone()[0]
    print()
    print(f"消息库共 {total_msgs} 条，其中{len(news)} 条有关联个股代码")
    if not news:
        print()
        print("★ 无可评估样本 —— 报告到此为止，不给任何方向性结论。")
        print("  原因：消息库里 related_codes 全为空（LLM 未关联到个股）。")
        con.close()
        return 0

    print()
    print("=" * 84)
    print("逐条评估（基准 = 消息后第一根分钟线的**开盘价**，盘后消息即次日开盘）")
    print("=" * 84)
    print("★ 注意：分钟K 线的 ts 是**区间结束时刻**（10:00 那根 = 09:30~10:00），")
    print("  所以基准必须用第一根的 open；若用它的 close，消息后第一个窗口的")
    print("  涨跌会被整个吞掉（实测 30m 窗口会恒为 0，看起来平静实则失效）。")
    print(f"{'ID':>4s}{'代码':>8s}{'方向':>8s}{'消息时间':>18s}"
          f"{'30m':>9s}{'1d':>9s}{'30m最高':>9s}{'30m最低':>9s}  判定")
    stats: dict[str, list[float]] = defaultdict(list)
    avail = 0
    for n in news:
        for code in n["codes"][:3]:
            w = eval_one(con, n, code, per)
            w30 = w.get("30m", {})
            wd = w.get("1d", {})
            if w30.get("available"):
                avail += 1
                stats["30m"].append(w30["ret_pct"])
                stats["1d"].append(wd["ret_pct"] if wd.get("available") else None)
                stats["gain30"].append(w30["max_gain"])
                stats["loss30"].append(w30["max_loss"])
            v30 = (f"{w30['ret_pct']:+.2f}%" if w30.get("available") else "无")
            v1d = (f"{wd['ret_pct']:+.2f}%" if wd.get("available") else "无")
            vg = (f"{w30['max_gain']:+.2f}%" if w30.get("available") else "无")
            vl = (f"{w30['max_loss']:+.2f}%" if w30.get("available") else "无")
            verdict, note = judge(n["direction"],
                                  w30["ret_pct"] if w30.get("available") else None)
            print(f"{n['id']:>4d}{code:>8s}{n['direction']:>8s}{n['ts'][:16]:>18s}"
                  f"{v30:>9s}{v1d:>9s}{vg:>9s}{vl:>9s}  {verdict}")
            if n["id"] == news[0]["id"]:
                print(f"     └ {n['content'][:70]}")
                print(f"       LLM: {n['reason']}")

    print()
    print("=" * 84)
    print("汇总统计")
    print("=" * 84)
    print(f"可评估样本：{avail} 条（消息数 {len(news)}）")
    if avail < MIN_SAMPLE_FOR_STAT:
        print()
        print(f"★★ 样本不足（{avail} < {MIN_SAMPLE_FOR_STAT}）⇒ 不做任何统计结论。")
        print("   1 条样本无法判断「LLM 打标准不准」—— 那只是单次巧合。")
        print("   要产出可信统计需要：")
        print("     ① 消息库积累多轮**盘中**消息（当前 80 条全是 2026-10-08 盘后）")
        print("     ② 更多消息被关联到个股（当前仅 1/80 = 1.25%）")
        print(f"     ③ m5/m1 覆盖要覆盖全市场（当前 {cov.get('m5',0)} 只）")
        con.close()
        return 0

    import statistics
    for k in ("30m", "1d"):
        v = [x for x in stats.get(k, []) if x is not None]
        if len(v) >= MIN_SAMPLE_FOR_STAT:
            print(f"  {k:>4s} n={len(v):<4d} 均值{statistics.fmean(v):+7.2f}% "
                  f"中位{statistics.median(v):+7.2f}% "
                  f"胜率{len([x for x in v if x > 0]) / len(v) * 100:5.1f}%")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
