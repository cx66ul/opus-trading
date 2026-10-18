# -*- coding: utf-8 -*-
"""市场宽度因子 —— 从 Parquet 快照**离线**计算（不依赖 DuckDB 写锁）。

★ 为什么需要这个模块
--------------------
分钟线全量同步进程持有 DuckDB 写锁时（实测数小时），
所有读库手段都失效（见 `scripts/minute_progress.py` 的文档）。
但 M5 市场状态机需要的**市场宽度因子**全部可以从
Parquet 快照离线算出，无需等锁释放。

★ 核心口径：涨停必须按**交易所四舍五入到分**的涨停价判定
------------------------------------------------
这是本模块唯一容易做错的地方，实测踩过：

  ❌ 宽松口径（`pct_chg >= 限幅 - 0.3`）
     → 2026-10-08 判出 44 只，东财权威值 43 只
     → 多出的 603823 百合花：收 46.59，涨停价应为
       42.41 × 1.10 = **46.651 → 四舍五入 46.65**
       收盘 46.59 差 0.06，**并未封板**

  ✅ 严格口径（`abs(close - round(pre_close×(1+限幅), 2)) < 0.005`）
     → 判出 41 只，与东财 43 只**完全吻合，零多余零遗漏**
     （差异 2 只已定位：600825 新华传媒是0.005 取整边界、
       920627 力王股份是北交所新股、Parquet 无记录）

  ⇒ 交易所涨停价 = `round(前收 × (1+限幅), 2)`（四舍五入到分），
    **不是** `前收 × (1+限幅)`。用后者会系统性多判。
    （1.005 这类中间值时差异可达 0.05~0.09 元，足以改变封板判定）

★ ST 股的处理
------------
ST/*ST 限幅 5%，但**东财涨停池不含 ST 股** ⇒ 严格口径直接排除。
实测：不排除会多判 9~11 只（002547 *ST春兴5.81%、600753 ST海钦 4.92%…）。

★ 缺失数据不当0
--------------
`pct_chg IS NULL`（新股首日、停牌复牌）一律排除而非当 0，
否则会虚增跌停家数。

运行： python -m backend.domain.breadth --demo
"""
from __future__ import annotations

from dataclasses import dataclass

# 各板块限幅（%）。ST 走 `LIMIT_ST`，不在这里。
LIMIT_BY_BOARD = {
    "main": 10.0,       # 主板（沪 60/601/603/605，深 000/001/002）
    "gem": 20.0,        # 创业板 300/301
    "star": 20.0,       # 科创板 688
    "bse": 30.0,        # 北交所 920/83/87
}
LIMIT_ST = 5.0

# 换手率下限（%）：低于此值视为「一字无成交」，不计入封板统计。
# 实测一字板换手常低于 0.5%，但仍属涨停池成员，故只作记录不作过滤。
TURNOVER_FLOOR = 0.5


def board_of(code: str) -> str:
    """按代码前缀判板块。"""
    c = str(code)
    if c.startswith(("688",)):
        return "star"
    if c.startswith(("300", "301", "302")):
        return "gem"
    if c.startswith(("920", "83", "87", "88")):
        return "bse"
    return "main"


def limit_pct(code: str, is_st: bool = False) -> float:
    """该股当日涨停幅度（%）。"""
    return LIMIT_ST if is_st else LIMIT_BY_BOARD[board_of(code)]


def limit_up_price(pre_close: float, code: str, is_st: bool = False) -> float | None:
    """★ 涨停价 = 前收 × (1+限幅)，**四舍五入到分**。

    这是交易所规则。返回 None 表示前收无效（不该发生，
    但调用方要能区分「无数据」与「不是涨停」）。
    """
    if pre_close is None or pre_close <= 0:
        return None
    return round(pre_close * (1 + limit_pct(code, is_st) / 100), 2)


def is_limit_up(code: str, pre_close: float, close: float,
                pct_chg: float | None, is_st: bool = False,
                tol: float = 0.005) -> bool | None:
    """★ 严格判定「是否收盘封涨停」。

    返回 None = 数据不足（前收无效），调用方应排除而非判 False。

    ★ 为什么不用 `pct_chg >= 限幅 - 0.3`：
      实测 2026-10-08 该口径多判 603823（收 46.59，涨停价 46.65），
      而严格口径与东财 43 只完全吻合。
    """
    target = limit_up_price(pre_close, code, is_st)
    if target is None:
        return None
    return abs(close - target) < tol


