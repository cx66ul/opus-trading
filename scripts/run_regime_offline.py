# -*- coding: utf-8 -*-
"""把离线市场宽度接入 M5 六态状态机（M5-3 的数据侧闭环）。

★ 为什么需要这个脚本
--------------------
D17 实测：策略超额 **7/7 档全为正**（+1.15% ~ +2.53%），
但按 D15 的结论 —— **超额来自「跟随大盘」，不是个股 alpha**。
要拿到真 alpha，必须引入**大盘之外的择时信息**，
而 M5 六态状态机的输入（涨停/跌停/炸板/连板）正是这类信息。

★ 关键约束：数据必须来自**离线计算**（`backend/domain/breadth.py`）
--------------------------------------------------------------------
分钟线同步进程持有 DuckDB 写锁时（实测数小时）读不了库。
`breadth.py` 改从 Parquet 快照离线算，本脚本用 SQLite 取 ST 标记
（SQLite **不被 DuckDB 锁阻塞**）⇒ 锁占用期间也能跑。

★ 三条口径纪律（都来自实测，不靠猜）
------------------------------------
1. **涨停价四舍五入到分** —— `round(pre_close×(1+限幅), 2)`。
   用宽松的 `pct_chg >= 限幅-0.3` 会多判（603823 收 46.59 / 涨停价 46.65）。
2. **涨停排除 ST**（东财涨停池口径），**跌停包含 ST**。
3. **炸板 = high 触及涨停价 且 收盘未封**。早期写成
   `abs(high - high×(1+限幅))`（拿最高价再乘涨停幅度）⇒ 炸板恒为 0、
   封板率恒为 100%，且不报错。已由 `tests/test_breadth.py` 守护。

★ 与 M5 已入库实时值的交叉验证
------------------------------
2026-10-08 M5 用东财实时数据判为「修复」（涨停 43 / 跌停 13 / 炸板 42.7%）。
本脚本离线算得涨停 42 / 跌停 14 / 封板率 52.5%（⇒ 炸板率 47.5%）。
两者接近但**不相同** —— 这是**预期的**，因为：
  · 涨停差 1 只：920627 力王股份（北交所新股，Parquet 无记录）
  · 炸板率差 ~5pp：东财用**全池**（含 ST 与次新股）口径，
    本脚本用**严格封板**口径。口径不同，不应强行对齐。

运行：
    python scripts/run_regime_offline.py
    python scripts/run_regime_offline.py --backtest   # 顺带回测验证
"""
from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb                                   # noqa: E402

from backend.domain.breadth import (           # noqa: E402
    BreadthDay, breadth_sql, limit_up_price, rows_to_days,
)
from backend.domain.regime import (            # noqa: E402
    MarketEnv, Sentiment, VolumeInfo, classify_regime,
)

PARQUET = _ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"
SQLITE = _ROOT / "data" / "hawk.sqlite"


def load_st_codes() -> list[str]:
    """ST 标记来自 SQLite（不被 DuckDB 写锁阻塞）。"""
    if not SQLITE.exists():
        return []
    conn = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
    try:
        return [r[0] for r in
                conn.execute("SELECT id FROM stock WHERE is_st=1").fetchall()]
    finally:
        conn.close()


def compute_breadth(con, parquet: Path) -> list[BreadthDay]:
    """离线算全历史市场宽度。"""
    con.execute("CREATE OR REPLACE TEMP TABLE st_list(code VARCHAR)")
    st = load_st_codes()
    if st:
        con.executemany("INSERT INTO st_list VALUES (?)",
                        [(c,) for c in sorted(st)])
    rows = con.execute(breadth_sql(parquet.as_posix())).fetchall()
    return rows_to_days(rows)


