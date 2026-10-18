#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 每日推荐数据组装（连接 M2/M3/M4/M5 与 M6）

职责：把各模块已算好的结果组装成 recommend() 需要的 StockRow。
    **本文件只做组装，不含业务判断** —— 判断全在 recommend.py。

数据来源与降级（实测 2026-10-08）：
    ✓ 缠论结构   M4 chan.analyze          —— 真实
    ✓ 个股 RPS   本地日线横截面排名        —— 真实
    ✓ 消息面     M2 news 表               —— 真实
    ✓ 情绪适配   M5 RegimeTracker         —— 真实
    ⚠️ 板块强度  成分股不可得（M5 结论）   —— 用「情绪周期」近似，
                                             并在 risk_note 中显式标注降级

用法：
    python scripts/daily_recommend.py --date 2026-10-08
    python scripts/daily_recommend.py --limit 800     # 只算前 800 只（试跑）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(
    sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}"
)


# ================================================================== RPS
def calc_rps(codes: list[str], as_of: date) -> dict[str, float]:
    """Rank 20-session returns across all stocks visible at the cutoff."""
    from backend.data import duck
    from backend.domain.strength import percentile

    if not codes:
        return {}
    con = duck.get_ro_conn()
    rows = con.execute(
        """
        WITH latest AS (
            SELECT max(trade_date) d FROM kline_daily WHERE trade_date<=?
        ), history AS (
            SELECT code, trade_date, close,
                   row_number() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
            FROM kline_daily, latest
            WHERE trade_date<=latest.d AND close>0 AND NOT coalesce(is_suspend, false)
        ), gains AS (
            SELECT code,
                   max(close) FILTER (WHERE rn=1) current_close,
                   max(close) FILTER (WHERE rn=21) base_close,
                   max(trade_date) last_date
            FROM history WHERE rn<=21 GROUP BY code
        )
        SELECT code, (current_close/base_close-1)*100 FROM gains, latest
        WHERE base_close>0 AND last_date=latest.d
        """,
        [as_of],
    ).fetchall()
    gains = [r[1] for r in rows]
    wanted = set(codes)
    return {code: round(percentile(gain, gains), 1)
            for code, gain in rows if code in wanted}


