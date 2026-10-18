#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 板块因子回填（M5 · #43）

把 `board_member_snapshot` × `kline_daily` 接到八因子上，
使`factor_coverage` 从 0.28 提到 ~0.67。

★ 为什么这个脚本存在（而不是当初就写对）
--------------------------------------
`strength.score_boards()` 是纯函数、只对**已有因子列**打分，
它不编造缺失因子（这是优点）。但缺的那几项里**有 4 个完全能用本地
`kline_daily` 算出来**（d2 相对 RS / d3 涨停占比 / d6 量能离散 / d8 rank斜率）。
⇒ 空白不是「拿不到数据」，是「没人把已有数据接进因子」。

★ 三条纪律
  1. **纯函数**：`as_of` 必填注入，禁 `datetime.now()`
  2. **缺失记None**，绝不当 0 或中性 50 分
  3. **样本量随因子落库**：同花顺翻页 ajax 401，每板块只有 20 只，
     这是子集口径而非全量 —— 不写下来就会被后人当全量用

用法：
    python scripts/backfill_board_factors.py --as-of 2026-10-08
    python scripts/backfill_board_factors.py --as-of 2026-10-08 --dry-run
    python scripts/backfill_board_factors.py --report     # 只看现状，不写库
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data import duck                              # noqa: E402
from backend.domain import board_factors as BF            # noqa: E402
from backend.domain import breadth                    # noqa: E402
from backend.domain import strength as S                  # noqa: E402

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | {message}")


def _d(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=CST)


