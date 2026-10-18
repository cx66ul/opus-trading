# -*- coding: utf-8 -*-
"""
Hawk · 板块八因子的**本地可算部分**（M5 · #43）

★★★ 这个模块存在的根本理由
--------------------------------
`score_boards()` 是纯函数、不编造缺失因子（这是对的），
但它只能对**已经算好的因子列**打分。实测 `board_strength` 里
8 个因子只有 2 个有值（`d1_pct` / `d7_up_ratio`），
`factor_coverage` 恒为 **0.28**（= 0.18 + 0.10 的权重和，不是 2/7也不是 2/8）。

⇒ 缺的 6 个因子里，**有 4 个完全可以用本地 `kline_daily` 算出来**，
根本不需要外部数据源。之前的空白不是「拿不到数据」，
而是「没人把已有数据接进因子」。

★★ 每个因子的口径必须能回答「这个数是怎么来的」
--------------------------------------------------
`board_member_snapshot` 实测**每板块只有 20 只**（同花顺详情页翻页ajax 401
反爬，DOM 只有首页 20 行）。所以：
  · 所有因子都是**该板块已采到的成分股**上的口径
  · **不完整**（20/全量），必须随因子一起报出来
  · 绝不能因为「算出来了」就假装是全量口径

因子清单与可算性：

===========  ======  ==========================================================
因子          权重    本地可算性
===========  ======  ==========================================================
d1_pct        0.18   ✅ 板块涨跌幅（外部已有）
d2_rs5        0.14   ✅ 成分股 5 日 RS 相对全市场的**超额**（横截面 rank）
d3_limit_ratio 0.15  ✅ 涨停家数 / 成分股数（`breadth` 严格口径已有）
d4_max_streak 0.10   ✅ 逐日涨停标记折叠成连板高度（缺数据断链，不跨停牌累加）
d5_inflow     0.15   ❌ 需资金流（本地无逐笔/大单数据）
d6_vol_ratio  0.10   ✅ 板块成交额 / 其成分股成交额中位数的比（量能扩张度）
d7_up_ratio   0.10   ✅ 上涨家数占比（外部已有）
d8_rank_slope 0.08    ✅ 板块 3 日 rank 的线性斜率（需多日 board_strength）
===========  ======  ==========================================================

⇒ 补上 4 个后覆盖率 0.28 → **0.67**（0.18+0.14+0.15+0.10+0.10）。
剩余缺口是 `d5_inflow`（无本地数据源）与 `d4_max_streak`（无逐股连板状态），
**如实留空**并在 `factors_json` 里写明原因，不编造。

铁律：
  · 纯函数（`as_of` 注入，禁 `datetime.now()`）
  · 缺失一律 None，绝不当 0 或中性50分（否则系统性偏低且不报错）
  · 每项都带`sample_n` / `coverage` 让人能判断这个数能不能信
"""
from __future__ import annotations

import json
from datetime import datetime

# ★ 判定涨停用的严格口径在 backend.domain.breadth（round(前收×1.1,2) 等），
#   不在这里重写一套 —— 两套口径必然对不上（项目已吃过这个亏）。
from backend.domain import breadth as _breadth