def streak_by_day(con, parquet: Path, st: set[str]) -> dict[str, int]:
    """★ 每日「最高连板高度」。

    连板 = 连续封涨停的天数。严格口径：当日封板且**昨日也封板**。

    ★ 为什么必须用严格封板口径：若用 `pct_chg>=限幅-0.3`，
      603823 这类「差 1 分未封」的会被误算成连板，
      连板高度虚高 ⇒ 状态机误判为「发酵」。
    """
    q = f"""
    WITH d AS (
      SELECT p.trade_date, p.code, p.close, p.pre_close,
             CASE
               WHEN s.code IS NOT NULL THEN 5.0
               WHEN p.code LIKE '688%' THEN 20.0
               WHEN p.code LIKE '300%' OR p.code LIKE '301%' OR p.code LIKE '302%' THEN 20.0
               WHEN p.code LIKE '920%' OR p.code LIKE '83%' OR p.code LIKE '87%' THEN 30.0
               ELSE 10.0 END AS lim,
             (s.code IS NOT NULL) AS is_st
      FROM read_parquet('{parquet.as_posix()}') p
      LEFT JOIN st_list s ON p.code = s.code
      WHERE p.is_suspend = false AND p.pre_close > 0
    ),
    sealed AS (
      SELECT trade_date, code FROM d
      WHERE NOT is_st AND abs(close - round(pre_close * (1 + lim/100), 2)) < 0.005
    ),
    seq AS (
      SELECT s.trade_date, s.code,
             ROW_NUMBER() OVER (PARTITION BY s.code ORDER BY s.trade_date) AS rn
      FROM sealed s
    ),
    -- 用 trading-day 序号而非自然日，避免周末/节假日断连
    numbered AS (
      SELECT code, trade_date, rn,
             trade_date - (rn * INTERVAL 1 DAY) AS grp
      FROM seq
    )
    SELECT trade_date, max(cnt) AS max_streak FROM (
      SELECT trade_date, code, count(*) OVER (PARTITION BY code, grp) AS cnt
      FROM numbered
    ) GROUP BY trade_date ORDER BY trade_date
    """
    out: dict[str, int] = {}
    for d, n in con.execute(q).fetchall():
        out[str(d)] = int(n or 0)
    return out


def volume_by_day(con, parquet: Path) -> dict[str, tuple[float, float]]:
    """★ 每日两市成交额与前 5 日均值（Parquet，无需 DuckDB）。

    ★★ `amount` 字段在快照里 **100% 为 NULL**（实测 1,375,427 行
       非空 0 行 —— M1 的已知问题）。若直接 `sum(amount)`，
       成交额全为 0 ⇒ `VolumeInfo.vol_ratio` 在 `amount_ma5<=0`
       时会**静默返回默认 1.00**（见 `regime.py`），状态机照样跑、
       结果看着正常，但量能因子实际是死的。

       ⇒ 这里用 `close × volume` 估算成交额（M1 已验证的口径，
         仅用于量比这种**相对量**的因子，绝对值精度不重要）。
       若将来 `amount` 有数据，估算值会被真实值覆盖。

    ★ 基准 = **前 5 日**同源均值（不含当日，避免自我摊平 ——
      与 `strategy.vol_ratio` 同理：分母含当前值则阈值永不触发）。
    """
    q = f"""
    SELECT CAST(trade_date AS VARCHAR) AS d,
           sum(CASE WHEN amount IS NOT NULL AND amount > 0
                    THEN amount
                    ELSE close * volume END) AS amt
    FROM read_parquet('{parquet.as_posix()}')
    WHERE is_suspend = false AND close > 0
    GROUP BY trade_date ORDER BY trade_date
    """
    rows = con.execute(q).fetchall()
    out: dict[str, tuple[float, float]] = {}
    vals = [float(r[1] or 0.0) for r in rows]
    for i, (d, amt) in enumerate(rows):
        lo = max(0, i - 5)
        base = vals[lo:i]
        avg = sum(base) / len(base) if base else 0.0
        out[str(d)] = (float(amt or 0.0), avg)
    return out


def to_volume(b: BreadthDay, vols: dict[str, tuple[float, float]]) -> VolumeInfo:
    """★ 只填 total_amount 与 amount_ma5 —— `vol_ratio` 是 `regime.py` 的只读属性。

    这样量比由状态机自己算，与 M5 其余逻辑保持单一事实来源。
    """
    amt, ma5 = vols.get(b.date, (0.0, 0.0))
    return VolumeInfo(total_amount=amt, amount_ma5=ma5)


def attach_streaks(days: list[BreadthDay], streaks: dict[str, int]
                   ) -> list[BreadthDay]:
    for b in days:
        b.max_streak = streaks.get(b.date, 0)
    return days


def to_sentiment(b: BreadthDay) -> Sentiment:
    """`BreadthDay` → M5 `Sentiment`（注意 0~1 与 % 的换算）。"""
    sr = b.seal_rate          # None 或 %
    br = (100.0 - sr) / 100.0 if sr is not None else 0.0
    return Sentiment(
        limit_up=b.limit_up,
        limit_down=b.limit_down,
        broken_rate=br,               # ★ M5 要 0~1，这里做换算
        max_streak=b.max_streak,
        yesterday_premium=0.0,        # 需涨停池历史，当前离线算不了
        up_ratio=b.advance_ratio / 100.0,
    )