# ══════════════════════════════════════════════════════ 取数
def market_ret5(as_of: datetime, window: int = 5) -> dict[str, float]:
    """全市场每只股票的 `window` 日累计收益（比例），用于算板块**超额**。

    ★ 用窗口函数自取`window` 日前的收盘，不信存储的 pre_close
      （实测 pre_close 只有 93.7% 与 lag(close) 一致）。
    ★ 只保留窗口内**有完整 window+1 个交易日**的票：不足窗口的
      收益不可比，混进来会让「超额」变成噪声。
    """
    con = duck.get_conn()
    day = as_of.strftime("%Y-%m-%d")
    # ★ 窗口结果包一层，**外层WHERE 只能在包层上**，
    #   且子查询必须把 trade_date 一起 select 出来
    #   （早先内层只 select code/ret5，外层写 trade_date=? 直接报
    #    "Referenced column trade_date not found" —— DuckDB 不像
    #    某些引擎那样自动把外层过滤条件推入窗口子查询）
    rows = con.execute(
        """
        WITH h AS (
          SELECT code, trade_date, close,
                 lag(close, {w}) OVER (PARTITION BY code
                                       ORDER BY trade_date) AS prev
          FROM kline_daily WHERE trade_date <= ?
        )
        SELECT code, (close / prev - 1.0) AS ret5
        FROM h
        WHERE trade_date = ? AND prev IS NOT NULL AND prev > 0
        """.format(w=window), [day, day]).fetchall()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def load_board_quotes(as_of: datetime, day_window: int = 6) -> dict[str, list[dict]]:
    """每个板块的成分股行情（含 pre_close / pct_chg / amount / ret5）。

    ★ `pre_close` **在这里用 lag 自取**，不用库里的存储列（见上）。
    ★ `amount` = `volume × close` 自算（库里 amount 全NULL，
      `kline_min.amount` 已废弃不可用）。
    """
    con = duck.get_conn()
    day = as_of.strftime("%Y-%m-%d")
    rows = con.execute(
        """
        WITH k AS (
          SELECT code, trade_date, open, high, low, close, volume,
                 lag(close) OVER (PARTITION BY code ORDER BY trade_date)
                     AS prev,
                 lag(close, 5) OVER (PARTITION BY code ORDER BY trade_date)
                     AS prev5
          FROM kline_daily WHERE trade_date <= ?
        )
        SELECT m.board_code, k.code, k.prev, k.close, k.volume,
               (k.close / k.prev - 1.0) * 100.0 AS pct_chg,
               k.close / k.prev5 - 1.0        AS ret5,
               k.volume * k.close              AS amount
        FROM board_member_snapshot m
        JOIN k ON k.code = m.code
        --★★★ 成分快照必须取「≤ as_of 的**最近一个快照日**」，**不能**简单 `<= day`
        --   `board_member_snapshot.snap_date` 记的是**采集日**，
        --   而采集可能发生在非交易日（实测 2026-10-10 周六采了329 条，
        --   而它的行情属于 10-09）。
        --   ⇒ `WHERE snap_date <= '2026-10-09'` 会把整份快照过滤掉，
        --   **383 个板块一个都取不到成分**，而日志只显示
        --   「有行情的板块 1/383」——看着像数据问题，实为口径写错。
        WHERE m.snap_date = (SELECT max(snap_date) FROM board_member_snapshot
                             WHERE snap_date <= CAST(? AS DATE))
          AND k.trade_date = ?
        """, [day, day, day]).fetchall()

    out: dict[str, list[dict]] = {}
    #★★★ 两套board_code 体系必须在这里归一（实测 2026-10-10）
    #   `board_member_snapshot.board_code` 存**裸 6 位码**（`881136`）
    #   `board_strength.board_code`      存**带前缀**（`THSIND881136`）
    #   ⇒ 直接 `quotes.get(code)` 时**383 个板块全部取不到成分**，
    #     而日志只打「有行情的板块 0/383」——看着像数据缺失，
    #     其实是两套主键口径不一致（典型的"归因错方向"）。
    #
    #   ★ 归一**不靠猜**：快照表根本没有 `board_type` 列
    #     （实测 `PRAGMA table_info` 只有 snap_date/board_code/code/weight），
    #     所以不能写死 `industry`。改用 `board_strength` 里该裸码
    #     实际对应哪个前缀来反查；**两种前缀都不存在就跳过**，
    #     绝不为了凑上覆盖率而挂到错误板块上。
    prefix_of: dict[str, str] = {}
    for raw, ind_n, con_n in con.execute(
            "SELECT DISTINCT m.board_code,"
            " (SELECT count(*) FROM board_strength b"
            "   WHERE b.board_code = 'THSIND' || m.board_code),"
            " (SELECT count(*) FROM board_strength b"
            "   WHERE b.board_code = 'THSCON' || m.board_code)"
            " FROM board_member_snapshot m").fetchall():
        if ind_n and not con_n:
            prefix_of[raw] = "THSIND"
        elif con_n and not ind_n:
            prefix_of[raw] = "THSCON"
        elif ind_n or con_n:
            # ★ 同裸码同时命中两种前缀 ⇒ 无法判定归属，**不猜**
            logger.warning(f"板块裸码 {raw} 同时匹配 THSIND/THSCON，"
                           f"跳过（不猜归属）")
    missing = sorted({r[0] for r in rows} - set(prefix_of))
    if missing:
        logger.warning(f"★ {len(missing)} 个板块裸码在 board_strength 中"
                       f"找不到对应前缀（例：{missing[:3]}）⇒ 这些板块不参与因子回填")

    for r in rows:
        key = prefix_of.get(r[0])
        if key is None:
            continue                    # 归属不明 ⇒ 不挂到任何板块上
        out.setdefault(f"{key}{r[0]}", []).append({
            "code": r[1], "pre_close": r[2], "close": r[3], "volume": r[4],
            "pct_chg": r[5], "ret5": r[6], "amount": r[7],
            "is_st": 0,
        })
    return out


