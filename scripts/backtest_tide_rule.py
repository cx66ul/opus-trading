# -*- coding: utf-8 -*-
"""
R_TIDE_CLEAR_30 的**真回测**：退潮期亏钱效应面积 >30% → 后续市场表现

★ 为什么这条规则不能做「逐股买卖」回测
--------------------------------------
`market_state` 与 `loss_area_ratio` 都是**市场级**因子（同一天全市场同值），
而 M10 的回测契约是「按 code 逐日滑窗求值」——
⇒ 那样会得到 5400 个重复的相同输入，回测出的是**同一个数字 ×5400**，
  样本量虚高、且没有任何统计意义（伪重复 pseudo-replication）。
⇒ 正确形式是**事件研究（event study）**：以「触发日」为样本单位，
  看触发后市场（前瞻收益）如何。

★ 这也解释了记忆里那条「同 kind 下强弱档必须分开」的同类问题：
  样本的**独立性**比样本量更重要。

★ 前视偏差声明
--------------
触发条件用的是**当日收盘**的涨跌停家数与当日市场状态，
而博主原话是盘中判断（"亏钱效应面积大于30%就清仓"）。
⇒ 严格实盘口径应是**次日开盘**执行。这里用「触发日收盘 → 后N 日收盘」
  作为前瞻收益的**测量口径**，明确标注它测的是"事后看跌了没有"，
  不等于"按这个信号能赚到什么"。若要算实盘可得收益，
  必须另跑「次日开盘买入」口径，两者差异就是执行滑点+隔夜跳空。
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
HORIZONS = (1, 3, 5, 10)
OVER = 0.30
STATES = ("ebb", "frozen")


def _st_set(con) -> set[str]:
    """真正权威的 ST 名单（`stock.is_st`）。

    ★ 修正上一版的错误注释：库里**有** `stock.is_st`（5571 行 / 202 只为 1），
      之前写"库内没有 ST 标志字段"是错的（那是 DuckDB 行情侧没有）。
    ⚠ 但它是**当前快照**，历史 ST 会漂移（当年是 ST、现在摘帽）。
      ⇒ 下面同时用「收盘≈round(前收×0.95,2)」反推做对照，
        两者差异必须报出来，而不是只报其中一个。
    """
    import sqlite3
    c = sqlite3.connect(str(ROOT / "data" / "hawk.sqlite"))
    try:
        return {r[0] for r in c.execute("SELECT id FROM stock WHERE is_st = 1")}
    finally:
        c.close()


def build_days(con) -> list[dict]:
    """逐交易日算：严格口径涨停/跌停家数 + 有效样本 + 亏钱效应面积。"""
    from backend.domain.breadth import (
        DENOM_LIMIT_POOL, is_limit_down, is_limit_up, loss_area_ratio,
    )
    st_true = _st_set(con)
    days = [r[0] for r in con.execute(
        "select distinct trade_date from kline_daily "
        "where close is not null "
        "and trade_date >= date '2025-10-01' order by 1").fetchall()]
    out = []
    for dt in days:
        #★★ `pre_close` **用 lag(close) 自取**，不信库里的存储列。
        #   实测该列只有 93.7% 与 lag(close) 一致
        #   ⇒ 剩下 6.3% 会让涨停价算错 ⇒ 涨停/跌停家数偏
        #   ⇒ 亏钱效应面积偏 ⇒ 而这条规则正好拿面积当阈值。
        #   且它**不会报错**：偏掉的 6% 只是让数字变「略微不对」。
        rows = con.execute(
            # ★ `trade_date` **必须一起 select 出来**：
            #   窗口子查询不 select 它，外层 WHERE 就报
            #   "Referenced column trade_date not found"
            #   （DuckDB 不像某些引擎那样把外层过滤条件推入窗口子查询）
            "with k as ("
            "  select code, trade_date, close, pct_chg,"
            "         lag(close) over (partition by code"
            "           order by trade_date) as prev"
            "  from kline_daily where trade_date <= ?"
            ") select code, prev, close, pct_chg from k "
            "where trade_date = ? and prev is not null and prev > 0",
            [dt, dt]).fetchall()
        ld = lu = ld_incl_st = lu_incl_st = 0
        for code, pre, close, pct in rows:
            st_guess = abs(close - round(pre * 0.95, 2)) < 0.005
            # 口径 A（主口径）：ST 用维表名单；口径 B（对照）：用反推
            for is_st, tag in ((code in st_true, "tbl"), (st_guess, "guess")):
                up = is_limit_up(code, pre, close, pct, is_st=is_st)
                dn = is_limit_down(code, pre, close, pct, is_st=is_st)
                if up is True and not is_st:
                    if tag == "tbl":
                        lu += 1
                    else:
                        lu_incl_st += 1
                if dn is True:
                    if tag == "tbl":
                        ld += 1
                    else:
                        ld_incl_st += 1
        total = len(rows)
        r = loss_area_ratio(lu, ld, total, denom=DENOM_LIMIT_POOL)
        rg = loss_area_ratio(lu_incl_st, ld_incl_st, total,
                             denom=DENOM_LIMIT_POOL)
        out.append({
            "date": str(dt), "sample": total,
            "limit_up": lu, "limit_down": ld,
            "ratio": r, "ratio_pct": None if r is None else round(100 * r, 2),
            # 对照口径
            "lu_guess": lu_incl_st, "ld_guess": ld_incl_st,
            "ratio_pct_guess": None if rg is None else round(100 * rg, 2),
        })
    return out


def attach_regime(con, days: list[dict]) -> list[dict]:
    """挂上当日市场状态（`market_regime.ts` 归一到 15:00）。"""
    rows = con.execute(
        "select cast(ts as date) as d, state from market_regime").fetchall()
    mp = {str(r[0]): r[1] for r in rows}
    hit = 0
    for x in days:
        x["state"] = mp.get(x["date"], "")
        x["over30"] = (x["ratio"] is not None and x["ratio"] > OVER)
        x["trigger"] = x["over30"] and x["state"] in STATES
        hit += bool(x["state"])
    for x in days:
        x["_regime_hit"] = hit
    return days


def market_forward(con, dates: list[str]) -> dict[str, dict]:
    """每个交易日的等权平均涨跌幅（市场前瞻收益的测量口径）。"""
    out = {}
    for dt in dates:
        r = con.execute(
            "select avg(pct_chg) from kline_daily "
            "where trade_date = ? and pct_chg is not null", [dt]).fetchone()
        out[str(dt)] = None if not r or r[0] is None else float(r[0])
    return out


def dose_response(con, days: list[dict], dates: list[str],
                  fwd_chg: dict, h: int = 5) -> dict:
    """★ 剂量-反应检验：亏钱效应越高，后续收益是否越差？

    ★★ 这是判断这条规则**有没有方向性**的关键检验。
      只看「阈值上下两组对比」会被**阈值选择**绑架：
      任何阈值都能切出「触发组比基线好/差」的结果。
      真正能证伪的是**单调性**：
        若ratio 从低到高分 5 档，前瞻收益**不随之上移**
        ⇒ 说明「>30%」这个数字是任选的，规则没有方向性。
    """
    import bisect
    buckets = [(0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.45), (0.45, 1.01)]
    rows = []
    for lo, hi in buckets:
        sel = [x for x in days if x["ratio"] is not None and lo <= x["ratio"] < hi]
        vals = []
        for x in sel:
            i = dates.index(x["date"])
            if i + h >= len(dates):
                continue
            seq = [fwd_chg[dates[j]] for j in range(i + 1, i + h + 1)]
            if any(v is None for v in seq):
                continue
            v = 1.0
            for s in seq:
                v *= (1 + s / 100.0)
            vals.append((v - 1) * 100)
        rows.append({
            "band": f"[{lo:.0%},{hi:.0%})", "n": len(vals),
            "avg_pct": round(sum(vals) / len(vals), 3) if vals else None,
            "median_pct": round(statistics.median(vals), 3) if vals else None,
        })
    # 单调性：档位递增时 avg 是否单调下降（允许最多 1 次非降，容忍噪声）
    avgs = [r["avg_pct"] for r in rows if r["avg_pct"] is not None]
    drops = sum(1 for i in range(len(avgs) - 1) if avgs[i + 1] < avgs[i])
    return {"h": h, "bands": rows,
            "monotone_decreasing": drops >= len(avgs) - 1,
            "drops": f"{drops}/{len(avgs) - 1}"}


def contemporaneous(days: list[dict], dates: list[str],
                    avg_chg: dict) -> dict:
    """★ 同期关系：亏钱效应比例与**当日**涨跌幅。

    ★★★ 这是本次回测最关键的发现，必须与前瞻并列呈现：
      比例越高 ⇒ 当日越差（完美单调，触发日 22/22 全跌）；
      但**前瞻**收益反而为正（h5 超额 +1.12pp）。
      ⇒ 该指标是**同步/滞后描述**，不是**预测信号**。
      博主原话是盘中判断（当下亏钱效应大 → 清仓），
      作为**盘中风控提醒**成立；
      作为「收盘后据此预测明日」的模型则**方向相反**，不能那样用。
    ⚠ 不要为了让前瞻为负去调阈值/换窗口 —— 那是在参数网格里挑好看的。
    """
    bands = [(0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.45), (0.45, 1.01)]
    rows = []
    for lo, hi in bands:
        sel = [x for x in days
               if x["ratio"] is not None and lo <= x["ratio"] < hi
               and avg_chg.get(x["date"]) is not None]
        if not sel:
            continue
        vs = [avg_chg[x["date"]] for x in sel]
        rows.append({"band": f"[{lo:.0%},{hi:.0%})", "n": len(vs),
                     "same_day_avg_pct": round(sum(vs) / len(vs), 3)})
    av = [r["same_day_avg_pct"] for r in rows]
    tr = [x for x in days if x["trigger"] and avg_chg.get(x["date"]) is not None]
    bs = [avg_chg[x["date"]] for x in days
          if not x["trigger"] and avg_chg.get(x["date"]) is not None]
    return {
        "bands": rows,
        "monotone_decreasing": all(av[i + 1] < av[i] for i in range(len(av) - 1)),
        "trigger_same_day_avg_pct": round(
            sum(avg_chg[x['date']] for x in tr) / len(tr), 3) if tr else None,
        "base_same_day_avg_pct": round(sum(bs) / len(bs), 3) if bs else None,
        "trigger_down_days": sum(1 for x in tr if avg_chg[x["date"]] < 0),
        "trigger_n": len(tr),
    }


def run(horizons=HORIZONS) -> dict:
    from backend.data import duck
    con = duck.get_ro_conn()
    days = build_days(con)
    days = attach_regime(con, days)
    dates = [x["date"] for x in days]
    fwd_chg = market_forward(con, dates)
    avg_chg = fwd_chg        # 同一份数据：单日等权涨跌幅，前瞻收益由它复合而成
    idx = {d: i for i, d in enumerate(dates)}

    trig = [x for x in days if x["trigger"]]
    base = [x for x in days if not x["trigger"]]
    res = {
        "days_total": len(days),
        "days_with_regime": sum(1 for x in days if x["state"]),
        "over30_days": sum(1 for x in days if x["over30"]),
        "trigger_days": len(trig),
        "state_dist": {},
        "over30_by_state": {},
    }
    for x in days:
        res["state_dist"][x["state"] or "(无)"] = \
            res["state_dist"].get(x["state"] or "(无)", 0) + 1
        if x["over30"]:
            k = x["state"] or "(无)"
            res["over30_by_state"][k] = res["over30_by_state"].get(k, 0) + 1

    # 阈值敏感性：报一串，不报单点（只报一个 = 在网格里挑好看的）
    res["threshold_sensitivity"] = []
    for ov in (0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        n = sum(1 for x in days
                if x["ratio"] is not None and x["ratio"] > ov
                and x["state"] in STATES)
        res["threshold_sensitivity"].append({"over": ov, "days": n})

    if not trig:
        res["verdict"] = "no_trigger"
        return res

    # 前瞻收益：触发日之后 h 个交易日的等权累计涨跌幅
    def fwd(sel, h):
        vals = []
        for x in sel:
            i = idx[x["date"]]
            if i + h >= len(fwd_chg):
                continue
            seq = [fwd_chg[dates[j]] for j in range(i + 1, i + h + 1)]
            if any(v is None for v in seq):
                continue
            v = 1.0
            for s in seq:
                v *= (1 + s / 100.0)
            vals.append((x["date"], (v - 1) * 100))
        return vals

    res["forward"] = {}
    for h in horizons:
        t = sorted(fwd(trig, h))
        b = sorted(fwd(base, h))
        if not t:
            continue
        tv = [r for _, r in t]
        bv = [r for _, r in b] or [0.0]
        # ★ 按时间切两段看符号稳定性（事件样本少，全样本报一个数不可信）
        seg = []
        if len(tv) >= 4:
            q = len(tv) // 2
            for k, ch in enumerate((tv[:q], tv[q:])):
                seg.append({"seg": f"H{k + 1}", "n": len(ch),
                            "avg_pct": round(sum(ch) / len(ch), 3)})
        res["forward"][f"h{h}"] = {
            "n": len(tv),
            "trigger_avg_pct": round(sum(tv) / len(tv), 3),
            "trigger_median_pct": round(statistics.median(tv), 3),
            "trigger_winrate_pct": round(100 * sum(1 for v in tv if v > 0)
                                         / len(tv), 2),
            "base_avg_pct": round(sum(bv) / len(bv), 3),
            # ★ 判edge 看**超额**不看绝对收益（大盘在跌时绝对收益天然不稳）
            "excess_pct": round(sum(tv) / len(tv) - sum(bv) / len(bv), 3),
            "segments": seg,
            "worst": min(tv), "best": max(tv),
        }

    res["dose_response"] = dose_response(con, days, dates, fwd_chg, h=5)
    res["contemporaneous"] = contemporaneous(days, dates, avg_chg)

    # 触发日的原始明细（可人工核对）
    res["trigger_detail"] = [
        {"date": x["date"], "state": x["state"],
         "ratio_pct": x["ratio_pct"], "ratio_pct_guess": x["ratio_pct_guess"],
         "lu": x["limit_up"], "ld": x["limit_down"], "sample": x["sample"]}
        for x in trig
    ]
    return res


if __name__ == "__main__":
    r = run()
    print("=" * 70)
    print("R_TIDE_CLEAR_30 事件研究（市场级规则 ⇒ 不能逐股回测）")
    print("=" * 70)
    print(f"交易日 {r['days_total']}｜有状态 {r['days_with_regime']}")
    print(f"状态分布: {r['state_dist']}")
    print(f">30% 的天数: {r['over30_days']}"
          f"｜其中状态分布: {r['over30_by_state']}")
    print(f"★ 同时满足 state∈{STATES} 且 >30% 的**触发日**: {r['trigger_days']}")
    print("\n阈值敏感性（同时满足两个条件的天数）:")
    for t in r["threshold_sensitivity"]:
        print(f"  >{t['over']:.0%}: {t['days']} 天")
    if r["trigger_days"]:
        print("\n前瞻收益（等权，触发日收盘起算）:")
        for h, v in r["forward"].items():
            print(f"  {h}: n={v['n']} 触发均 {v['trigger_avg_pct']:+.2f}% "
                  f"| 基线 {v['base_avg_pct']:+.2f}% "
                  f"| **超额 {v['excess_pct']:+.2f}pp** "
                  f"| 胜率 {v['trigger_winrate_pct']}%")
            if v["segments"]:
                print("分段: " + " ".join(
                    f"{s['seg']}={s['avg_pct']:+.2f}%" for s in v["segments"]))
    print("\n★ 同期关系（比例 vs **当日**涨幅）:")
    ct = r.get("contemporaneous")
    if ct:
        for b in ct["bands"]:
            print(f"  {b['band']:>12}  n={b['n']:<4} 当日均 {b['same_day_avg_pct']:+.2f}%")
        print(f"  单调下降: {ct['monotone_decreasing']}")
        print(f"  触发日当日均 {ct['trigger_same_day_avg_pct']:+.2f}% vs "
              f"基线 {ct['base_same_day_avg_pct']:+.2f}%"
              f"｜当日下跌 {ct['trigger_down_days']}/{ct['trigger_n']} 天")

    print("\n★ 剂量-反应（h=5，按亏钱效应分档，看后续收益是否单调下降）:")
    dr = r.get("dose_response")
    if dr:
        for b in dr["bands"]:
            av = "n/a" if b["avg_pct"] is None else f"{b['avg_pct']:+.2f}%"
            md = "n/a" if b["median_pct"] is None else f"{b['median_pct']:+.2f}%"
            print(f"  {b['band']:>12}  n={b['n']:<4} 均 {av:>8} 中位 {md:>8}")
        print(f"  单调下降: {dr['monotone_decreasing']} ({dr['drops']})")

    out = ROOT / "data" / "tide_rule_event_study.json"
    out.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {out}")

    # ★★★ 结论必须写在产物里，不能只留在stdout。
    #   D13 立场：系统不自动卖出 ⇒ 这条规则只能作为**提醒**，
    #   且必须同时说明它是同步指标而非预测信号。
    ct, dr = r.get("contemporaneous"), r.get("dose_response")
    if ct and dr:
        print("\n" + "=" * 70)
        print("结论")
        print("=" * 70)
        print(f"  同期关系单调下降: {ct['monotone_decreasing']}"
              f"（比例越高，当日越差 ⇒ 口径正确）")
        print(f"  前瞻剂量-反应单调: {dr['monotone_decreasing']}"
              f"（比例越高，后续越差 ⇒ 未成立）")
        if ct["monotone_decreasing"] and not dr["monotone_decreasing"]:
            print("  ⇒ **该指标是同步/滞后描述，不是预测信号**。")
            print("    合法用法：盘中实时观察，亏钱效应扩散时**提醒**收缩风险。")
            print("    非法用法：收盘后据此预测次日方向（实测方向相反）。")
            print("⚠ 未上线：evidence 未听校（speech_verified=False），"
                  "且语义定位已澄清为『提醒』而非『信号』。"
                  "禁止为了让它看起来能用而去调阈值/换窗口。")