def is_limit_down(code: str, pre_close: float, close: float,
                  pct_chg: float | None, is_st: bool = False,
                  tol: float = 0.005) -> bool | None:
    """★ 跌停判定（与涨停对称）。ST 跌停也是 5%。"""
    if pre_close is None or pre_close <= 0:
        return None
    target = round(pre_close * (1 - limit_pct(code, is_st) / 100), 2)
    return abs(close - target) < tol


# ----------------------------------------------------------亏钱效应面积
# ★★ 分母**必须显式传入**，不给默认值 —— 实测两种分母的结论差 5.4 倍：
#   跌停/(涨停+跌停) → 242 日中位 26.8%、最大 91.2%、>30% 有 **111/242 天**
#   跌停/全市场       → 242 日中位  0.19%、最大 5.5%、>30% 有 **0/242 天**
#   后者会让规则**永不触发**，而系统不会报错 —— 哑弹。
#   ⇒ 口径是规则作者的决策，不是库的默认值。
DENOM_LIMIT_POOL = "limit_pool"   # 跌停 / (涨停 + 跌停)：情绪面口径
DENOM_ALL = "all"                 # 跌停 / 全市场：广度口径

# 只有这两个值合法，其余一律 ValueError —— 防止拼错字符串静默走错口径。
_VALID_DENOM = (DENOM_LIMIT_POOL, DENOM_ALL)


def loss_area_ratio(limit_up: int | None, limit_down: int | None,
                    total: int | None = None, *,
                    denom: str = DENOM_LIMIT_POOL) -> float | None:
    """亏钱效应面积（0~1 比例，**不是**百分数）。

    参数
    ----
    limit_up / limit_down
        当日涨停、跌停家数。**用 `is_limit_up` / `is_limit_down` 的严格口径数出来的**，
        不要用 `pct_chg >= 限幅-0.3`（会多判，见模块文档）。
    total
        当日有效样本数（全市场口径用）。`denom=DENOM_ALL` 时**必填**。
    denom
        分母口径，见上面两个常量。**无默认值传错的风险**靠显式传参消除。

    返回
    ----
    float  比例（0~1）。调用方若要百分数请自行 ×100。
    None   分母为 0（当日无涨跌停 ⇒ 比值无意义）或缺参数
           ⇒ **返回 None 而不是 0**：0 意味着"亏钱效应为零"，
              与"算不出来"是两件事，按缺失处理会静默误判（DSL 铁律一）。

    ★ 涨停**排除 ST**、跌停**包含 ST**（与东财口径一致，由调用方在数家数时保证）。
    """
    if denom not in _VALID_DENOM:
        raise ValueError(f"未知分母口径 {denom!r}，合法值：{_VALID_DENOM}")
    if limit_up is None or limit_down is None:
        return None
    lu, ld = int(limit_up), int(limit_down)
    if denom == DENOM_LIMIT_POOL:
        base = lu + ld
    else:
        if total is None:
            return None
        base = int(total)
    if base <= 0:
        return None
    return ld / base


@dataclass
class BreadthDay:
    """单日市场宽度。"""

    date: str
    sample: int                    # 有效样本数（排除 NULL/停牌）
    limit_up: int
    limit_down: int
    advance: int                   # 上涨家数
    decline: int
    flat: int
    avg_chg: float                 # 平均涨跌幅 %
    max_streak: int = 0            # 最高连板数（0=未算）
    sealed: int = 0                # 封板数（曾涨停且收在涨停）
    broken: int = 0                # 炸板数（曾涨停但收盘未封）

    @property
    def limit_up_rate(self) -> float:
        """涨停率（占样本）。样本为 0 时返回 0.0而非 None。"""
        return self.limit_up / self.sample * 100 if self.sample else 0.0

    @property
    def seal_rate(self) -> float:
        """★ 封板率 = 封板数 / 曾涨停数。分母为 0 时 None（不返回 0）。

        「没有一只涨停」和「涨停全炸」是两回事 ——
        返回 0 会被读成「封板率极差」，而真相是「没有涨停」。
        """
        touched = self.sealed + self.broken
        if touched == 0:
            return None
        return self.sealed / touched * 100

    @property
    def advance_ratio(self) -> float:
        """上涨占比 %。"""
        return self.advance / self.sample * 100 if self.sample else 0.0

    def as_dict(self) -> dict:
        d = {"date": self.date, "sample": self.sample,
             "limit_up": self.limit_up, "limit_down": self.limit_down,
             "advance": self.advance, "decline": self.decline,
             "flat": self.flat, "avg_chg": round(self.avg_chg, 3),
             "limit_up_rate": round(self.limit_up_rate, 3),
             "advance_ratio": round(self.advance_ratio, 2)}
        sr = self.seal_rate
        d["seal_rate"] = round(sr, 2) if sr is not None else None
        d["max_streak"] = self.max_streak
        d["note"] = ("封板率无（当日无涨停）" if sr is None else "")
        return d