def load_streak_series(as_of: datetime, codes: set[str],
                       lookback: int = 30) -> tuple[list[str], dict[str, dict]]:
    """d4 用：每只成分股的逐日「是否封涨停」+ 交易日轴。

    返回 `(trade_days, {code: {day: bool|None}})`。

    ★★ 涨停判定复用 `breadth.is_limit_up` 严格口径（round(前收×限幅,2)），
      **不重写** —— 项目已吃过「两套口径必然对不上」的亏。
    ★ `pre_close` 用 lag 自取，不信存储列（实测只有 93.7% 一致）。
    ★ 返回的 `trade_days` 是**该窗口内真实有行情的交易日**（而非日历推断），
      因为连板只能在「实际发生交易的日子」上推进；
      某只票在这段里缺行 ⇒ `BF.max_streak` 会因窗口不完整而排除它。
    ★ 30 日窗口对应 `max_streak` 内部的上限，两边必须一致，
      否则会出现「窗口比函数回看更长 ⇒ 多算提前于窗口的连板」。
    """
    con = duck.get_conn()
    day = as_of.strftime("%Y-%m-%d")
    if not codes:
        return [], {}
    # 只取成分票，避免全市场 5500 只 × 30 天的无谓扫描
    ph = ",".join("?" * len(codes))
    rows = con.execute(
        f"""
        WITH k AS (
          SELECT code, trade_date, close,
                 lag(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev
          FROM kline_daily
          WHERE code IN ({ph}) AND trade_date <= ?
        ),
        d AS (SELECT DISTINCT trade_date FROM k)
        SELECT code, trade_date, prev, close FROM k
        ORDER BY code, trade_date
        """, [*sorted(codes), day]).fetchall()

    is_st = {}
    try:
        import sqlite3 as _sq
        from backend.data.sqlite import DB_PATH as SQLITE_PATH
        cn = _sq.connect(str(SQLITE_PATH))
        # ★ `stock` 表的股票代码列叫 **`id`**（实测 PRAGMA 确认），
        #   写 `code` 会抛 no such column ⇒走 except 静默降级成「全按非 ST」，
        #   ST 股的 5% 限幅就按 10% 判了 ⇒ 连板高度偏低却**不报错**。
        for sid, flag in cn.execute(
                "SELECT id, is_st FROM stock WHERE id IN (%s)" % ph,
                sorted(codes)).fetchall():
            is_st[sid] = bool(flag)
        cn.close()
        logger.info(f"d4 读到is_st 标记 {sum(is_st.values())} 只 ST")
    except Exception as e:                      # noqa: BLE001
        # ★ is_st 只是**当前**快照（历史会漂移），
        #   拿不到就退化为按非 ST 处理，**不因此整段失败**，但必须告警。
        logger.warning(f"读 stock.is_st 失败（d4 按非 ST 处理）: {e}")

    # 交易日轴：从行情里取近 lookback 个有数据的日子
    all_days = sorted({str(r[1]) for r in rows})[-lookback:]
    out: dict[str, dict] = {}
    for code, d, prev, close in rows:
        # ★ `d` 是 `datetime.date` 而 `all_days` 是 `str`
        #   ⇒ `d not in all_days` **恒为True** ⇒ 全部continue
        #   ⇒ 「0 只成分票有 K 线」且**不报错**。
        #   这正是「类型不匹配的等值比较会静默清空结果」的典型。
        dkey = str(d)
        if dkey not in all_days:
            continue
        hit = breadth.is_limit_up(code, prev, close, None,
                                 is_st=is_st.get(code, False))
        out.setdefault(code, {})[dkey] = (bool(hit) if hit is not None
                                         else None)
    return all_days, out


def rank_history(board_code: str, as_of: datetime, days: int = 3) -> list:
    """该板块最近 `days` 天的 rank（d8 用）。"""
    con = duck.get_conn()
    day = as_of.strftime("%Y-%m-%d")
    rows = con.execute(
        """
        SELECT CAST(ts AS TIMESTAMP), rank
        FROM board_strength
        WHERE board_code = ? AND rank IS NOT NULL
          AND CAST(ts AS DATE) <= ?
        ORDER BY ts DESC LIMIT ?
        """, [board_code, day, days]).fetchall()
    return list(reversed(rows))


