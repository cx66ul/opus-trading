# -*- coding: utf-8 -*-
"""修正 `market_regime.total_amount`（被 688 单位错误连带污染，2026-10-10）

★ 为什么要修：
  `total_amount` 由 parquet 快照的 `close*volume` 算出，
  而那份 volume 的 688 行被放大 100 倍 ⇒ 成交额虚高约 15 倍。
  2026-09-29实测：修前 218130 亿 → 修后 14237 亿。

★ 影响面（务必区分两类量）：
  ✅ `vol_ratio`（今日/前 5 日均）**不受影响** —— 688 全体被同一个常数
     因子放大，比值不变 ⇒ 状态机 score/state 结论不受影响。
  ❌ `total_amount` 是**绝对值**字段，已失真；`detail_json` 里也存了一份。

★ 只改数值不改口径：把 688 的贡献除回 100，等价于「重算」，
  因为我们知道错因就是固定的 100 倍。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    from backend.data import duck
    con = duck.get_conn()

    print("=" * 72)
    print("修正 market_regime.total_amount")
    print("=" * 72)

    # ---- 1. 用修好的 kline_daily 重算每日成交额 ----
    daily = {str(d): v for d, v in con.execute("""
        select cast(trade_date as date) as d,
               sum(close*volume) as amt
        from kline_daily
        where close > 0 and volume > 0
        group by 1""").fetchall()}

    rows = con.execute(
        "select ts, total_amount, detail_json from market_regime").fetchall()
    changed, skipped = 0, []
    for ts, old, dj in rows:
        key = str(ts)[:10]
        new = daily.get(key)
        if not new:
            skipped.append(key)
            continue
        if abs(new - float(old or 0)) < 1.0:
            continue
        # detail_json 里的副本也要一起改，否则两处不一致
        try:
            obj = json.loads(dj) if dj else {}
        except json.JSONDecodeError:
            obj = {}
        obj["total_amount"] = new
        con.execute(
            "update market_regime set total_amount=?, detail_json=? where ts=?",
            [new, json.dumps(obj, ensure_ascii=False), ts])
        changed += 1

    print(f"更新 {changed}/{len(rows)} 行；跳过（当日无日线）{len(skipped)} 行")
    if skipped:
        print(f"  跳过日期: {sorted(skipped)[:10]}{' ...' if len(skipped) > 10 else ''}")

    # ---- 2. 独立复验 ----
    print("\n【复验】")
    bad = 0
    for ts, amt, dj in con.execute(
            "select ts, total_amount, detail_json from market_regime "
            "order by ts desc limit 8").fetchall():
        d = str(ts)[:10]
        ref = daily.get(d)
        ratio = float(amt) / ref if ref else None
        ok = ratio and 0.95 < ratio < 1.05
        bad += 0 if ok else 1
        dj_amt = (json.loads(dj) or {}).get("total_amount") if dj else None
        consistent = dj_amt and abs(float(dj_amt) - float(amt)) < 1.0
        print(f"  {d} total={float(amt)/1e8:>10,.0f}亿 | "
              f"日线重算={ref/1e8:>10,.0f}亿 | 比值={ratio:.3f} "
              f"{'✅' if ok else '❌'} | detail_json一致={'✅' if consistent else '❌'}")
    print(f"\n结论: {'✅ 全部对齐' if bad == 0 else f'❌ {bad} 行仍不对齐'}")
    if bad:
        print("⚠ 差异原因应是**当日日线覆盖不全**（不是 688 单位问题）")


if __name__ == "__main__":
    main()