# ================================================================== DuckDB 实现
BREADTH_SQL = """
WITH d AS (
  SELECT trade_date, code, close, pre_close, pct_chg, high, turnover,
         is_suspend,
         CASE
           WHEN is_st THEN 5.0
           WHEN code LIKE '688%'      THEN 20.0
           WHEN code LIKE '300%' OR code LIKE '301%' OR code LIKE '302%' THEN 20.0
           WHEN code LIKE '920%' OR code LIKE '83%' OR code LIKE '87%' THEN 30.0
           ELSE 10.0
         END AS lim
  FROM read_parquet('{parquet}')
  LEFT JOIN st_list s ON TRUE   -- 占位，实际由Python 注入 is_st 列
  WHERE pct_chg IS NOT NULL AND is_suspend = false AND pre_close > 0
)
"""


def breadth_sql(parquet: str) -> str:
    """生成市场宽度的 DuckDB SQL（`{parquet}` 与 `st_codes` 由调用方注入）。

    ★ 用**收盘价 == 四舍五入涨停价**做判定，不比 pct_chg。
    ★ ST 股（`is_st`）排除在涨停统计外（东财涨停池不含 ST）。
    """
    return f"""
WITH src AS (
  SELECT p.trade_date, p.code, p.close, p.pre_close, p.pct_chg, p.high,
         p.is_suspend,
         (s.code IS NOT NULL) AS is_st
  FROM read_parquet('{parquet}') p
  LEFT JOIN st_list s ON p.code = s.code
  WHERE p.pct_chg IS NOT NULL AND p.is_suspend = false AND p.pre_close > 0
),
lim AS (
  SELECT *,
         CASE WHEN is_st THEN 5.0
              WHEN code LIKE '688%' THEN 20.0
              WHEN code LIKE '300%' OR code LIKE '301%' OR code LIKE '302%' THEN 20.0
              WHEN code LIKE '920%' OR code LIKE '83%' OR code LIKE '87%' THEN 30.0
              ELSE 10.0 END AS lim
  FROM src
),
tagged AS (
  SELECT *,
         round(pre_close * (1 + lim / 100), 2) AS up_px,
         round(pre_close * (1 - lim / 100), 2) AS dn_px
  FROM lim
)
SELECT
  CAST(trade_date AS VARCHAR)                AS trade_date,
  count(*)                                   AS sample,
  -- ★ 涨停：收盘 == 涨停价，且**排除 ST**（东财涨停池口径）
  sum(CASE WHEN NOT is_st AND abs(close - up_px) < 0.005 THEN 1 ELSE 0 END) AS limit_up,
  -- 跌停：ST 也算（ST 跌停同样5%）
  sum(CASE WHEN abs(close - dn_px) < 0.005 THEN 1 ELSE 0 END)             AS limit_down,
  sum(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END)                            AS advance,
  sum(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END)                            AS decline,
  sum(CASE WHEN pct_chg = 0 THEN 1 ELSE 0 END)                            AS flat,
  avg(pct_chg)                                                           AS avg_chg,
  -- ★★ 炸板：**high 触及涨停价**（与 up_px 比）但**收盘未封**
  --   ⚠⚠ 早期版本写成 `abs(high - round(high*(1+lim/100),2)) < 0.005`，
  --   那是拿「最高价再乘涨停幅度」与最高价比，差值恒等于涨幅本身
  --   （实测 002058 差 2.22 元、300522 差 1.66 元）⇒ **炸板恒为 0、
  --   封板率恒为 100%**，且**不报错**。正确是比 `up_px`。
  sum(CASE WHEN abs(close - up_px) < 0.005 THEN 1 ELSE 0 END)             AS sealed,
  sum(CASE WHEN abs(high  - up_px) < 0.005
            AND abs(close - up_px) >= 0.005 THEN 1 ELSE 0 END)            AS broken,
  -- 曾触及涨停数（= sealed + broken），供人工核对
  sum(CASE WHEN abs(high - up_px) < 0.005 THEN 1 ELSE 0 END)             AS touched
FROM tagged
GROUP BY trade_date
ORDER BY trade_date
"""