# ================================================================== 组装
def build_rows(as_of: datetime, limit: int = 0, news_days: int = 3) -> list:
    """组装推荐引擎的输入行。"""
    from backend.data import duck
    from backend.data.sqlite import get_conn
    from backend.domain.chan import analyze
    from backend.domain.recommend import StockRow
    from backend.domain.image_strategy import evaluate, load_policy

    # Daily bars become available at the close, never in the morning report.
    d = as_of.date() if as_of.hour >= 15 else as_of.date() - timedelta(days=1)
    sconn = get_conn()
    con = duck.get_ro_conn()

    # ---- 基础列表（非 ST、非停牌）----
    sql = (
        "SELECT id, name, float_mv, is_suspend FROM stock "
        "WHERE status='active' AND is_st=0"
    )
    args: list = []
    base = sconn.execute(sql, args).fetchall()
    if limit and len(base) > limit:
        # ★ 随机采样而非按代码顺序取前 N（实测踩坑：按 id 排序取前 200
        #   恰好全是 920xxx 北交所，代表性完全失真）
        import random

        base = random.Random(as_of.date().isoformat()).sample(base, limit)
    logger.info(f"基础池 {len(base)} 只")

    codes = [r[0] for r in base]
    if not codes:
        return []

    # ---- 缠论结构分（真实计算）----
    chan_scores: dict[str, tuple[float, list[str]]] = {}
    wanted = set(codes)
    # Avoid binding thousands of Python scalars per query. In the standalone
    # runtime optional-module probing made these IN lists dominate cold ranking.
    # Scan the date window once, then retain the requested universe in Python.
    bars_map = con.execute(
        "SELECT code, trade_date, open, high, low, close, volume, pct_chg "
        "FROM kline_daily WHERE trade_date >= ? AND trade_date <= ? ORDER BY code, trade_date",
        [d - timedelta(days=300), d],
    ).fetchall()
    by_code: dict[str, list] = {}
    for r in bars_map:
        if r[0] not in wanted:
            continue
        by_code.setdefault(r[0], []).append(
            {
                "trade_date": r[1],
                "open": r[2],
                "high": r[3],
                "low": r[4],
                "close": r[5],
                "volume": r[6],
                "pct_chg": r[7],
            }
        )
    logger.info(f"缠论计算中（{len(by_code)} 只，窗口 300 天）…")
    for i, code in enumerate(by_code):
        rows_ = by_code[code]
        if len(rows_) < 60:
            continue
        try:
            res = analyze(code, rows_, as_of=as_of.isoformat(timespec="seconds"))
        except Exception:
            continue
        score, reasons = _chan_to_score(res)
        chan_scores[code] = (score, reasons)
        if (i + 1) % 500 == 0:
            logger.info(f"  缠论 {i + 1}/{len(by_code)}")
    logger.info(f"缠论完成：{len(chan_scores)} 只有结构")

    # ---- RPS ----
    rps = calc_rps(codes, d)
    logger.info(f"RPS 完成：{len(rps)} 只")

    # ---- 近 5 日涨幅 ----
    gain5 = _gain_5d(con, codes, d)
    logger.info(f"5日涨幅完成：{len(gain5)} 只")

    # ---- 当日一字板（买不进）----
    one_word = _one_word_limits(by_code, d)
    if one_word:
        logger.info(f"一字板：{len(one_word)} 只（买不进，硬性剔除）")

    # ---- 消息面（近 3 日）----
    news_map = _news_scores(sconn, codes, news_days, as_of=as_of)
    logger.info(f"消息面完成：{len(news_map)} 只有消息")

    # ---- 上市日期下界 ----
    # ★ 修正（实测踩坑）：原先用「网络请求逐只探测上市日期」，
    #   但只对「2e9<=fmv<2e11 且未停牌」的股票采样，
    #   而被硬性过滤剔除的恰恰是市值越界/停牌的那些 ——
    #   形成死锁：越可能被剔除的越拿不到日期，于是全被「无上市日期」剔除。
    #
    #   正确做法：**直接用本地日线的首根日期**（D11 已验证可靠），
    #   对全市场一次性 SQL 算出，无需任何网络请求。
    lb_map = _list_date_lower_bounds(con, codes, d)
    logger.info(
        f"上市日期下界：{sum(1 for v in lb_map.values() if v)} 只"
        f"（本地日线首根日期，零网络请求）"
    )

    from backend.services.market_context import board_context, market_context

    boards = board_context(con, as_of)
    market = market_context(con, as_of)
    logger.info("板块与市场环境完成，计算图片蒸馏因子…")
    emotion = {
        "frozen": 10,
        "repair": 40,
        "start": 70,
        "ferment": 85,
        "peak": 55,
        "ebb": 20,
    }.get(market["state"], 0)

    image_policy = load_policy()
    latest_session = con.execute("SELECT max(trade_date) FROM kline_daily WHERE trade_date<=?", [d]).fetchone()[0]
    index_rows = con.execute(
        "SELECT trade_date, close, volume FROM index_daily WHERE index_code='sh000001' "
        "AND trade_date<=? AND trade_date>=? ORDER BY trade_date", [d, d - timedelta(days=300)]
    ).fetchall()
    index_bars = [dict(zip(("trade_date", "close", "volume"), r)) for r in index_rows]

    # ---- 组装行 ----
    rows_out: list[StockRow] = []
    for code, name, fmv, susp in base:
        cs, creasons = chan_scores.get(code, (0.0, []))
        nr = news_map.get(
            code, {"score": 50.0, "direction": "neutral", "strength": 0, "reasons": []}
        )
        board = boards.get(code)
        board_ok = board is not None and not board["stale"]
        board_note = (
            f"{board['name']} 强度 {board['strength']:.1f}，"
            f"因子覆盖 {board['coverage'] or 0:.0%}，时点 {board['ts']}"
            if board_ok
            else "板块强度因子降级（无当时成分映射或快照已过期）"
        )
        rows_out.append(
            StockRow(
                code=code,
                name=name,
                is_st=False,
                is_suspend=bool(susp),
                float_mv=float(fmv or 0),
                list_date_lb=lb_map.get(code),
                gain_5d=gain5.get(code, 0.0),
                is_one_word_limit=bool(one_word.get(code, False)),
                s_board=board["strength"] if board_ok else 50.0,
                board_note=board_note,
                board_degraded=not board_ok or board["degraded"],
                s_chanlun=cs,
                s_rps=rps.get(code, 50.0),
                s_news=nr["score"],
                s_emotion=emotion,
                image_strategy=evaluate(by_code.get(code, []), index_bars, as_of,
                                        image_policy, expected_day=latest_session),
                chan_reasons=creasons,
                news_reasons=nr["reasons"],
                news_direction=nr["direction"],
                news_strength=nr["strength"],
            )
        )
    logger.info(f"图片蒸馏因子完成：{len(rows_out)} 只")
    return rows_out


