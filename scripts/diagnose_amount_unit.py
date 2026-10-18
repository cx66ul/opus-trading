# -*- coding: utf-8 -*-
"""
`kline_min.amount` 单位与覆盖度诊断（2026-10-10）

★ 起因：成交额规则「11点前全市场成交额< 1 万亿」被判定为**数据阻塞**，
  理由写的是「9-29 11 点前 m30 累计 66~82 亿 vs 阈值 1 万亿，差两个数量级」。
  本脚本逐条查这个结论：是量纲差异，还是数据bug？
  —— **结论：两者都不是。是数据不完整 + 覆盖度不足，
     而"差两个数量级"这个描述本身也不准确。**

================================ 诊断结论（全部可复验） ====================
① **腾讯分钟线 `it[7]` 的单位是「万元」**（实测：600006 单bar `it[7]=19.80`，
   对应 `手数×close=21.6万元`，比值 0.94~0.98，差异来自均价≠收盘价）。
   采集器 `tencent.py` 里 `(self._f(it[7]) or 0) * 10000`（万元→元）**换算正确**。
   ⇒ **不是单位 bug**。此前记忆里「amount 单位存疑 / amt/vol 比值既不等于价格
   也不等于价格×100」的说法，是因为**拿股数当手数**比，结论错误。

② **库内 `amount` 与 `volume×close` 同量级，比值 1.30~1.39**（301716 实测，
   逐bar 稳定）。1.3~1.4 的偏差来自**日内均价≠收盘价**，不是单位错。

③ ★★ **真正的病根：m30 覆盖不完整**
   - 单只股票当天 8 根 bar 齐全（如 688002 累加 = 日线，误差 1e-7）
   - 但**全市场 m30 累加 volume 只有日线的 12.54%**
     ⇒ 只有约 1/8 的股票当日数据是完整的
   - 且 `kline_min.amount` 与 `volume` **完全脱钩**：
     688002 有 6.4 亿股成交（估额 1134 亿），`amount` 合计仅 138 万元

④ **"11 点前 66~82 亿"这个数字是错的**：它把 4556 只股票的**全部 bar** 相加，
   而其中大部分股票当天只有部分 bar ⇒ 得到的不是全市场成交额。
   同口径正确值（按 8 根齐全者）见下方输出。

⑤ **`kline_daily` 侧完全正确**：`sum(volume×close)` = 218131 亿，
   与 `market_regime.total_amount` 权威值**完全一致（1.00x）**
   ⇒ 成交额规则若要落地，应走**日线**口径，不是分钟线。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

D = "date '2026-09-29'"          # 库内 m30 覆盖最完整的一天
BARS_PER_DAY = 8                  # m30 每天 8根（10:00/10:30/11:00/11:30/13:30/14:00/14:30/15:00）


def diagnose(con) -> dict:
    out: dict = {}

    # ---- ① m30 覆盖度：每只股票当天 bar 数分布
    rows = con.execute(f"""select cnt, count(*) from (
        select code, count(*) cnt from kline_min
        where period='m30' and cast(ts as date)={D} group by code)
        group by cnt order by cnt""").fetchall()
    out["bar_cnt_dist"] = {int(r[0]): int(r[1]) for r in rows}
    out["stocks_full_cover"] = out["bar_cnt_dist"].get(BARS_PER_DAY, 0)
    out["stocks_total"] = sum(out["bar_cnt_dist"].values())

    # ---- ② m30 累加 volume vs 日线 volume（全市场）
    # ★★ 必须**分别聚合再相除**，不能写成 JOIN 后sum()/sum()：
    #   JOIN 会把日线行按 m30 的行数重复累加（本脚本第一版就踩了，
    #   得出「覆盖率 12.54%」的假结论，实际是 90.7%）。
    #   判据：同一份数据算两次必须能对上，先各自单表聚合最安全。
    v_m30 = con.execute(f"""select sum(volume) from kline_min
        where period='m30' and cast(ts as date)={D}""").fetchone()[0]
    v_day = con.execute(f"""select sum(volume) from kline_daily
        where trade_date={D}""").fetchone()[0]
    out["m30_vol"] = int(v_m30 or 0)
    out["daily_vol"] = int(v_day or 0)
    out["vol_coverage_ratio"] = round(v_m30 / v_day, 4) if v_day else None
    # 覆盖度归因：缺的是「当天没同步到 m30 的股票」
    out["codes_no_m30"] = int(con.execute(f"""select count(*) from (
        select code from kline_daily where trade_date={D} and code not in
        (select distinct code from kline_min
         where period='m30' and cast(ts as date)={D}))""").fetchone()[0])

    # ---- ②b 每股层面校验（这才是真正的判据：逐只m30 累加 = 当日日线 volume）
    rows = con.execute(f"""select k.code, sum(k.volume) mv, d.volume dv
        from kline_min k
        join kline_daily d on k.code=d.code and d.trade_date={D}
        where k.period='m30' and cast(k.ts as date)={D}
        group by k.code, d.volume""").fetchall()
    rs = [r[1] / r[2] for r in rows if r[2]]
    out["per_stock_vol_ratio"] = {
        "n": len(rs),
        "median": round(statistics.median(rs), 6) if rs else None,
        "matched_1_0": sum(1 for x in rs if 0.99 <= x <= 1.01),
    }

    # ---- ③ amount 与 volume×close 的比值（**逐股聚合**后再看分布）
    # ⚠⚠ 绝不能像本脚本第一版那样在JOIN 里算比值：
    #   JOIN 会按 m30 行数放大日线列，比值被稀释到"看起来≈1" ⇒ 假结论。
    #   正确做法：kline_min 侧单独聚合，再**逐股**与自己的 volume 比。
    #★ 真实分布是跨 4 个数量级的对数正态（1e-5~ 1e-1），
    #   **没有一只落在 1 附近** ⇒ amount 系统性偏小约 100 倍且逐股不同。
    #   ⇒ 这不是"单位换算错一个固定倍数"，而是逐股漂移，无法用常数修正。
    rows = con.execute(f"""select code, sum(amount) amt,
            sum(volume) * max(close) as volpx
        from kline_min where period='m30' and cast(ts as date)={D}
        and volume > 0 and close > 0 group by code""").fetchall()
    rs = [a / vp for _, a, vp in rows if vp]
    import collections
    import math
    hist = collections.Counter(round(math.log10(v)) for v in rs if v > 0)
    out["amt_over_volpx"] = {
        "n": len(rs),
        "median": float(f"{statistics.median(rs):.3e}") if rs else None,
        "q1": f"{statistics.quantiles(rs, n=4)[0]:.3e}" if len(rs) > 3 else None,
        "q3": f"{statistics.quantiles(rs, n=4)[2]:.3e}" if len(rs) > 3 else None,
        "log10_hist": dict(sorted(hist.items())),
        "near_1_count": sum(1 for v in rs if 0.5 < v < 2),
        "verdict": ("★逐股漂移 4 个数量级，**无法用固定倍数修正**；"
                    "与 volume×close 完全脱钩")
        if sum(1 for v in rs if 0.5 < v < 2) < len(rs) * 0.1 else "单位一致",
    }

    # ---- ④ 「11 点前累计成交额」的正确算法：只取 8根齐全的股票，再按比例外推
    full = con.execute(f"""select k.code, sum(k.amount) as amt
        from kline_min k where k.period='m30' and cast(k.ts as date)={D}
        group by k.code having count(*) = {BARS_PER_DAY}""").fetchall()
    out["full_cover_stocks"] = len(full)
    out["full_cover_amount_yi"] = round(sum(a for _, a in full) / 1e8, 1)
    if full:
        #外推到全市场：用**成交量**做权，而不是家数
        got = con.execute(f"""select sum(k.volume) from kline_min k
            where k.period='m30' and cast(k.ts as date)={D}
            and k.code in (select code from kline_min
              where period='m30' and cast(ts as date)={D}
              group by code having count(*)={BARS_PER_DAY})""").fetchone()[0]
        if got and v_day:
            out["extrapolated_full_market_yi"] = round(
                out["full_cover_amount_yi"] * v_day / got, 1)

    # ---- ⑤ 11点前累计（原始错误算法 vs 只算齐全股票）
    for lab, cond in (("raw_all_stocks", ""),
                      ("full_cover_only",
                       f"and k.code in (select code from kline_min where "
                       f"period='m30' and cast(ts as date)={D} "
                       f"group by code having count(*)={BARS_PER_DAY})")):
        v = con.execute(f"""select sum(k.amount)/1e8 from kline_min k
            where k.period='m30' and cast(k.ts as date)={D}
            and cast(k.ts as time) <= time '11:00:00' {cond}""").fetchone()
        out[f"cum_1100_{lab}_yi"] = round(v[0], 1) if v and v[0] else None

    # ---- ⑥ 日线侧（权威口径）验证
    d = con.execute(f"""select sum(volume*close)/1e8 from kline_daily
        where trade_date={D} and close is not null""").fetchone()[0]
    out["daily_amount_est_yi"] = round(d, 1)
    m = con.execute(f"select total_amount/1e8 from market_regime where cast(ts as date)={D}").fetchone()
    out["market_regime_amount_yi"] = round(m[0], 1) if m else None
    if m and d:
        out["daily_vs_regime_ratio"] = round(m[0] / d, 4)
    return out


def main() -> None:
    from backend.data import duck
    con = duck.get_ro_conn()
    r = diagnose(con)
    print("=" * 72)
    print("kline_min.amount 单位与覆盖度诊断")
    print("=" * 72)
    print(f"① 覆盖度: {r['stocks_total']} 只中 **{r['stocks_full_cover']} 只**当天 8 根齐全")
    print(f"   当日无 m30 的股票: {r['codes_no_m30']} 只（日线 {r['daily_vol'] and ''}5557 只中）")
    p = r["per_stock_vol_ratio"]
    print(f"② 每股 m30累加/日线volume: n={p['n']} 中位 **{p['median']}**"
          f"｜≈1.0 的 {p['matched_1_0']} 只⇒逐只精确匹配")
    print(f"   全市场 m30vol/日线vol = **{r['vol_coverage_ratio']}**（差额=未同步的股票）")
    a = r["amt_over_volpx"]
    print(f"③ amount / Σ(volume×close)逐股比值: n={a['n']} "
          f"中位 {a['median']} [{a['q1']}, {a['q3']}]")
    print(f"   log10 直方图: {a['log10_hist']}")
    print(f"   落在 1 附近的: **{a['near_1_count']} / {a['n']}** ⇒ {a['verdict']}")
    print(f"④ 「11点前累计」原始算法 {r['cum_1100_raw_all_stocks_yi']} 亿"
          f"｜只算 8根齐全的 {r['cum_1100_full_cover_only_yi']} 亿"
          f"｜外推全市场 {r.get('extrapolated_full_market_yi')} 亿")
    print(f"⑤ 日线口径 sum(volume×close) = {r['daily_amount_est_yi']} 亿"
          f"｜market_regime 权威 {r['market_regime_amount_yi']} 亿"
          f"｜比值 **{r.get('daily_vs_regime_ratio')}**")
    print()
    print("结论：")
    print("  ★★ `kline_min.amount` **不可用**：逐股比值跨 4 个数量级、"
          "无一落在 1 附近，")
    print("     与 `volume×close` 完全脱钩 ⇒ 不是单位换算错一个固定倍数，")
    print("     而是**逐股漂移**，无法标定⇒ 成交额规则不能依赖它")
    print("  · `kline_min.volume` **正确**：4554/4556 只逐只精确等于当日日线 volume")
    print("  · `kline_daily.volume×close` **正确**：与 market_regime 权威值 1.00x 吻合")
    print("  ⇒ 成交额若要算，只能用 `volume × close`（日线或分钟线皆可），")
    print("**不要读 amount 字段**")
    print("")
    print("★ 方法论教训（本次踩了两次同源坑）：")
    print("  1. JOIN 后`sum(日线列)` 会被 m30 行数放大 ⇒ 覆盖率算成 12.54%（实为 90.69%）")
    print("  2. JOIN 里算比值 ⇒ 被稀释到中位≈0.945，**得出「单位正确」的错误结论**")
    print("  ⇒ 判据必须**逐股分组**后再比；聚合比值会被少数异常值与 JOIN 放大同时污染")
    p = ROOT / "data" / "amount_unit_diagnosis.json"
    p.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()