# ══════════════════════════════════════════════════════ 主流程
def backfill(as_of: datetime, dry_run: bool = False) -> dict:
    con = duck.get_conn()
    day = as_of.strftime("%Y-%m-%d")

    # 已有板块快照（以board_strength 当日行为准）
    #★★ 原料列必须**全读**：漏读一列，写回时它就变NULL。
    #   而本脚本是「DELETE 当日 → 重算 → 插回」，下一轮读的原料
    #   就是这一轮写坏的结果 ⇒ **每重跑一次就丢一截**（实测 net_inflow 383→0）。
    snap_rows = con.execute(
        "SELECT board_code, board_name, type, pct_chg, net_inflow, up_ratio,"
        " limit_up_cnt, limit_down_cnt, max_streak, rank_change,"
        " vol_ratio, leader_code, amount "
        "FROM board_strength WHERE CAST(ts AS DATE) = ?", [day]).fetchall()
    if not snap_rows:
        logger.warning(f"board_strength 在 {day} 无行，先跑 sync_board_strength.py")
        return {"ok": False, "reason": "no_board_snapshot"}

    mret = market_ret5(as_of)
    quotes = load_board_quotes(as_of)
    logger.info(f"全市场 5 日收益样本 {len(mret)} 只；"
                f"有行情的板块 {len(quotes)}/{len(snap_rows)} 个")

    # ★★ 必须显式拦住「横截面不可用」的 as_of，而不是让它算出 coverage=0.33
    #   实测 2026-10-10（残缺日，仅 108 只）→ 5 日收益样本 **0 只**
    #   ⇒ 所有因子为 None ⇒ coverage 掉到 0.33（只剩 d1+d5）
    #   而脚本会照常打印「回填成功」——**数字看着正常、没有任何报错**。
    #   判据**复用 data_gate**（不自己数股票，否则会出现两套完备度口径）。
    from backend.domain import data_gate as G

    cov = G.coverage_from_duck(con)
    if not cov.is_usable(str(day)):
        logger.error(
            f"★ 拒绝回填：{day} 横截面不可用"
            f"（{day} 有 {cov.per_day.get(str(day), 0)} 只标的，"
            f"门槛 {cov.threshold}）。最近可用交易日：{cov.last_usable}。"
            f"—— 在残缺日回填会得到 coverage≈0.33 却报「成功」。")
        return {"ok": False, "reason": f"cross_section_unusable: {day}"}

    # ---- d4：逐股连板高度（只需在**有成分的板块**上算）
    #   ★ 全库只有 17 个板块有成分，而成分一共 17×20 只上下⇒
    #   先把 code 收集起来一次批量取K 线，而不是逐板块查 383 次。
    all_codes = {q["code"] for qs in quotes.values() for q in qs
                 if q.get("code")}
    trade_days, streak_raw = load_streak_series(as_of, all_codes)
    logger.info(f"d4 连板轴：{len(trade_days)} 个交易日，"
                f"{len(streak_raw)} 只成分票有 K 线")

    n_market = len(mret)
    if n_market < max(100, int(len(snap_rows))):
        logger.error(
            f"★ 拒绝回填：{day} 的 5 日收益样本只有 {n_market} 只，"
            f"不足板块数 {len(snap_rows)} —— 该日横截面不可用（残缺日）。")
        return {"ok": False,
                "reason": f"cross_section_unusable: 5日样本仅 {n_market} 只"}

    rows_out: list[dict] = []
    for (code, name, typ, pct_chg, inflow, up_ratio,
         lu_cnt, ld_cnt, mx_streak, rank_chg, vol_ratio,
         leader, amount) in snap_rows:
        qs = quotes.get(code, [])
        n = len(qs)

        d2 = BF.rs5_excess(qs, mret)
        d3, n_lu, base_lu = BF.limit_ratio(qs)
        d6 = BF.vol_expansion(qs)
        d7, base_up = BF.up_ratio(qs)
        d8 = BF.rank_slope(rank_history(code, as_of))

        # ── d4：把逐日涨停标记折叠成连板高度序列，再取成分内最大
        qs_streak = []
        for q in qs:
            raw = streak_raw.get(q["code"])
            if not raw:
                continue
            qs_streak.append({
                "code": q["code"],
                "streaks": BF.limit_up_streaks(list(raw.items()), trade_days),
            })
        d4, d4_n = BF.max_streak(qs_streak, day, trade_days)

        # d5_inflow：外部 net_inflow 有值就沿用（这是数据源给的，不是本地算的）
        d5 = None
        if inflow is not None:
            d5 = float(inflow)

        #★★★★ 必须把**原始列**原样带下来，否则写回时会把它们写成 NULL。
        #   `INSERT` 那段用的是 `r.get("net_inflow")` / `r.get("up_ratio")`，
        #   而本 dict 里的键叫 `d5_inflow` / `d7_up_ratio`
        #   ⇒ `.get()` 恒为 None ⇒ 这两列被清空。
        #   症状极其隐蔽：脚本自己 `DELETE` 掉当日的**原料**再重算，
        #   而重算的原料正是它自己上一轮写坏的行 ⇒ **重跑一次丢一截**
        #   （实测 `net_inflow` 383 → 0，coverage 0.82 → 0.67）。
        #   ⇒ 「先删后插 + 从表里读原料」这个结构本身就是不幂等的；
        #     修法是让写回严格取自本次读到的行，绝不r.get 猜键。
        rows_out.append({
            "board_code": code, "board_name": name, "type": typ,
            "d1_pct": pct_chg, "d2_rs5": d2, "d3_limit_ratio": d3,
            "d4_max_streak": d4,   # ★ 本地 K 线可算（见 BF.max_streak）
            "d5_inflow": d5, "d6_vol_ratio": d6, "d7_up_ratio": d7,
            "d8_rank_slope": d8,
            # —— 原始列，1:1 透传（本轮读到什么就写回什么）——
            #★★ 命名冲突要显式处理：`d4_max_streak` 是因子名，而表里的
            #   实体列叫 `max_streak`。若只透传原料 `mx_streak`，
            #   本地算出的 d4 就**只活在 factors_json 里、实体列全空**
            #   （实测 max_streak 383 行全 NULL，而页面读的是实体列）。
            #   ⇒ 本地算得出的因子优先写实体列，原料仅作兜底。
            "net_inflow": inflow, "up_ratio": up_ratio,
            "limit_up_cnt": lu_cnt if lu_cnt is not None else n_lu,
            "limit_down_cnt": ld_cnt,
            "max_streak": d4 if d4 is not None else mx_streak,
            "rank_change": rank_chg,
            "vol_ratio": d6 if d6 is not None else vol_ratio,
            "leader_code": leader,
            "amount": amount,
            "_n_members": n, "_n_lu_base": base_lu, "_n_up_base": base_up,
        })

    # 交给纯函数打分（不重写加权逻辑，避免两套口径）
    scored = S.score_boards(rows_out, as_of)

    # ★ 先统计再 pop：`_n_members` 下面会被 pop 掉，
    #   原先在 stats 里写 `r.get("_n_members")` ⇒ 恒为 0。
    with_members = sum(1 for r in scored if r.get("_n_members"))
    if with_members == 0:
        logger.error("★ 没有任何板块取到成分股行情（board_member_snapshot 与 "
                     "kline_daily 无交集？）—— 若继续写库会得到 coverage=0 的假快照")
        return {"ok": False, "reason": "no_board_quotes"}

    # 附样本量说明
    #★★★ 这里原来写成 `r.pop("_n_members", 0), r.pop("_n_members", 0)`：
    #   同一个 key pop **两次** ⇒ 第二个永远是默认值 0。
    #   后果：`annotate()` 的 members_known 恒为 0，而 `sample_note`
    #   是用 members_known 拼的 ⇒ 落库写着「每板块只能取到首页 0 只」，
    #   **数字自相矛盾却没人报错**（这正是"漂亮数字"的来源）。
    #   ⇒ 只 pop 一次，把值同时用于 sample_n 与 members_known。
    for r in scored:
        members_n = r.pop("_n_members", 0)
        # ★ 留一份不会被 pop 的副本，供dry-run 展示用
        #   （`_n_members` 在这里就被消费掉了，后面再读必然拿不到）
        r["_members"] = members_n
        r.pop("_n_lu_base", None)
        r.pop("_n_up_base", None)
        #★★★ `factors_json` 必须存 `score_boards` 算出的 `r["factors"]`
        #   （raw/percentile/weight/contribution 四件套），
        #   **不能**用 `{k: v for k, v in r.items() if k.startswith("d")}`。
        #   那个 dict 里 d1_pct/d2_rs5… 是**裸数值**，
        #   而 `_factors_of()` / `report()` / 下游读的都是
        #   `{"raw":…, "percentile":…}` 结构 ⇒ 两者类型不兼容，
        #   结果是报告永远 `有=[] 缺=[]`、各因子非空率恒 0，
        #   而 `factor_coverage` 却有值 —— **自相矛盾却没有任何报错**。
        r["factors_json"] = BF.dumps(BF.annotate(
            r["factors"], members_n, members_n))
        r["source"] = "local_backfill_v1"
        # ★ source_complete=False 是**诚实标注**：成分只有 20 只子集
        r["source_complete"] = False

    cov = [r["factor_coverage"] for r in scored if r["factor_coverage"] is not None]
    stats = {
        "n_boards": len(scored),
        "coverage_min": round(min(cov), 3) if cov else 0.0,
        "coverage_max": round(max(cov), 3) if cov else 0.0,
        "coverage_avg": round(sum(cov) / len(cov), 3) if cov else 0.0,
        #★ 必须在上面 pop 之前抓，否则恒为 0（"漂亮数字"）
        "with_members": with_members,
    }
    logger.info(f"回填结果：{json.dumps(stats, ensure_ascii=False)}")

    if dry_run:
        # ★ 优先展示**有成分**的板块。
        #   早先固定取 `scored[:5]`，而列表按 strength 排序，
        #   前 5 个常常恰好是**没有成分股**的（coverage 只有 d1=0.15），
        #   打印出`有=[]缺=[] n=0` ⇒ **看起来像回填完全没生效**，
        #   实际有成分的 17 个板块 coverage 是 0.82。
        #   ★ 注意不能读 `r["_n_members"]`：它在上面循环里已被 pop 掉。
        shown = [r for r in scored if r.get("_members", 0) > 0][:5] \
            or scored[:5]
        for r in shown:
            # ★ 用 _factors_of() 统一解包，别在这里假设 JSON 结构
            #   （早先直接取 f["factors"] 里的 percentile，而 score_boards
            #    输出的因子在**顶层** ⇒ 报'float' object has no attribute 'get'
            #   —— 报错点在 print 分支，但根因是读错了库里的真实结构）
            d = _factors_of(r["factors_json"])
            have = [k for k, v in d.items() if v.get("percentile") is not None]
            miss = [k for k, v in d.items() if v.get("percentile") is None]
            #★ 样本量在 `_meta` 下，不是顶层（早先读 fj.get('sample_n')
            #   恒为 None，报告里n=None 看着像"没数据"，其实是取错层级）
            meta = json.loads(r["factors_json"]).get("_meta", {})
            print(f"  {r['board_name']:12s} cov={r['factor_coverage']:.2f} "
                  f"strength={r['strength']:.2f} "
                  f"n={meta.get('sample_n')} 有={have} 缺={miss}")
        print(f"  （有成分的板块 {sum(1 for r in scored if r.get('_members', 0) > 0)}"
              f"/{len(scored)}；其余仅 d1 有值）")
        return {"ok": True, "dry_run": True, **stats}

    # 写入（先删当日再插，保持一日一快照的不变式）
    # ★★ 本脚本**全程 `get_conn()`**（含读），不碰 `get_ro_conn()`：
    #   同进程内read_only=True 与读写连接不能共存，
    #   「先读后写」的脚本若读用ro、写用rw，必然在切换处抛
    #   `Can't open a connection to same database file with a
    #   different configuration`。DuckDB 单连接本身就支持读写。
    wcon = duck.get_conn()
    wcon.execute("DELETE FROM board_strength WHERE CAST(ts AS DATE) = ?", [day])
    for r in scored:
        wcon.execute(
            """
            INSERT INTO board_strength
              (ts, board_code, board_name, type, strength, rank,
               rank_change, limit_up_cnt, limit_down_cnt, max_streak,
               net_inflow, vol_ratio, up_ratio, leader_code,
               pct_chg, amount, factor_coverage, score_method, factors_json,
               source, source_complete)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [r["ts"], r["board_code"], r["board_name"], r["type"],
             r["strength"], r["rank"],
             r.get("rank_change"), r.get("limit_up_cnt"), r.get("limit_down_cnt"),
             r.get("max_streak"), r.get("net_inflow"), r.get("vol_ratio"),
             r.get("up_ratio"), r.get("leader_code"), r.get("d1_pct"),
             r.get("amount"), r["factor_coverage"], r["score_method"],
             r["factors_json"], r["source"], r["source_complete"]])
    logger.info(f"已写入 board_strength（{day}）：{len(scored)} 个板块")
    return {"ok": True, **stats}


def _factors_of(fj: str | None) -> dict:
    """从 factors_json 里取出 {因子名: {raw,...}}。

    ★★ 实测：`score_boards()` 输出的是 `{**row, ...}`，
      **因子在 JSON 顶层**，不在 `factors` 子键下。
      早先按 `d["factors"]` 读 ⇒ 永远空 dict ⇒ 报告全是 0/50，
      而库内明明有值 —— **报告说「全空」比说「全满」更危险**，
      因为它会让人以为「因子没算」而去重写一遍。
    ★ 两种结构都兼容：顶层是因子 dict，或包在 factors 下。
    """
    if not fj:
        return {}
    d = json.loads(fj)
    if isinstance(d.get("factors"), dict):
        return d["factors"]
    # 顶层就是因子 dict（score_boards 的实际输出）
    return {k: v for k, v in d.items()
            if k.startswith("d") and isinstance(v, dict)
            and ("raw" in v or "percentile" in v)}


def report() -> None:
    """现状报告：每天各因子的非空率。"""
    con = duck.get_conn()
    keys = ["d1_pct", "d2_rs5", "d3_limit_ratio", "d4_max_streak",
            "d5_inflow", "d6_vol_ratio", "d7_up_ratio", "d8_rank_slope"]
    print("\n=== board_strength 各日覆盖（从 factors_json 读）===")
    for (day,) in con.execute(
            "SELECT DISTINCT CAST(ts AS DATE) FROM board_strength "
            "ORDER BY 1 DESC LIMIT 5").fetchall():
        rows = con.execute(
            "SELECT factors_json, factor_coverage FROM board_strength "
            "WHERE CAST(ts AS DATE) = ?", [day]).fetchall()
        if not rows:
            continue
        n = len(rows)
        cnt = {k: 0 for k in keys}
        cov = [float(c) for _, c in rows if c is not None]
        for fj, _ in rows:
            d = _factors_of(fj)
            for k in keys:
                if d.get(k, {}).get("raw") is not None:
                    cnt[k] += 1
        head = f"{day}（{n} 个板块"
        head += f"，coverage 均值 {sum(cov) / len(cov):.3f}" if cov else "，coverage 全空"
        print("\n" + head + "）")
        for k in keys:
            bar = "█" * int(cnt[k] / n * 20) if n else ""
            print(f"  {k:16s} {cnt[k]:>4}/{n:<4} {bar}")


def main() -> int:
    p = argparse.ArgumentParser(description="板块八因子回填（M5）")
    p.add_argument("--as-of", help="交易日 YYYY-MM-DD（必填，铁律二）")
    p.add_argument("--dry-run", action="store_true", help="只算不写")
    p.add_argument("--report", action="store_true", help="只打印覆盖现状")
    a = p.parse_args()

    if a.report:
        report()
        return 0
    if not a.as_of:
        p.error("--as-of 必填（铁律二：不许取系统时间）")

    res = backfill(_d(a.as_of), dry_run=a.dry_run)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())