def _chan_to_score(res) -> tuple[float, list[str]]:
    """缠论结果 -> 结构分（0~100）+ 依据。

    附录 4.3「结构分」定义：未破前一买点低点=100；跌破=0；处于新中枢上沿=70~90。
    这里按位置 + 信号强度综合折算。
    """
    score = 40.0
    reasons: list[str] = []

    pos = res.position()
    if pos == "中枢上方":
        score = 75.0
        reasons.append("处于最后中枢上方（结构偏强）")
    elif pos == "中枢内":
        score = 55.0
        reasons.append("处于中枢区间内（结构中性）")
    elif pos == "中枢下方":
        score = 35.0
        reasons.append("处于最后中枢下方（结构偏弱）")

    # 有强级别买点加权
    for s in res.signals:
        if s.kind == "3buy" and s.level == "strong":
            score += 20
            reasons.append(f"三买成立（{s.date}）：{s.reason}")
            break
        if s.kind == "1buy":
            score += 15
            reasons.append(f"一买（底背驰，{s.date}）")
            break
    # 有卖点减分
    for s in res.signals:
        if s.kind in ("3sell", "1sell") and s.level == "strong":
            score -= 15
            reasons.append(f"卖点信号（{s.date}）：{s.reason}")
            break

    if not reasons:
        reasons.append(f"无明确买卖点；{pos}")
    return max(0.0, min(100.0, score)), reasons


def _list_date_lower_bounds(
    con, codes: list[str], as_of: date | None = None
) -> dict[str, str | None]:
    """用本地日线首根日期作为上市日期下界（D11 方案）。

    ★ 相比逐只网络探测的优势：
      · 零网络请求，瞬时完成
      · 对全市场一次性 SQL 算出，不存在「采样漏掉谁」的问题
      · 实测精度：宁德时代/中国平安精确命中，茅台差 22 天
        （首根日期是下界，只偏晚不偏早，恰好够「上市 > 60 日」判定）
    """
    if not codes:
        return {}
    wanted = set(codes)
    try:
        rows = con.execute(
            "SELECT code, min(trade_date) FROM kline_daily "
            + (" WHERE trade_date<=?" if as_of else "")
            + " GROUP BY code",
            [as_of] if as_of else [],
        ).fetchall()
        return {r[0]: (str(r[1]) if r[1] else None) for r in rows if r[0] in wanted}
    except Exception as exc:
        logger.warning(f"上市日期下界计算失败: {exc}")
        return {}


def _gain_5d(con, codes: list[str], d: date) -> dict[str, float]:
    """近 5 个交易日累计涨幅 %。"""
    if not codes:
        return {}
    wanted = set(codes)
    try:
        rows = con.execute(
            f"""
            WITH dd AS (
                SELECT DISTINCT trade_date FROM kline_daily
                WHERE trade_date <= DATE '{d}' ORDER BY trade_date DESC LIMIT 6
            ),
            firstlast AS (
                SELECT code,
                       first_value(close) OVER w AS c0,
                       last_value(close) OVER w AS c1,
                       count(*) OVER (PARTITION BY code) AS n
                FROM kline_daily
                WHERE trade_date IN (SELECT trade_date FROM dd)
                WINDOW w AS (PARTITION BY code ORDER BY trade_date
                             ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
            )
            SELECT DISTINCT code, (c1 / c0 - 1) * 100 FROM firstlast WHERE c0 > 0 AND n=6
            """,
            [],
        ).fetchall()
        return {r[0]: round(r[1], 2) for r in rows if r[0] in wanted}
    except Exception as exc:
        logger.warning(f"近5日涨幅计算失败: {exc}")
        return {}


def _one_word_limits(by_code: dict[str, list], d: date) -> dict[str, bool]:
    """判定「当日一字板涨停」（买不进）。

    ★ 一字板的定义（与「首板」完全不同，勿混）：
        open == high == low == close  且  该收盘价 == 涨停价
      即开盘即在涨停价、全天不开板。

    涨停价 = 前收 × (1 + 限幅)，四舍五入到分；限幅按板块取
    10%（主板）/20%（创业板、科创板）/30%（北交所），ST 为 5%。
    这里复用 backend.domain.breadth 已验证的限幅口径，避免第二套实现走样。

    ★ 原实现把该字段硬编码为 False 并注释「需一字板数据源」，
      但一字板所需的 OHLC 与前收本地日线里全都有 —— 不需要额外数据源。
      硬编码 False 会让 recommend.hard_filter 里那条「一字板买不进」
      的过滤**永久失效**（该分支只在单元测试里被喂过 True）。
    """
    from backend.domain.breadth import limit_up_price

    out: dict[str, bool] = {}
    for code, rows in by_code.items():
        # rows 已按 trade_date 升序，且只含 <= d 的交易日
        if len(rows) < 2:
            continue
        cur, prev = rows[-1], rows[-2]
        if str(cur.get("trade_date")) != str(d):
            # 当日无行情（停牌/未收盘）不算一字板
            continue
        o, h, l, c = cur.get("open"), cur.get("high"), cur.get("low"), cur.get("close")
        pre = prev.get("close")
        if None in (o, h, l, c, pre):
            continue
        if not (o == h == l == c):
            continue
        lp = limit_up_price(float(pre), code, is_st=False)
        if lp is not None and abs(float(c) - lp) < 1e-6:
            out[code] = True
    return out