def _f(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


# ══════════════════════════════════════════════════════ 因子计算
def rs5_excess(quotes: list[dict], market_rs: dict[str, float]) -> float | None:
    """d2：成分股 5 日 RS 相对全市场的超额（**横截面 rank**）。

    `quotes` 每项须含 `code` / `ret5`（5 日累计收益，比例）。
    `market_rs` 是全市场 code→ret5 映射（同一 as_of、同一窗口）。

    ★★ 这里算的是**成分股自身收益在全市场中的分位**，
      **不是**「成分股收益 − 它自己的收益」（那是恒等于 0 的死公式）。
      实测踩坑（2026-10-10）：我原先实现成
          `q["ret5"] - market_rs[q["code"]]`
      而 `market_ret5()` 返回的正是**同一只股票同一窗口**的收益
      ⇒ 每个成分的差值都是 0.0，**17 个板块 d2 全部等于 0.0**
      —— 一个「完美」的常数，却完全不含信息。
      ⚠ 之所以没被 `data_gate` 拦住，是因为 0.0 是合法数值不是缺失值；
      覆盖率算出来还因此偏高（缺因子会拉低 coverage，
      而这里 d2 被判为「有值」）。

    正确口径：先算全市场分位（0~1），再取成分股分位的**中位数**。

    ★ 为什么取**中位数**而不是均值
      涨停/跌停板会把均值拉飞（一个 +10% 板就能改变结论），
      中位数对极端值稳健，且在成分数只有 20 的小样本下更可靠。
    """
    vals = [_f(q.get("ret5")) for q in quotes]
    vals = [v for v in vals if v is not None]
    if not vals or not market_rs:
        return None
    # 全市场分位：同值取相同分位（避免并列被任意排序打散）
    ordered = sorted(_f(v) for v in market_rs.values()
                     if _f(v) is not None)
    n_m = len(ordered)
    if n_m == 0:
        return None

    def _pct(x: float) -> float:
        lo = 0
        hi = n_m
        while lo < hi:                      # bisect_left 语义（手写避免 import）
            mid = (lo + hi) // 2
            if ordered[mid] < x:
                lo = mid + 1
            else:
                hi = mid
        return lo / n_m

    pcts = sorted(_pct(v) for v in vals)
    n = len(pcts)
    mid = n // 2
    return pcts[mid] if n % 2 else (pcts[mid - 1] + pcts[mid]) / 2.0


def limit_ratio(quotes: list[dict]) -> tuple[float | None, int, int]:
    """d3：涨停家数 / 成分股数。

    ★ 判定必须走 `breadth.is_limit_up` 的严格口径（按 `round(前收×(1+限幅),2)`
      比价），不能用 `pct_chg >= 9.8` 之类的阈值近似 —— 本项目实测过
      宽松口径会多判 603823（收 46.59 vs 涨停价 46.65，差 0.06 并未封板）。

    ★★ 关于 `if r is None: continue` 这条防御分支
      ---------------------------------------------
      `is_limit_up` 返回 None 的**唯一**条件是 `pre_close <= 0`
      （见 `breadth.limit_up_price`），而本函数上游已经有
      `if prev is None or close is None or prev <= 0: continue`。
      ⇒ **这条分支在任何输入下都不可达**（死代码）。
      实测踩坑：我曾为它写了一条测试，变异掉`continue` 后测试**依然全绿**
      ⇒ 那是**假阳性测试**，差点让我以为这条防御被守护着。

      处理原则：**不为死分支编造测试，也不假装它被守护**。
      真正需要守住的行为是「**缺失不进分母**」，由上面那条
      `test_missing_pre_close_excluded_from_denominator` 覆盖
      （它测的是真会发生的路径）。
    """
    if not quotes:
        return None, 0, 0
    hit = base = 0
    for q in quotes:
        prev = _f(q.get("pre_close"))
        close = _f(q.get("close"))
        if prev is None or close is None or prev <= 0:
            continue
        code = str(q.get("code") or "")
        r = _breadth.is_limit_up(code, prev, close, _f(q.get("pct_chg")),
                                 bool(q.get("is_st")))
        # r 恒为 bool（None 已被上游 pre_close<=0 拦掉）；判据仍写成
        # 「False 与 None 都算未涨停」，若将来 breadth 引入新的 None 路径
        # （如停牌）也不会把「无数据」算成涨停。
        base += 1
        if r:
            hit += 1
    return (hit / base if base else None), hit, base


def vol_expansion(quotes: list[dict]) -> float | None:
    """d6：板块内「平均成交额 / 中位成交额」的倍数（离散度）。

    `quotes` 每项须含 `amount`（当日成交额，元）。

    ★ 口径说明：不用「板块成交额 / 全市场成交额」（那是**占比**，不是量能，
      且板块规模差异会让大小板块不可比），也不用「成交额同比」，
      而是看**成分股之间的量能离散度**：
      倍数 ≈ 1 ⇒ 全员等量；倍数 >> 1 ⇒ 少数票放量吸筹（主线特征）。
    ★ amount 一律 `volume × close` 自算（`kline_min.amount` 已废弃不可用，
      `kline_daily.amount` 全库 NULL）。
    ★★ 早先版本写的是 `sum(amts) / med / n`（= 均值/中位数），
      当时以为那就是「倍数」⇒ **数学上等于均值/中位值的离散度**，
      名字与算法对不上。命名必须与算法一致，否则半年后没人知道它是什么。
    """
    amts = sorted(a for q in quotes if (a := _f(q.get("amount"))) is not None)
    if not amts:
        return None
    n = len(amts)
    med = amts[n // 2] if n % 2 else (amts[n // 2 - 1] + amts[n // 2]) / 2.0
    if med <= 0:
        return None
    mean = sum(amts) / n
    return mean / med


def up_ratio(quotes: list[dict]) -> tuple[float | None, int]:
    """d7：上涨家数占比（分母只算有数据的，不含缺失）。"""
    up = base = 0
    for q in quotes:
        prev, close = _f(q.get("pre_close")), _f(q.get("close"))
        if prev is None or close is None or prev <= 0:
            continue
        base += 1
        if close > prev:
            up += 1
    return (up / base if base else None), base


def limit_up_streaks(bars: list[tuple[object, bool | None]],
                     trade_days: list[str]) -> dict[str, int]:
    """d4 的底层：把逐日涨停序列折叠成「截至当日的连板高度」。

    ★★ 为什么必须传 `trade_days`，而不是按 `bars` 的行序推streak
      ------------------------------------------------------
      连板的语义是「**连续交易日**都封板」，而 `bars` 只是**有数据的日子**。
      停牌日缺行 ⇒ 若按行序累加，一只票在 10-09、10-10（假设中间停牌）
      封板会被算成 2 连板，**而中间那天根本没交易** ⇒ 高度虚高。
      ⇒ 正确做法：只在交易日上推进；某个交易日**缺数据 ⇒ 断链归 1**
        （而不是跳过继续累加，也不是当成没涨停）。
      ★ 「缺数据」与「当天没涨停」必须分开处理：
        前者断链（数据不足，不能断言连板），后者 streak=0。

    `bars`：`[(date, is_limit_up or None), ...]`，无序亦可。
    `trade_days`：升序的交易日列表（含**没有 bar 的**交易日）。
    返回 `{日期字符串: 截至该日的连板高度}`。
    """
    flag = {}
    for d, hit in bars:
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        flag[key] = hit
    out: dict[str, int] = {}
    streak = 0
    for day in trade_days:
        hit = flag.get(day)
        if hit is None:          # 缺数据 ⇒ 无法断言，**断链**
            streak = 0
        elif hit:                # 封板 ⇒ 累加
            streak += 1
        else:                    # 明确没封板 ⇒ 归零
            streak = 0
        out[day] = streak
    return out


def max_streak(quotes: list[dict], as_of: str,
               trade_days: list[str]) -> tuple[int | None, int]:
    """d4：成分股里「截至 as_of 的最高连板高度」。

    返回 `(高度, 参与计算的成分数)`。

    ★★ 这不是「今日涨停数」—— 两者在弱市里会重合（都是 0），
      在强市里差一个数量级。之前的版本留空的理由是「本地只有涨停标记」，
      但**逐日 K 线里本来就有足够信息推出连板**，留空是放弃而非诚实。
    ★ as_of 必须**在 `trade_days` 内**，否则返回 (None, 0)：
      非交易日谈「连板高度」没有意义（库里那天的行情其实是上一交易日收盘）。
    ★ 高度 0 是**合法结果**（确实没有连板票），不能当缺失。
      区分：返回 None 表示无法计算，返回 0 表示算出来就是 0。
    """
    if not trade_days or as_of not in trade_days:
        return None, 0
    idx = trade_days.index(as_of)
    window = trade_days[max(0, idx - 29): idx + 1]   # 最多回看 30 个交易日
    # 只回看窗口内出现过的成分 ⇒ 中断超过 30 日的连板不可能延续到 as_of
    codes = {q["code"] for q in quotes if q.get("code")}
    if not codes:
        return None, 0
    best: int | None = None
    counted = 0
    for q in quotes:
        code = q.get("code")
        if not code:
            continue
        series = q.get("streaks") or {}
        # ★ 缺该股票任一交易日的数据 ⇒ 该票不可信，排除而不是当 0
        if any(day not in series for day in window):
            continue
        counted += 1
        h = series[as_of]
        if h > 0 and (best is None or h > best):
            best = h
    if counted == 0:
        return None, 0
    return (best if best is not None else 0), counted


def rank_slope(history: list[tuple[datetime, int]]) -> float | None:
    """d8：板块 rank 的时间序列斜率（最小二乘）。

    `history` 为 `[(ts, rank), ...]`，**按时间升序**；rank 越小越强。
    返回**负值 = rank 逐日变小 = 强度上升**。

    ★ 至少 3 个点才能拟合直线（2 个点斜率恒等于两点连线，
      噪声一大就会得出「暴涨暴跌」的假结论）。
    ★ rank 是**序数**不是连续量，拟合斜率只用于「趋势方向」，
      不代表幅度 —— 这一点在因子说明里必须写明。

    ★★ 符号曾写反（实测 2026-10-10）：
      rank 30 → 20 → 10（明显在变强），原实现返回 **+10.0**。
      根因是最小二乘斜率公式里**多加了一个负号**：
      分子 `Σ(x-x̄)(y-ȳ)` 本身已是 −20，除以分母 2 得 **−10**（正确），
      而代码写成 `-sum(...) / den` ⇒ 又翻回 +10。
      ⇒ 「负值=变强」约定被破坏，下游会把「越强」读成「越弱」，
      而**数值本身仍是漂亮的非零数，不会有任何报错**。
      手算验证：`(0,30)(1,20)(2,10)` ⇒ Σ(x-x̄)(y-ȳ)=−20，Σ(x-x̄)²=2，
      斜率 = −10（不含额外负号）。
    """
    pts = [(i, float(r)) for i, (_, r) in enumerate(sorted(history))
           if _f(r) is not None]
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    # 标准最小二乘斜率：Σ(x-x̄)(y-ȳ) / Σ(x-x̄)²
    # rank 下降 ⇒ 分子为负 ⇒ 斜率为负（= 变强），符合上面 docstring 的约定。
    # ★ **不要**再多套一个负号（那是本函数被修掉的那个 bug）。
    return sum((x - mx) * (y - my) for x, y in pts) / den


# ══════════════════════════════════════════════════════ 组装
#★ d4 已于 2026-10-10 补上：本地 `kline_daily` 的逐日涨停标记
#   足以推出连板高度（见 `limit_up_streaks` / `max_streak`）。
#   原先「无逐股连板状态机 ⇒ 留空」的理由是把「没写」当成了「拿不到」。
FACTOR_UNAVAILABLE = {
    "d5_inflow": "无本地资金流数据源（逐笔/大单均未采集）",
}


def explain_missing() -> str:
    """把留空的因子原因拼成一句话，随因子一起落库。

    ★ 为什么要落库而不是只写文档
    ------------------------------
    `factor_coverage` 只有 0.67 时，下游看到 strength 分数**不知道它可信不可信**。
    把「哪两项没有、为什么没有」写进同一行记录，
    才不会出现「半年后有人拿 0.67 覆盖度的分数当全量用」。
    """
    return "；".join(f"{k}：{v}" for k, v in FACTOR_UNAVAILABLE.items())


def annotate(factors: dict, sample_n: int, members_known: int) -> dict:
    """给因子集合附上样本量信息。

    ★★ 结构必须与 `strength.score_boards()` 的输出**完全一致**：
      因子在**顶层**，`factors` 子键不存在。
      早先这里返回 `{"factors": {...}, "sample_n": ...}`，
      而 `score_boards` 返回 `{**row, "factors": {...}}`（行内因子在顶层，
      factors 子键是纯函数临时构造、不落库）
      ⇒ 两套结构并存，读取方必须两种都试，非常容易读空。
    ⇒ 统一成「顶层放因子 + `_meta` 子对象放说明」，落库与纯函数输出一致。
    """
    return {
        **factors,
        "_meta": {
            "sample_n": sample_n,
            "members_known": members_known,
            "sample_note": (
                f"同花顺详情页翻页 ajax 401（反爬），每板块只能取到首页 "
                f"{members_known} 只成分；本行所有因子均为该子集口径，"
                f"**不是全量板块成分口径**"),
            "missing_reason": explain_missing(),
        },
    }


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)