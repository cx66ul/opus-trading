# -*- coding: utf-8 -*-
"""全历史回填 `market_regime` 表（M5-3 数据侧闭环的最后一块）。

★ 为什么现在能做
-----------------
分钟线同步已结束，DuckDB 写锁空闲。此前 `run_regime_offline.py` 已证明：
市场宽度、连板高度、两市成交额**全部可从 Parquet 快照离线算出**，
不必等锁、不必逐日调数据源。
本脚本把 `run_regime_offline` 的结论**真正写进库** —— 此前它只打印到屏幕，
M6 五因子与 M7 邮件模板读不到，`market_regime` 一直是 0 行。

★ 输入（全部 point-in-time，只用 date<=d 的数据）
--------------------------------------------------
  · 涨停/跌停/炸板/封板率 ← `breadth.breadth_sql`（严格涨停价口径）
  · 最高连板      ← 严格封板 + 连续交易日序号分组
  · 两市成交额     ← `close × volume` 估算（`amount` 全NULL，见下）
  · 昨日涨停今日溢价 ← ★本脚本新增，见`premium_by_day`
  · 指数涨跌      ← 全市场等权平均涨跌幅（代理口径，已显式标注）

★★ 昨日涨停今日溢价：此前一直「离线算不了」，其实是没写
------------------------------------------------------
`Sentiment.yesterday_premium` 恒为 0 会让 `classify_regime` 的
「退潮」分支（`broken>=0.55 and prem<-1.5`）**永不触发**，
「修复」分支（`prem>=0`）退化成只看涨跌停家数。
本脚本用「昨日封板的股票，今日 pct_chg 的均值」算出来 —— 数据就在 Parquet 里。

★ 指数涨跌用等权平均而非真实指数
-------------------------------
`kline_daily` 只有个股，没有指数表。用 `breadth.avg_chg`（全市场等权均值）
作代理，口径不同（等权 vs 市值加权），已在 `detail_json` 里显式标注
`index_pct_source="equal_weight_avg"`，不冒充上证指数。

★★ 状态机用迟滞版（`RegimeTracker`），不是单点判定
--------------------------------------------------
单点判定会在阈值附近逐日翻转。生产落库必须用带迟滞的轨迹，
否则回测读到的状态序列会自相矛盾（同一份数据两次跑出不同结果）。
`RegimeTracker` 是**有状态**组件，其输入顺序即输出，本脚本严格按日期升序驱动。

运行：
    python scripts/backfill_regime.py                # 全历史回填
    python scripts/backfill_regime.py --dry-run      # 只报告不落库
    python scripts/backfill_regime.py --limit-days 20
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

import duckdb                                                       # noqa: E402

# 复用 run_regime_offline 已验证的三个离线计算函数（不复制代码）
from run_regime_offline import (                # noqa: E402
    compute_breadth, load_st_codes, streak_by_day, to_sentiment, volume_by_day,
)

from backend.data import duck                   # noqa: E402
from backend.domain.data_gate import (          # noqa: E402
    coverage_from_parquet, daily_universe,
)
from backend.domain.regime import (             # noqa: E402
    MarketEnv, REGIME_META, RegimeTracker, score_market,
)

PARQUET = _ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"


# ------------------------------------------------------------------ 昨日涨停溢价
def premium_by_day(con, parquet: Path) -> dict[str, tuple[float | None, int]]:
    """★ 昨日涨停的股票，**今日**平均涨跌幅（%）。

    「昨日涨停今日溢价」是情绪面的核心指标：它衡量的是
    **资金愿不愿意接力**，而不是「今天有多少只涨停」。

    ★ 实现要点：必须用**交易日序号**做滞后，不能用 `trade_date - 1 DAY`。
      周末/节假日会让自然日滞后错位（周五的涨停股对应「周四」而非「周六」），
      交易日序号（`dense_rank`）才对。

    ★★ 返回键是**日期**而非序号：调用方会把不可用日期过滤掉
      （横截面不完备的65 天），若这里返回序号，两边的序号就会错位
      —— 昨日溢价会被安到错误的那一天，且不报错。
    """
    q = f"""
    WITH d AS (
      SELECT p.trade_date, p.code, p.close, p.pre_close, p.pct_chg,
             (s.code IS NOT NULL) AS is_st,
             CASE
               WHEN s.code IS NOT NULL THEN 5.0
               WHEN p.code LIKE '688%' THEN 20.0
               WHEN p.code LIKE '300%' OR p.code LIKE '301%' OR p.code LIKE '302%' THEN 20.0
               WHEN p.code LIKE '920%' OR p.code LIKE '83%' OR p.code LIKE '87%' THEN 30.0
               ELSE 10.0 END AS lim
      FROM read_parquet('{parquet.as_posix()}') p
      LEFT JOIN st_list s ON p.code = s.code
      WHERE p.is_suspend = false AND p.pre_close > 0 AND p.pct_chg IS NOT NULL
    ),
    sealed AS (
      SELECT trade_date, code FROM d
      WHERE NOT is_st AND abs(close - round(pre_close * (1 + lim/100), 2)) < 0.005
    ),
    -- 全部交易日的序号（★不用自然日，避免节假日错位）
    numbered AS (
      SELECT trade_date,
             dense_rank() OVER (ORDER BY trade_date) AS td_idx
      FROM (SELECT DISTINCT trade_date FROM d)
    ),
    prev_sealed AS (
      SELECT n.td_idx, s.code
      FROM sealed s JOIN numbered n ON n.trade_date = s.trade_date
    ),
    today AS (
      SELECT n.trade_date, d.code, d.pct_chg,
             CASE WHEN ps.code IS NOT NULL THEN 1 ELSE 0 END AS was_lu_yesterday
      FROM d
      JOIN numbered n ON n.trade_date = d.trade_date
      LEFT JOIN prev_sealed ps ON ps.code = d.code AND ps.td_idx = n.td_idx - 1
    )
    SELECT CAST(trade_date AS VARCHAR) AS d,
           avg(CASE WHEN was_lu_yesterday = 1 THEN pct_chg END) AS prem,
           sum(was_lu_yesterday)AS n_prev
    FROM today GROUP BY trade_date ORDER BY trade_date
    """
    out: dict[str, tuple[float | None, int]] = {}
    for d, prem, n_prev in con.execute(q).fetchall():
        out[str(d)] = (float(prem) if prem is not None else None, int(n_prev or 0))
    return out


# ★★ 语料口径（2026-10-11 用户确认）：**大盘 = 上证指数** sh000001
INDEX_MAIN = "sh000001"


def index_pct_by_day(con, index_code: str = INDEX_MAIN) -> dict[str, float]:
    """真实指数日涨跌幅（%），按 `index_daily` 取。

    ★★ 2026-10-11 起替代 `b.avg_chg`（全市场**等权均值**）代理。
      实测两者在 30 个交易日里**符号经常相反**、平均绝对差 0.5pp、
      最大 2.0pp（09-09 等权 −0.47% vs 上证 +0.28%）
      ⇒ 等权代理会让市场状态判反，且**不报任何错**。

    ★ 用窗口函数自取前收，不信任何存储的 pre_close 列
      （与项目里其它地方同一条纪律）。
    ★ 缺该指数的交易日**返回缺失**而非 0.0：涨跌幅 0 与「没数据」
      在状态机里含义完全不同（0 = 真实平盘）。
    """
    #★★ 本脚本用 `duckdb.connect()`（**内存连接**，不加载 hawk.duckdb），
    #   所以读不到主库的 `index_daily` 表 ——
    #   实测报 `Catalog Error: Table with name index_daily does not exist!
    #   Did you mean "pg_index"?`
    #   ⇒ 只能**显式按路径 attach 主库**，而不是改连接模式
    #   （改连接模式会和本脚本「只读 parquet、不碰主库」的设计冲突）。
    from backend.data.duck import DB_PATH
    if not DB_PATH.exists():
        return {}
    #  ★ `ATTACH` 是 DDL，**不支持占位符**（实测 Parser Error），
    #    路径只能内插 ⇒ 必须自己挡引号/分号，否则一个路径就能执行任意 SQL
    path = str(DB_PATH).replace("'", "''")
    if ";" in path:
        raise ValueError(f"数据库路径含分号，拒绝内插：{path!r}")
    con.execute(f"ATTACH '{path}' AS hawkdb (READ_ONLY)")
    try:
        rows = con.execute(
            """
            WITH k AS (
              SELECT trade_date, close,
                     lag(close) OVER (ORDER BY trade_date) AS prev
              FROM hawkdb.index_daily WHERE index_code = ?
            )
            SELECT CAST(trade_date AS VARCHAR), (close / prev - 1.0) * 100.0
            FROM k WHERE prev IS NOT NULL AND prev > 0
            """, [index_code]).fetchall()
    finally:
        con.execute("DETACH hawkdb")
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


#: 每日记录「当天用的是代理还是真指数」，跑完在报告里汇总
_FALLBACK_DAYS: list[str] = []


def _fallback_idx(b) -> float:
    """该日无指数数据时的退路：等权均值。

    ★ 必须**记账**：退而不记 ⇒ 事后看到某天状态判反，
      根本不知道那天用的是哪一种口径。
    ★ 这不是「静默降级」：调用点会把
      `index_pct_source` 写成 `equal_weight_avg_fallback`，
      而报告会打印退回了多少天。
    """
    _FALLBACK_DAYS.append(str(b.date))
    return b.avg_chg


def build_rows(con, parquet: Path, limit_days: int | None = None
               ) -> tuple[list[dict], dict]:
    """从 Parquet 计算全历史 market_regime 行（★不写库）。

    ★★ 横截面完备度闸门（`backend/domain/data_gate.py`）
      M1 渐进同步导致 2025-06-19~09-04 每天只有 1~93 只股票。
      不过滤的话：
        · `sum(close*volume)` 分母只有 1 只 ⇒ 量比出现 **57.78** 爆表值
          （真实区间 0.55~1.5），而 `vol_ratio >= 1.5` 直接把量能分打满 100
        · 涨停家数 / 上涨占比的分母只有 1~20 只 ⇒ 情绪因子完全失真
      ⇒ 这 65 天不进库，`market_regime` 从完备区间起算。
    """
    con.execute("CREATE OR REPLACE TEMP TABLE st_list(code VARCHAR)")
    st = load_st_codes()
    if st:
        con.executemany("INSERT INTO st_list VALUES (?)", [(c,) for c in sorted(st)])

    cov = coverage_from_parquet(con, parquet)
    ok_days = set(cov.usable)

    idx_pct = index_pct_by_day(con)

    days = [b for b in compute_breadth(con, parquet) if b.date in ok_days]
    if not days:
        raise SystemExit("完备区间为空 —— 检查 data_gate 阈值是否过严")
    days = streak_by_day_and_recalc(con, parquet, st, days)
    vols = volume_by_day(con, parquet)
    prem_raw = premium_by_day(con, parquet)

    tracker = RegimeTracker()
    rows: list[dict] = []
    by_state: dict[str, int] = defaultdict(int)
    missing_prem = 0

    for b in days:
        sent = to_sentiment(b)
        prem, n_prev = prem_raw.get(b.date, (None, 0))
        if prem is None:
            # ★ 缺失不当0：昨天没有任何涨停股时溢价无定义，
            #   沿用 0.0 会让「退潮」分支的 prem<-1.5 永不成立。
            #   这里显式计数，最终在报告里告知比例。
            missing_prem += 1
            sent.yesterday_premium = 0.0
        else:
            sent.yesterday_premium = prem

        amt, ma5 = vols.get(b.date, (0.0, 0.0))
        from backend.domain.regime import VolumeInfo
        env = MarketEnv(
            as_of=b.date,
            sentiment=sent,
            volume=VolumeInfo(total_amount=amt, amount_ma5=ma5),
            # ★★ 真实上证指数；该日缺指数时才退回等权代理并计数
            index_pct=(idx_pct.get(b.date) if idx_pct.get(b.date) is not None
                       else _fallback_idx(b)),

            outside_ok=None,             # 外围无数据源 → 中性50+重新归一化
        )
        env = score_market(env)
        point = tracker.update(env)

        detail = dict(env.detail)
        detail.update({
            # ★ 记录这一行**实际用的是哪个口径**——
            #   混用两种口径而不留痕，事后无法判断某天判反是不是因为这个
            "index_pct_source": (
                INDEX_MAIN if idx_pct.get(b.date) is not None
                else "equal_weight_avg_fallback"),
            "yesterday_premium_n": n_prev,
            "yesterday_premium_available": prem is not None,
            "sample": b.sample,
            "avg_chg": round(b.avg_chg, 3),
            "limit_down": b.limit_down,
            "seal_rate": (round(b.seal_rate, 2) if b.seal_rate is not None else None),
            "broken": b.broken,
            "sealed": b.sealed,
            "advance": b.advance,
            "decline": b.decline,
            "regime_meta": REGIME_META[point.regime],
            # ★★ 判定与评分是否自相矛盾：score 只有 38 却判「启动」时必须留痕。
            #   六态是**序数分类**（规则优先），MarketScore 是**连续评分**，
            #   两者本可不一致；但不一致占比过高说明分类规则已失去区分度。
            "score_regime_gap": round(env.score - point.cap * 100, 2),
        })
        sr = b.seal_rate
        rows.append({
            "ts": datetime.strptime(b.date, "%Y-%m-%d").replace(hour=15),
            "score": round(env.score, 2),
            "state": point.regime,
            "position_cap": point.cap,
            "limit_up_cnt": b.limit_up,
            "limit_down_cnt": b.limit_down,
            # 「当日无涨停」时封板率无定义 ⇒炸板率也���定义，返回 None 而非 0
            "broken_rate": (None if sr is None
                            else round((100.0 - sr) / 100.0, 4)),
            "max_streak": b.max_streak,
            "total_amount": amt,
            "up_ratio": round(b.advance_ratio / 100.0, 4),
            "detail_json": json.dumps(detail, ensure_ascii=False),
        })
        by_state[point.regime] += 1

    if limit_days:
        rows = rows[-limit_days:]

    stats = {
        "days": len(days),
        "rows": len(rows),
        "state_dist": dict(by_state),
        "missing_premium_days": missing_prem,
        "date_range": (days[0].date, days[-1].date) if days else None,
        "st_codes": len(st),
        "coverage": cov.summary(),
    }
    return rows, stats


def streak_by_day_and_recalc(con, parquet: Path, st: set[str], days):
    """把连板高度贴回 `BreadthDay`（`run_regime_offline.attach_streaks` 的薄封装）。"""
    streaks = streak_by_day(con, parquet, st)
    for b in days:
        b.max_streak = streaks.get(b.date, 0)
    return days


# ------------------------------------------------------------------ 写库
def write_rows(rows: list[dict]) -> int:
    duck.init_db()
    c = duck.get_conn()
    c.executemany(
        """
        INSERT INTO market_regime
          (ts, score, state, position_cap, limit_up_cnt, limit_down_cnt,
           broken_rate, max_streak, total_amount, up_ratio, detail_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ts) DO UPDATE SET
          score=EXCLUDED.score, state=EXCLUDED.state,
          position_cap=EXCLUDED.position_cap, limit_up_cnt=EXCLUDED.limit_up_cnt,
          limit_down_cnt=EXCLUDED.limit_down_cnt, broken_rate=EXCLUDED.broken_rate,
          max_streak=EXCLUDED.max_streak, total_amount=EXCLUDED.total_amount,
          up_ratio=EXCLUDED.up_ratio, detail_json=EXCLUDED.detail_json
        """,
        [(r["ts"], r["score"], r["state"], r["position_cap"], r["limit_up_cnt"],
          r["limit_down_cnt"], r["broken_rate"], r["max_streak"], r["total_amount"],
          r["up_ratio"], r["detail_json"]) for r in rows],
    )
    return len(rows)


def print_report(rows: list[dict], stats: dict) -> None:
    print()
    print("=" * 92)
    print("market_regime 全历史回填")
    print("=" * 92)
    dr = stats["date_range"]
    cv = stats["coverage"]
    print(f"ST 标记 {stats['st_codes']} 只")
    print(f"★ 横截面完备度闸门：剔除 {cv['n_unusable']} 天"
          f"（标的数 < {cv['threshold']} = {cv['max_universe']}×{cv['min_universe_frac']}）")
    print(f"  ⇒ 不剔除的话，前几天只有 1 只股票，两市成交额会低两个数量级，")
    print(f"     量比爆表到 57.78（真实区间 0.55~1.5），量能因子直接被打满 100 分")
    print(f"有效交易日 {stats['days']} 天（{dr[0]} ~ {dr[1]}），待写入 {stats['rows']} 行")
    print()
    print("状态分布：")
    total = sum(stats["state_dist"].values()) or 1
    for k, v in sorted(stats["state_dist"].items(), key=lambda kv: -kv[1]):
        m = REGIME_META[k]
        print(f"  {m['emoji']} {m['label']:4s} {v:5d} 天  {v / total * 100:5.1f}%  "
              f"仓位上限 {m['cap']:.0%}")
    print()
    # ★ 口径披露：真实指数 vs 等权代理，退回了多少天必须看得见
    n_fb = len(_FALLBACK_DAYS)
    if n_fb:
        print(f"  ⚠ 大盘涨跌有 {n_fb} 天缺 {INDEX_MAIN} 数据，"
              f"已退回**等权均值代理**（口径不同，状态可能判反）：")
        print(f"     例：{', '.join(_FALLBACK_DAYS[:5])}")
        print("     ⇒ 先跑 scripts/fetch_index_daily.py --all 补齐")
    else:
        print(f"  ✓ 大盘涨跌全部用真实指数 {INDEX_MAIN}（非等权代理）")
    print()
    if stats["missing_premium_days"]:
        print(f"  ⚠ 昨日涨停溢价不可用 {stats['missing_premium_days']} 天"
              f"（当日无涨停股 ⇒ 溢价无定义，已按 0 处理并计数）")
    print()
    print(f"{'日期':>12s}{'评分':>7s}{'状态':>8s}{'涨停':>6s}{'跌停':>6s}"
          f"{'炸板率':>8s}{'连板':>6s}{'溢价':>8s}{'量比':>7s}  说明")
    for r in rows[-12:]:
        d = json.loads(r["detail_json"])
        vr = d.get("vol_ratio", 0.0)
        prem = d.get("yesterday_premium")
        prem_s = f"{prem:+.2f}" if prem is not None else "无"
        m = REGIME_META[r["state"]]
        print(f"{r['ts']:%Y-%m-%d}  {r['score']:6.1f}{m['emoji']}{m['label']:>4s}"
              f"{r['limit_up_cnt']:6d}{r['limit_down_cnt']:6d}"
              f"{r['broken_rate'] * 100:7.1f}%{r['max_streak']:6d}"
              f"{prem_s:>8s}{vr:7.2f}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 market_regime（全历史）")
    ap.add_argument("--parquet", default=str(PARQUET))
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    ap.add_argument("--limit-days", type=int, default=None)
    args = ap.parse_args()

    pq = Path(args.parquet)
    if not pq.exists():
        print(f"Parquet 不存在: {pq}")
        return 1

    con = duckdb.connect()
    rows, stats = build_rows(con, pq, args.limit_days)
    con.close()

    print_report(rows, stats)

    if args.dry_run:
        print("--dry-run：未写库")
        return 0

    n = write_rows(rows)
    print(f"✔ 已写入 market_regime {n} 行")
    total = duck.table_counts().get("market_regime")
    print(f"  表内现有 {total:,} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