def _news_scores(
    conn, codes: list[str], days: int = 3, *, as_of: datetime
) -> dict[str, dict]:
    """近 N 日消息面加权得分（附录 4.4）。

    利好 +strength；利空 −strength×1.5（非对称）。
    """
    try:
        rows = conn.execute(
            """
            SELECT related_codes, direction, strength, content, ts
            FROM news
            WHERE substr(ts,1,10) >= ? AND substr(ts,1,10) <= ?
              AND related_codes IS NOT NULL
            """,
            ((as_of - timedelta(days=days + 1)).date().isoformat(),
             (as_of + timedelta(days=1)).date().isoformat()),
        ).fetchall()
    except Exception as exc:
        logger.warning(f"消息面查询失败: {exc}")
        return {}

    agg: dict[str, dict] = {}
    for codes_json, direction, strength, content, ts in rows:
        try:
            when = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=CST)
        cutoff = as_of if as_of.tzinfo else as_of.replace(tzinfo=CST)
        if not cutoff - timedelta(days=days) <= when <= cutoff:
            continue
        try:
            import json as _j

            lst = _j.loads(codes_json) if codes_json else []
        except Exception:
            continue
        s = int(strength or 0)
        if direction == "bull":
            delta = s
        elif direction == "bear":
            delta = -s * 1.5  # ★ 利空权重更高（非对称）
        else:
            continue
        for c in lst:
            if c not in codes:
                continue
            e = agg.setdefault(
                c, {"raw": 0.0, "reasons": [], "direction": "neutral", "strength": 0}
            )
            e["raw"] += delta
            if abs(delta) >= 2:
                e["reasons"].append(
                    f"{'利好' if delta > 0 else '利空'}强度{abs(delta):.1f}："
                    f"{(content or '')[:30]}"
                )
            if delta > 0 and direction == "bull" and s > e["strength"]:
                e["direction"] = "bull"
                e["strength"] = s
            elif delta < 0 and direction == "bear":
                e["direction"] = "bear"
                e["strength"] = max(e["strength"], s)

    out: dict[str, dict] = {}
    for code, e in agg.items():
        # 原始分 -> 0~100（±10 分对应 0~100）
        score = max(0.0, min(100.0, 50 + e["raw"] * 5))
        out[code] = {
            "score": round(score, 1),
            "direction": e["direction"],
            "strength": e["strength"],
            "reasons": e["reasons"][:3] or ["近3日消息影响中性"],
        }
    return out


# ================================================================== main
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    from backend.data import duck
    duck.init_db()
    p = argparse.ArgumentParser(description="每日推荐")
    p.add_argument("--date", default="", help="YYYY-MM-DD，默认今日")
    p.add_argument("--limit", type=int, default=0, help="只算前 N 只（试跑）")
    p.add_argument("--out", default="data/recommend_latest.json")
    a = p.parse_args()

    d = date.fromisoformat(a.date) if a.date else datetime.now(CST).date()
    as_of = datetime(d.year, d.month, d.day, 8, 30, tzinfo=CST)
    logger.info(f"生成推荐 as_of={as_of.isoformat()}")

    rows = build_rows(as_of, limit=a.limit)
    if not rows:
        logger.error("无可用数据")
        return 1
    logger.info(f"组装完成 {len(rows)} 行")

    from backend.domain.recommend import recommend
    from backend.services.market_context import market_context

    market = market_context(duck.get_ro_conn(), as_of)
    res = recommend(
        rows, d, market_state=market["state"], position_cap=market["position_cap"]
    )
    res["market_context"] = market
    res["as_of"] = as_of.isoformat()
    res["metadata_time_policy"] = "stock 主表属性为当前值，历史属性尚未版本化"

    logger.info(
        f"过滤：{res['total_input']} → 通过 {res['after_filter']}"
        f"（剔除 {res['rejected']}）"
    )
    logger.info(f"分档：{res['stats']}")

    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("=" * 70)
    logger.info("【推荐清单】")
    for rec in res["recommendations"][:12]:
        logger.info(
            f"  [{rec['tier']}] {rec['code']} {rec['name']} 得分 {rec['score']:.1f}"
        )
        if rec["reasons"]:
            logger.info(f"依据: {rec['reasons'][0][:60]}")
        logger.info(f"   ★风险: {rec['risk_note'][:90]}")

    logger.info("=" * 70)
    logger.info("【剔除统计 Top5 原因】")
    from collections import Counter

    cnt = Counter()
    for rj in res["rejections"]:
        for rs in rj["reasons"]:
            key = rs.split("（")[0].split(" ")[0]
            cnt[key] += 1
    for k, v in cnt.most_common(5):
        logger.info(f"  {k}: {v} 只")

    logger.info(f"结果已落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