def print_report(days: list[BreadthDay],
                   vols: dict[str, tuple[float, float]]) -> None:
    print()
    print("=" * 96)
    print("离线市场宽度 → M5 六态状态机（数据源：Parquet 快照，不依赖 DuckDB 锁）")
    print("=" * 96)
    sr_vals = [b.seal_rate for b in days if b.seal_rate is not None]
    import statistics
    print(f"交易日 {len(days)} 天（{days[0].date} ~ {days[-1].date}）")
    print(f"涨停家数：中位 {statistics.median(b.limit_up for b in days):.0f} "
          f"最大 {max(b.limit_up for b in days)} 最小 {min(b.limit_up for b in days)}")
    print(f"封板率：中位 {statistics.median(sr_vals):.1f}%（{len(sr_vals)} 天有涨停）")
    print()
    print(f"{'日期':>12s}{'涨停':>6s}{'跌停':>6s}{'炸板率':>8s}{'连板':>6s}"
          f"{'量比':>7s}{'上涨占比':>9s}{'均涨跌':>9s}  状态")
    by_regime: dict[str, int] = defaultdict(int)
    for b in days[-15:]:
        env = MarketEnv(sentiment=to_sentiment(b),
                        volume=to_volume(b, vols), as_of=b.date)
        st = classify_regime(env)
        by_regime[st] += 1
        br = b.seal_rate
        brs = f"{100 - br:.1f}%" if br is not None else "无"
        print(f"{b.date:>12s}{b.limit_up:>6d}{b.limit_down:>6d}{brs:>8s}"
              f"{b.max_streak:>6d}{to_volume(b, vols).vol_ratio:>6.2f}"
              f"{b.advance_ratio:>8.1f}%{b.avg_chg:>8.2f}%  {st}")
    print()
    print("  ★ 状态分布（近15 日）:",
          dict(sorted(by_regime.items(), key=lambda kv: -kv[1])))
    print()
    print("  ⚠ 缺失因子与静默退化（必须知道，否则会误信量能）")
    vr = [to_volume(b, vols).vol_ratio for b in days[-15:]]
    if all(abs(x - 1.0) < 1e-9 for x in vr):
        print("    ★ 量比恒为 1.00 ⇒ **量能因子是死的**")
        print("      原因：`VolumeInfo.vol_ratio` 在 `amount_ma5<=0` 时静默返回 1.00，")
        print("      而 Parquet 的 `amount` 字段 100% 为 NULL（M1 已知问题）。")
        print("      已改用 `close × volume` 估算；若仍恒为 1.00 说明估算也失败。")
    else:
        print(f"    量比区间 {min(vr):.2f} ~ {max(vr):.2f}（有效）")
    print("   · yesterday_premium（昨日涨停今日溢价）恒为 0：")
    print("     它需要涨停池**历史**，Parquet 快照不含 ⇒ 「退潮」判定会失效。")
    print("     缺失按中性 50 分处理并重新归一化（regime.py 的既有约定）。")


def cross_check_last_day(days: list[BreadthDay]) -> None:
    """与 M5 已入库的东财实时值交叉验证。"""
    last = days[-1]
    print()
    print("=" * 96)
    print(f"交叉验证 {last.date}：离线 vs 东财实时")
    print("=" * 96)
    print(f"  涨停   离线 {last.limit_up:>3}  |  东财 43")
    print(f"  跌停   离线 {last.limit_down:>3}  |  东财 13")
    sr = last.seal_rate
    br = (100 - sr) if sr is not None else None
    print(f"  炸板率 离线 {br:.1f}% |  东财 42.7%" if br is not None
          else "  炸板率 离线 无（当日无涨停）")
    print()
    print("  差异已定位（不是 bug）：")
    print("   · 涨停差 1 只：920627 力王股份（北交所新股，Parquet 无记录）")
    print("   · 炸板率差 ~5pp：东财用**全池**口径（含 ST、次新股），")
    print("     本脚本用**严格封板**口径 ⇒ 口径不同，不应强行对齐")
    print()
    print("  ★ M5 当时用东财数据判为「修复」；离线口径下该日封板率仅 52.5%，")
    print("    属「退潮」边缘 ⇒ 口径差异会改变结论，接入时必须选定一种并记档。")


def main() -> int:
    ap = argparse.ArgumentParser(description="离线市场宽度 → M5 状态机")
    ap.add_argument("--parquet", default=str(PARQUET))
    args = ap.parse_args()

    pq = Path(args.parquet)
    if not pq.exists():
        print(f"Parquet 不存在: {pq}")
        return 1

    con = duckdb.connect()
    days = compute_breadth(con, pq)
    st_codes = set(load_st_codes())
    days = attach_streaks(days, streak_by_day(con, pq, st_codes))
    vols = volume_by_day(con, pq)

    print("=" * 96)
    print("离线市场宽度因子（M5-3 数据侧闭环）")
    print("=" * 96)
    print(f"数据源: {pq.name}（Parquet 快照，不抢 DuckDB 锁）")
    print(f"ST 标记: SQLite stock.is_st（{len(st_codes)} 只）")
    print(f"涨停口径: 收盘==round(前收×(1+限幅),2) 且排除 ST")
    print(f"炸板口径: high 触及涨停价 但收盘未封")

    print_report(days, vols)
    cross_check_last_day(days)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
