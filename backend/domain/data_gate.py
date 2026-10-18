# -*- coding: utf-8 -*-
"""★ 数据完备度闸门 —— 「这一天能不能用于横截面统计」的唯一判据。

★★★ 本项目最危险的一类缺陷（2026-10-09 发现）
------------------------------------------------
M1 日线同步是**渐进**的：先抓关注池，再扩全市场。
结果 `kline_daily` 里 **2025-06-19 ~ 2025-09-04 这 56 个交易日
每天只有 1~93 只股票**，而 2025-09-18 之后有 5000+ 只。

这批数据本身**没有错**（那 94 只股票当天的行情是真的），
错的是**任何按「全市场」口径做的横截面统计**：

  · `avg(close)` 等权指数：从 14.81（1 只）"涨"到 24.64（5000 只）
    ⇒ 伪造出一段 +66% 的"上涨行情"
  · `sum(close*volume)` 两市成交额：分母只有 1 只股票
    ⇒ 量比出现 **57.78** 的爆表值（真实区间 0.55~1.5）
  · 涨停家数 / 上涨家数占比：分母只有 1~20 只
    ⇒ 涨跌停结构、封板率全部失真

★ 已污染的既有结论（实测）
--------------------------
  `run_regime_split.market_segments` 首个「上涨」段
  2025-09-11~12-18 段内 60 日均值 **+69.78%** ——
  这不是行情，是**样本从 1 只股票扩大到全市场**。
  该段覆盖了 D16 结论「上涨市超额 +1.61% 最好」的主要样本。

★★ 为什么难以发现
------------------
不报错、不崩、结果「看起来合理」（上涨市确实好），
只是**好在一个不存在的原因上**。若不去逐日打印标的数，
永远不会有人发现前 56 天只有 1 只股票。

★ 闸门规则
----------
`MIN_UNIVERSE_FRAC = 0.5`：某日横截面标的数须≥ **全库最大标的数的 50%**
才可用于横截面统计。

为什么用**比例**而非固定值：
  · 固定 3000 会在数据规模变化（加自选池、减北交所）后失效
  · 比例口径自带"相对于本库完备水平"的语义，跨批次可比

为什么不直接删掉那 56 天：
  它们对**单票**分析（缠论、回测）完全有效 —— 002731 有 56 根连续 K线。
  只在**横截面**口径下排除，两种用途各取所需。

运行：
    python -m backend.domain.data_gate --report
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 某日横截面标的数 ≥全库最大标的数 × 本比例，才算「完备」
MIN_UNIVERSE_FRAC = 0.5

# 低于此绝对数量直接判不可用（哪怕全库本身很小）
MIN_UNIVERSE_ABS = 50


@dataclass
class CoverageReport:
    """横截面完备度报告。"""

    per_day: dict[str, int]                # 日期 → 当日标的数
    max_universe: int
    threshold: int
    usable: list[str]                      # 可用于横截面统计的日期（升序）
    unusable: list[str]

    @property
    def first_usable(self) -> str | None:
        return self.usable[0] if self.usable else None

    @property
    def last_usable(self) -> str | None:
        return self.usable[-1] if self.usable else None

    @property
    def n_usable(self) -> int:
        return len(self.usable)

    @property
    def n_unusable(self) -> int:
        return len(self.unusable)

    def is_usable(self, d: str) -> bool:
        return d in set(self.usable)

    def summary(self) -> dict:
        return {
            "max_universe": self.max_universe,
            "threshold": self.threshold,
            "n_days_total": len(self.per_day),
            "n_usable": self.n_usable,
            "n_unusable": self.n_unusable,
            "first_usable": self.first_usable,
            "last_usable": self.last_usable,
            "min_universe_frac": MIN_UNIVERSE_FRAC,
        }


def build_coverage(per_day: dict[str, int],
                   frac: float = MIN_UNIVERSE_FRAC,
                   floor: int = MIN_UNIVERSE_ABS) -> CoverageReport:
    """由「日期 → 当日标的数」构造完备度报告。

    `per_day` 的值必须是该日**横截面**的标的数（distinct code），
    不是行数 —— 一只股票一天一行，但停牌股会缺失，用distinct 才准确。
    """
    if not per_day:
        return CoverageReport({}, 0, 0, [], [])
    mx = max(per_day.values())
    th = max(int(mx * frac), floor)
    usable = sorted(d for d, n in per_day.items() if n >= th)
    unusable = sorted(d for d, n in per_day.items() if n < th)
    return CoverageReport(per_day, mx, th, usable, unusable)


def coverage_from_duck(con, table: str = "kline_daily") -> CoverageReport:
    """从 DuckDB 直接算完备度。"""
    rows = con.execute(
        f"SELECT CAST(trade_date AS VARCHAR), count(DISTINCT code) "
        f"FROM {table} GROUP BY trade_date ORDER BY trade_date"
    ).fetchall()
    return build_coverage({str(r[0]): int(r[1]) for r in rows})


def coverage_from_parquet(con, parquet: str | Path) -> CoverageReport:
    """从 Parquet 快照算完备度（不抢 DuckDB 锁）。"""
    p = Path(parquet)
    rows = con.execute(
        f"SELECT CAST(trade_date AS VARCHAR), count(DISTINCT code) "
        f"FROM read_parquet('{p.as_posix()}') GROUP BY trade_date ORDER BY trade_date"
    ).fetchall()
    return build_coverage({str(r[0]): int(r[1]) for r in rows})


# ================================================================== 口径改写
def equal_weight_index(con, parquet: str | Path, cov: CoverageReport | None = None,
                       min_universe: int | None = None) -> list[tuple[str, float]]:
    """★ 等权指数 —— **只用完备横截面**。

    这是 `run_regime_split.market_segments` 原本用的口径，
    但那里没有完备度过滤 ⇒ 前 56 天用 1 只股票的均价当"大盘"。

    ★★ 默认必须过滤：`cov`/`min_universe` 都不传时**自行统计完备度**，
      不是「不过滤」。若允许默认不过滤，调用方漏传参数就会静默
      退回污染口径 —— 这正是本模块要防的那类缺陷的翻版。
      真要不过滤请显式传 `min_universe=1`。

    ⚠ 等权指数与真实指数的差异（必须显式承认）：
      `avg(close)` 是**价格均值**，不是指数 —— 它受**股票数量**影响
      （新股低价、拉低均值；高价股退市…）且**不做基期归一**。
      60 日滚动的**比值**（vals[i]/vals[i-window]）能消掉常数倍偏移，
      所以涨跌百分比基本可用，但**绝不能对外披露其绝对值**。
      真实大盘请用指数行情（腾讯 sh000001）。
    """
    p = Path(parquet)
    if min_universe is None:
        min_universe = cov.threshold if cov is not None else (
            coverage_from_parquet(con, p).threshold)
    rows = con.execute(
        f"""
        SELECT CAST(trade_date AS VARCHAR) AS d, avg(close) AS eq, count(DISTINCT code) AS n
        FROM read_parquet('{p.as_posix()}')
        WHERE close > 0 AND is_suspend = false
        GROUP BY trade_date ORDER BY trade_date
        """
    ).fetchall()
    return [(str(r[0]), float(r[1])) for r in rows if int(r[2]) >= min_universe]


def daily_universe(con, parquet: str | Path) -> dict[str, int]:
    """日期 → 当日标的数（纯查询，供 `build_coverage`）。"""
    p = Path(parquet)
    rows = con.execute(
        f"SELECT CAST(trade_date AS VARCHAR), count(DISTINCT code) "
        f"FROM read_parquet('{p.as_posix()}') GROUP BY trade_date"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


if __name__ == "__main__":                       # pragma: no cover
    import json

    import duckdb

    from backend.data import duck as _duck

    print("=" * 84)
    print("数据完备度闸门 · 自检")
    print("=" * 84)

    pq = _ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"
    con = duckdb.connect()
    cov = coverage_from_parquet(con, pq)
    s = cov.summary()
    print(json.dumps(s, ensure_ascii=False, indent=2))

    print()
    print(f"⚠ 不可用于横截面统计的 {cov.n_unusable} 天（前 12 个）:")
    for d in cov.unusable[:12]:
        print(f"    {d}  仅 {cov.per_day[d]:>4d} 只")
    if cov.n_unusable > 12:
        print(f"    ...共 {cov.n_unusable} 天")
    print()
    idx = equal_weight_index(con, pq, cov)
    print(f"★ 过滤后等权指数样本 {len(idx)} 天: "
          f"{idx[0][0]} ({idx[0][1]:.2f}) → {idx[-1][0]} ({idx[-1][1]:.2f})")
    bad = equal_weight_index(con, pq, min_universe=1)
    print(f"  （不过滤会有 {len(bad)} 天，起始 {bad[0][0]} 等权 {bad[0][1]:.2f}）")
    print()
    print("  ⇒ 不做闸门时，那段 1→5000 只的样本扩张会被误读为一段大涨。")
    con.close()