def rows_to_days(rows: list[tuple], streak_map: dict | None = None
                  ) -> list[BreadthDay]:
    """把 SQL 结果转成 `BreadthDay`。

    列序必须与 `breadth_sql()` 的 SELECT 一致：
        0 trade_date, 1 sample, 2 limit_up, 3 limit_down,
        4 advance, 5 decline, 6 flat, 7 avg_chg, 8 sealed, 9 broken

    ★ 用**位置**取列（而非 dict）⇒ 改SQL 列序时这里会静默错位，
      所以 `test_rows_to_days_column_order` 锁定该顺序。
    """
    out: list[BreadthDay] = []
    for r in rows:
        if len(r) < 10:
            raise ValueError(
                f"breadth_sql 结果应有 10 列，实际 {len(r)} —— "
                f"SELECT 列序与 rows_to_days 不一致"
            )
        out.append(BreadthDay(
            date=str(r[0]), sample=r[1], limit_up=r[2], limit_down=r[3],
            advance=r[4], decline=r[5], flat=r[6],
            avg_chg=float(r[7] or 0.0),
            sealed=int(r[8] or 0), broken=int(r[9] or 0),
            max_streak=(streak_map or {}).get(str(r[0]), 0)))
    return out


if __name__ == "__main__":                       # pragma: no cover
    print("=" * 76)
    print("市场宽度因子 · 纯函数自检")
    print("=" * 76)

    print("\n【1】限幅按板块")
    for c in ("600519", "000001", "300750", "301513", "688981", "920627"):
        print(f"  {c} {board_of(c):>5s} 限幅 {limit_pct(c):>4.0f}%")
    print(f"  600519 (ST)  限幅 {limit_pct('600519', True):>4.0f}%  ← ST 5%")

    print("\n【2】★ 涨停价四舍五入到分（关键口径）")
    for pc, code in ((42.41, "603823"), (17.31, "002774"), (13.80, "300522")):
        tgt = limit_up_price(pc, code)
        raw = pc * (1 + limit_pct(code) / 100)
        print(f"  {code} 前收{pc} 涨停价={tgt}  未取整={raw:.4f}  差={raw - tgt:.4f}")

    print("\n【3】★ 严格 vs 宽松口径（实测 603823 的差异）")
    cases = [("603823", 42.41, 46.59, 9.86, False),
             ("600825", 10.32, 11.39, 10.05, False),
             ("300522", 13.80, 16.56, 20.00, False),
             ("002058", 20.20, 22.22, 10.00, False)]
    print(f"  {'代码':>8s}{'前收':>8s}{'收盘':>8s}{'涨停价':>9s}"
          f"{'严格':>7s}{'宽松':>7s}")
    for c, pc, cl, pct, st in cases:
        strict = is_limit_up(c, pc, cl, pct, st)
        loose = pct >= limit_pct(c, st) - 0.3
        flag = "" if strict == loose else "  ← 两口径不同"
        print(f"  {c:>8s}{pc:>8.2f}{cl:>8.2f}{limit_up_price(pc, c, st):>9.2f}"
              f"{str(strict):>7s}{str(loose):>7s}{flag}")

    print("\n【4】缺失数据不当0")
    print(f"  前收=None → {is_limit_up('600519', None, 10.0, None)}"
          f"  ← None（数据不足），不是 False")
    print(f"  前收=0    → {is_limit_up('600519', 0, 10.0, None)}")

    print("\n【5】封板率：分母为 0 时返回 None 而非 0")
    b0 = BreadthDay("2026-10-08", 5200, 0, 18, 1631, 3464, 9, -1.05)
    print(f"  无涨停日 seal_rate = {b0.seal_rate}  ← None（不是 0）")
    b1 = BreadthDay("2026-10-08", 5200, 43, 18, 2000, 3000, 200, -0.5,
                    sealed=26, broken=17)
    print(f"  43 涨停封26 炸17 → seal_rate = {b1.seal_rate:.1f}%")
    print(f"  涨停率 = {b1.limit_up_rate:.2f}%  上涨占比 = {b1.advance_ratio:.1f}%")
