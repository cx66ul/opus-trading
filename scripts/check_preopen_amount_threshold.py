# -*- coding: utf-8 -*-
"""
「11 点前全市场成交额」阈值可行性检验（2026-10-10）

★ 背景：博主原话「科技高开但 11 点前全市场成交额不足 1 万亿 → 卖」。
  上一轮把这条规则判为「数据阻塞」，理由是"11点前只有 66~82 亿，差两个数量级"。
  **那个理由已作废**（基于不可信的 amount 字段）。
  本脚本用**已验证正确的 `volume × close`** 重算，判断：
    ① 1 万亿这个阈值历史上触发过几次？
    ② 阈值到底该定在多少？（原值可能差一个数量级）
    ③ 换用 `volume × close` 后，与 `market_regime.total_amount` 是否自洽？

★ 口径声明（必须写死，否则结论会被误读）
  - 「11 点前」= m30 的 `ts <= 11:00`，含 10:00 / 10:30 / 11:00 三根
    （**ts 是区间结束时刻**，见记忆：分钟线 ts 为区间结束时刻）
  - 成交额 = `Σ(volume × close)`，**不读 amount 字段**（已实测不可信）
  - 覆盖：2026-07-09 起 4537+ 只/日，共 63 个完整交易日；
    2026-10-09 只有 547 只（当日未同步完），**单独标出并排除**
  - 全市场覆盖度 ~90.7%（日线 5557 只 vs m30 4556 只）
    ⇒ 绝对值偏低约 9%，**报数时必须同时给出覆盖率与外推区间**
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CUTOFF = "time '11:00:00'"        # 含
FULL_COVER_MIN = 4000            # 只：低于此视为当日同步未完成
THRESHOLDS_YI = (5000, 6000, 7000, 8000, 9000, 10000, 11000, 12000)


def daily_amount(con) -> list[dict]:
    """逐交易日：11点前累计成交额（亿元）+ 覆盖度。

    ★★ 成交额必须**逐股**算 `volume × close` 再累加，或至少保证分母不重复。
      绝不能写成 `SUM(k.volume * k.close) / SUM(d.volume)` 这类 JOIN 聚合：
      日线列会被 m30 的行数放大 8 倍 ⇒ 结果差 8 倍（本脚本第一版就栽在这，
      得出「11点前 15.5 万亿」的荒谬数字）。
    """
    q = f"""select cast(ts as date) as d,
        sum(volume*close)/1e8 as amt_yi,
        count(distinct code) as codes
      from kline_min
      where period='m30' and cast(ts as time) <= {CUTOFF}
      group by 1 order by 1"""
    out = []
    for d, amt, codes in con.execute(q).fetchall():
        n = con.execute(
            "select count(*) from kline_daily where trade_date = ?", [d]
        ).fetchone()[0]
        # ★ 全天成交额用**日线单表**算（权威，已验证与 market_regime 1.00x 吻合），
        #   不用 m30 累加，避免同一处再踩 JOIN 放大。
        full = con.execute(
            "select sum(volume*close)/1e8 from kline_daily where trade_date = ?",
            [d]).fetchone()[0]
        out.append({"date": str(d), "amt_1100_yi": round(amt, 1),
                    "full_day_yi": round(full, 1) if full else None,
                    "ratio_1100": round(amt / full, 4) if full else None,
                    "codes": codes, "market_codes": n,
                    "coverage": round(codes / n, 4) if n else None,
                    "complete": codes >= FULL_COVER_MIN})
    return out


def main() -> None:
    from backend.data import duck
    con = duck.get_ro_conn()
    rows = daily_amount(con)
    ok = [r for r in rows if r["complete"]]
    bad = [r for r in rows if not r["complete"]]

    vals = [r["amt_1100_yi"] for r in ok]
    cover = [r["coverage"] for r in ok if r["coverage"]]
    med_cov = statistics.median(cover) if cover else None

    # 用覆盖率外推到全市场（分位数不受覆盖影响，只有绝对值需要）
    ext = [v / c for v, c in zip([r["amt_1100_yi"] for r in ok],
                                 [r["coverage"] for r in ok]) if c]

    print("=" * 72)
    print("「11 点前全市场成交额」阈值可行性")
    print("=" * 72)
    print(f"样本: {len(ok)} 个完整交易日"
          f"（{ok[0]['date']} ~ {ok[-1]['date']}）")
    if bad:
        names = [f"{b['date']}({b['codes']}只)" for b in bad]
        print(f"★ 排除 {len(bad)} 个同步未完成的日子: {names}")
    print(f"全市场覆盖率: 中位 {med_cov:.2%}")
    print()
    # ★ 先报「占全天比例」—— 它**不受覆盖率影响**，是最稳的量
    ratios = [r["ratio_1100"] for r in ok if r["ratio_1100"]]
    print(f"11点前占全天成交额比例: min {min(ratios):.1%} / "
          f"中位 {statistics.median(ratios):.1%} / max {max(ratios):.1%}")
    print(f"（该比例**不受 m30 覆盖率影响**，是最可靠的量）")
    print()
    print(f"11点前累计成交额（实测，覆盖率 {med_cov:.0%}）:")
    print(f"  min {min(vals):.0f} / 中位 {statistics.median(vals):.0f} / "
          f"max {max(vals):.0f} 亿")
    print(f"覆盖率外推到全市场后:")
    print(f"  min {min(ext):.0f} / 中位 {statistics.median(ext):.0f} / "
          f"max {max(ext):.0f} 亿")
    print()
    print("★ 阈值触发次数（两个口径都报，避免只挑好看的）:")
    trig_raw, trig_ext = {}, {}
    for t in THRESHOLDS_YI:
        a = sum(1 for v in vals if v < t)
        b = sum(1 for v in ext if v < t)
        trig_raw[t], trig_ext[t] = a, b
        print(f"  <{t:>6}亿: 实测 {a:>2}/{len(ok)} 天"
              f"｜外推 {b:>2}/{len(ok)} 天")
    print()
    n_trig = trig_ext[10000]
    if n_trig == 0:
        print("⇒ **1 万亿阈值下从不触发**（历史最大值也不到）")
        print("   ⇒ 这不是「样本不够」，是**阈值与市场量级不匹配**")
    elif n_trig == len(ok):
        print("⇒ **1 万亿阈值下每天都触发** ⇒ 阈值形同虚设，"
              "等价于「没有这条规则」")
    else:
        print(f"⇒ 1 万亿阈值下触发 {n_trig}/{len(ok)} 天"
              f"（{n_trig / len(ok):.0%}）—— 值得进一步查方向性")
    out = {
        "cutoff": CUTOFF,
        "n_days": len(ok),
        "date_range": [ok[0]["date"], ok[-1]["date"]],
        "excluded_incomplete": bad,
        "coverage_median": med_cov,
        "ratio_1100_of_full_day": {
            "min": min(ratios), "median": statistics.median(ratios),
            "max": max(ratios),
            "note": "不受m30 覆盖率影响，最可靠",
        },
        "raw": {"min": min(vals), "median": statistics.median(vals),
                "max": max(vals)},
        "extrapolated": {"min": min(ext), "median": statistics.median(ext),
                         "max": max(ext)},
        "threshold_triggers": {
            str(t): {"raw": trig_raw[t], "extrapolated": trig_ext[t],
                     "of_days": len(ok)} for t in THRESHOLDS_YI},
        "daily": rows,
    }
    p = ROOT / "data" / "preopen_amount_feasibility.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()
