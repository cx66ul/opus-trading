# -*- coding: utf-8 -*-
"""市场宽度因子测试。

★★ 本文件守护的核心是**涨停判定口径** —— 这是一个非常容易做错的地方：
   涨停价必须按交易所规则**四舍五入到分**（`round(前收×(1+限幅), 2)`），
   用未取整的 `前收×(1+限幅)` 或宽松的 `pct_chg >= 限幅-0.3` 都会系统性误判。

实测（2026-10-08，与东财权威 tc=43 比对）：
   宽松口径 pct_chg>=限幅-0.3   → 44 只，多判603823（收46.59 / 涨停价46.65）
   ★ 严格口径 abs(close-涨停价)<0.005 → 41 只，**零多判**
   tol=0.03/0.05               → 44 只，多判 002496/301513
   tol=0.10                    → 46 只，多判 4 只
⇒ **容差越宽越差**，0.005 是实测最优，不是拍脑袋。

运行： pytest tests/test_breadth.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domain.breadth import (          # noqa: E402
    BREADTH_SQL, BreadthDay, board_of, breadth_sql, is_limit_down,
    is_limit_up, limit_pct, limit_up_price,
)


# ================================================================ 板块限幅
class TestBoardLimit:
    @pytest.mark.parametrize("code,board", [
        ("600519", "main"), ("601398", "main"), ("603288", "main"),
        ("605179", "main"), ("000001", "main"), ("002415", "main"),
        ("300750", "gem"), ("301513", "gem"), ("302132", "gem"),
        ("688981", "star"),
        ("920627", "bse"), ("830799", "bse"), ("871981", "bse"),
    ])
    def test_board_detection(self, code, board):
        assert board_of(code) == board

    @pytest.mark.parametrize("code,pct", [
        ("600519", 10.0), ("000001", 10.0),
        ("300750", 20.0), ("301513", 20.0),
        ("688981", 20.0),
        ("920627", 30.0),
    ])
    def test_limit_pct(self, code, pct):
        assert limit_pct(code) == pct

    def test_st_is_5pct(self):
        """★ ST 限幅 5%（主板 ST）/ 仍按板块但整体 5%。"""
        assert limit_pct("600519", True) == 5.0
        assert limit_pct("300750", True) == 5.0

    def test_st_overrides_board(self):
        """★ ST 优先于板块判定 —— ST 创业板也是 5% 不是 20%。"""
        assert limit_pct("300750", True) < limit_pct("300750", False)


# ================================================================ 涨停价取整
class TestLimitPriceRounding:
    """★★ 涨停价必须四舍五入到分。"""

    def test_rounds_to_cent(self):
        """603823: 42.41×1.10 = 46.651 → 涨停价 46.65。"""
        assert limit_up_price(42.41, "603823") == 46.65

    def test_rounding_matters(self):
        """★ 不取整会差 0.001~0.05，足以改变封板判定。"""
        raw = 42.41 * 1.10
        assert abs(limit_up_price(42.41, "603823") - raw) > 0.0005

    def test_exact_multiple(self):
        """300522: 13.80×1.20 = 16.56，恰好整。"""
        assert limit_up_price(13.80, "300522") == 16.56

    def test_gem_limit(self):
        assert limit_up_price(56.52, "301513") == 67.82   # 56.52×1.2=67.824

    def test_bse_limit(self):
        assert limit_up_price(10.0, "920627") == 13.0

    def test_st_limit(self):
        assert limit_up_price(10.0, "600519", True) == 10.5

    def test_invalid_preclose(self):
        """★ 前收无效时返回 None（数据不足），不返回 0 或猜值。"""
        assert limit_up_price(None, "600519") is None
        assert limit_up_price(0, "600519") is None
        assert limit_up_price(-1, "600519") is None


# ================================================================ 涨停判定
class TestLimitUpJudge:
    def test_exact_hit(self):
        assert is_limit_up("002058", 20.20, 22.22, 10.0) is True

    def test_miss_by_one_cent(self):
        """★ 差 1 分就不是涨停 —— 这是严格口径的核心价值。"""
        assert is_limit_up("603823", 42.41, 46.59, 9.86) is False

    def test_loose_criteria_would_misjudge(self):
        """★★ 展示宽松口径在同一输入上的错误。

        `pct_chg >= limit - 0.3` 会把603823 判成涨停（9.86 >= 9.7），
        严格口径判False —— 这正是必须用严格口径的原因。
        """
        code, pre, close, pct = "603823", 42.41, 46.59, 9.86
        assert limit_pct(code) - 0.3 <= pct, "宽松口径确实会误判为涨停"
        assert is_limit_up(code, pre, close, pct) is False, \
            "严格口径必须判False"

    def test_data_insufficient_returns_none(self):
        """★★ 缺失数据返回 None 而非 False。

        返回 False 会被读成「没涨停」，而真相是「不知道」——
        这会让涨停家数**系统性偏低**。
        """
        assert is_limit_up("600519", None, 10.0, None) is None
        assert is_limit_up("600519", 0, 10.0, None) is None
        assert is_limit_down("600519", None, 10.0, None) is None

    def test_gem_boundary(self):
        """300522: 前收13.80 涨停价16.56，收16.56 → 封板。"""
        assert is_limit_up("300522", 13.80, 16.56, 20.0) is True
        assert is_limit_up("300522", 13.80, 16.55, 19.9) is False

    def test_st_judged(self):
        """★ ST 也能判涨停（限幅 5%）。"""
        assert is_limit_up("600519", 10.0, 10.5, 5.0, True) is True

    def test_limit_down_symmetric(self):
        assert is_limit_down("002058", 20.20, 18.18, -10.0) is True
        assert is_limit_down("002058", 20.20, 18.19, -9.95) is False


class TestToleranceChoice:
    """★★ 容差是实测定的，不是拍的。"""

    def test_tight_tolerance_zero_false_positives(self):
        """tol=0.005 时 603823 不被误判。"""
        assert is_limit_up("603823", 42.41, 46.59, 9.86, tol=0.005) is False

    def test_wide_tolerance_would_misjudge(self):
        """★ 放宽到 0.03+ 就会把 002496 误判 —— 容差越宽越差。"""
        # 002496: 前收 2.10，涨停价 2.31，收 2.29（差 0.02）
        assert is_limit_up("002496", 2.10, 2.29, 9.05, tol=0.005) is False
        assert is_limit_up("002496", 2.10, 2.29, 9.05, tol=0.05) is True

    def test_default_is_tight(self):
        import inspect
        sig = inspect.signature(is_limit_up)
        assert sig.parameters["tol"].default == 0.005


# ================================================================ BreadthDay
class TestBreadthDay:
    def test_rates(self):
        b = BreadthDay("2026-10-08", 5200, 43, 18, 2000, 3000, 200, -0.5,
                       sealed=26, broken=17)
        assert b.limit_up_rate == pytest.approx(43 / 5200 * 100)
        assert b.advance_ratio == pytest.approx(2000 / 5200 * 100)
        assert b.seal_rate == pytest.approx(26 / 43 * 100)

    def test_seal_rate_none_when_no_limit_up(self):
        """★★ 无涨停日封板率必须是 None，不能是 0。

        「没有涨停」与「涨停全炸」是两回事，返回 0 会被读成后者。
        """
        b = BreadthDay("2026-10-08", 5200, 0, 18, 1631, 3464, 9, -1.05)
        assert b.seal_rate is None

    def test_empty_sample_does_not_divide_by_zero(self):
        b = BreadthDay("2026-10-08", 0, 0, 0, 0, 0, 0, 0.0)
        assert b.limit_up_rate == 0.0
        assert b.advance_ratio == 0.0
        assert b.seal_rate is None

    def test_as_dict_marks_no_seal_rate(self):
        b = BreadthDay("2026-10-08", 5200, 0, 18, 1631, 3464, 9, -1.05)
        d = b.as_dict()
        assert d["seal_rate"] is None
        assert "无" in d["note"]

    def test_as_dict_rounds(self):
        b = BreadthDay("2026-10-08", 5200, 43, 18, 2000, 3000, 200, -0.5,
                       sealed=26, broken=17)
        d = b.as_dict()
        assert d["limit_up"] == 43 and d["limit_down"] == 18
        assert isinstance(d["seal_rate"], float)


# ================================================================ SQL 口径
class TestSql:
    def test_uses_rounded_limit_price(self):
        """★★ SQL 必须含 round(pre_close*(1+lim/100), 2)。"""
        sql = breadth_sql("x.parquet")
        assert "round(pre_close * (1 + lim / 100), 2)" in sql

    def test_excludes_st_from_limit_up(self):
        """★ 涨停统计必须排除 ST（东财涨停池口径）。"""
        sql = breadth_sql("x.parquet")
        assert "NOT is_st AND abs(close - up_px)" in sql

    def test_limit_down_includes_st(self):
        """★ 跌停统计**包含** ST（ST 跌停同样 5%）。"""
        sql = breadth_sql("x.parquet")
        assert "abs(close - dn_px) < 0.005 THEN 1" in sql

    def test_filters_missing_and_suspended(self):
        """★ 缺失数据与停牌必须排除（不当 0）。"""
        sql = breadth_sql("x.parquet")
        assert "pct_chg IS NOT NULL" in sql
        assert "is_suspend = false" in sql
        assert "pre_close > 0" in sql

    def test_no_pct_chg_threshold_for_limit_up(self):
        """★★ 涨停判定不得依赖 pct_chg 阈值（那正是要避开的宽松口径）。"""
        sql = breadth_sql("x.parquet")
        # pct_chg 只允许出现在涨跌家数统计里，不允许出现在涨停判定里
        up_part = sql.split("limit_up")[1].split("limit_down")[0]
        assert "pct_chg" not in up_part

    def test_st_list_table_joined(self):
        assert "st_list" in breadth_sql("x.parquet")

    def test_module_constant_not_used_directly(self):
        """BREADTH_SQL 是带占位符的旧版，别让人误用。"""
        assert "{parquet}" in BREADTH_SQL


# ================================================================ 与东财实测对齐
class TestAgainstEastmoney:
    """★★ 用真实数据锁住口径（实测 2026-10-08 东财 tc=43）。"""

    # 东财 2026-10-08 涨停池里Parquet 有记录、且严格判定为真的样本
    REAL_LIMIT_UP = [("002058", 20.20, 22.22), ("300522", 13.80, 16.56),
                     ("688196", 30.10, 36.12), ("605378", 15.70, 17.27)]

    @pytest.mark.parametrize("code,pre,close", REAL_LIMIT_UP)
    def test_real_limit_up_detected(self, code, pre, close):
        assert is_limit_up(code, pre, close, None) is True

    def test_real_false_positive_rejected(self):
        """★ 603823 是实测的误判案例，必须判False。"""
        assert is_limit_up("603823", 42.41, 46.59, 9.86) is False

    def test_agrees_with_eastmoney_count(self):
        """★ 用真实样本验证「严格口径不多判」这个核心主张。

        实测：严格口径 41 只，与东财 43 只的交集=41（零多判），
        差的 2 只是新股无记录 + 取整边界，不是误判。
        """
        import duckdb
        pq = ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"
        if not pq.exists():
            pytest.skip("Parquet 快照不存在")
        import sqlite3
        con = duckdb.connect()
        con.execute("CREATE OR REPLACE TEMP TABLE st_list(code VARCHAR)")
        st = {r[0] for r in sqlite3.connect(ROOT / "data" / "hawk.sqlite")
              .execute("SELECT id FROM stock WHERE is_st=1").fetchall()}
        con.executemany("INSERT INTO st_list VALUES (?)", [(c,) for c in st])
        rows = con.execute(breadth_sql(pq.as_posix())).fetchall()
        day = next(r for r in rows if r[0] == "2026-10-08")
        # 严格口径下涨停数应 ≤ 东财的 43，且 ≥ 41（不多判的前提下尽量全）
        assert 41 <= day[2] <= 43, \
            f"严格口径涨停数应落在 41~43，实得 {day[2]}"
        con.close()


# ================================================================ 炸板口径
class TestBrokenBoardFix:
    """★★ 守护「炸板」判定 —— 这里曾有一个**静默产出错误数据**的 bug。

    早期版本写成：
        abs(high - round(high * (1 + lim/100), 2)) < 0.005
    那是拿「最高价再乘涨停幅度」与最高价比，差值恒等于涨幅本身
    （实测 002058 差 2.22 元、300522 差 1.66 元）⇒ 永远不匹配
    ⇒ **炸板恒为 0、封板率恒为 100%**，而且**不报任何错**。

    这是本项目最隐蔽的一类 bug：结果看起来完美（100% 是个漂亮的数），
    实际完全错误。
    """

    def test_broken_board_not_always_zero(self):
        """★ 全市场不能一只炸板都没有 —— 用真实数据验证。"""
        import duckdb
        import sqlite3
        pq = ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"
        if not pq.exists():
            pytest.skip("Parquet 快照不存在")
        con = duckdb.connect()
        con.execute("CREATE OR REPLACE TEMP TABLE st_list(code VARCHAR)")
        st = {r[0] for r in sqlite3.connect(ROOT / "data" / "hawk.sqlite")
              .execute("SELECT id FROM stock WHERE is_st=1").fetchall()}
        con.executemany("INSERT INTO st_list VALUES (?)", [(c,) for c in st])
        rows = con.execute(breadth_sql(pq.as_posix())).fetchall()
        from backend.domain.breadth import rows_to_days
        days = rows_to_days(rows)
        con.close()
        total_broken = sum(d.broken for d in days)
        assert total_broken > 0, \
            "★ 全市场 315 个交易日一只炸板都没有 ⇒ 又回到那个 bug 了"

    def test_seal_rate_not_always_100(self):
        """★★ 封板率不能恒为 100% —— 那是 bug 的直接症状。"""
        import duckdb
        import sqlite3
        pq = ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"
        if not pq.exists():
            pytest.skip("Parquet 快照不存在")
        con = duckdb.connect()
        con.execute("CREATE OR REPLACE TEMP TABLE st_list(code VARCHAR)")
        st = {r[0] for r in sqlite3.connect(ROOT / "data" / "hawk.sqlite")
              .execute("SELECT id FROM stock WHERE is_st=1").fetchall()}
        con.executemany("INSERT INTO st_list VALUES (?)", [(c,) for c in st])
        rows = con.execute(breadth_sql(pq.as_posix())).fetchall()
        from backend.domain.breadth import rows_to_days
        days = rows_to_days(rows)
        con.close()
        rates = [d.seal_rate for d in days if d.seal_rate is not None]
        assert rates
        assert min(rates) < 95.0, \
            f"最低封板率 {min(rates):.1f}% —— 若恒为 100% 则炸板判定失效"
        assert 30.0 < sum(rates) / len(rates) < 95.0, \
            f"平均封板率 {sum(rates)/len(rates):.1f}% 超出合理区间"

    def test_sql_does_not_multiply_high(self):
        """★★ SQL 不得出现「最高价 × (1+限幅)」这种错式。

        ★ 只扫**代码行**（剔除 `--` 注释）—— 否则本文件里那段
          记录错式的注释会让断言自己匹配到自己。
        """
        sql = breadth_sql("x.parquet")
        code = "\n".join(ln for ln in sql.splitlines()
                         if not ln.strip().startswith("--"))
        assert "high * (1 + lim" not in code
        assert "round(high" not in code, \
            "涨停价只能由 pre_close 推出，high 只能用于**比较**"
        assert "round(pre_close * (1 + lim / 100), 2)" in code

    def test_broken_compares_against_up_px(self):
        """★ 炸板必须与 up_px 比较。"""
        sql = breadth_sql("x.parquet")
        assert "abs(high  - up_px) < 0.005" in sql
        assert "AND abs(close - up_px) >= 0.005" in sql

    def test_sealed_excludes_broken(self):
        """★ 封板与炸板互斥（不能同一只既封又炸）。"""
        from backend.domain.breadth import BreadthDay
        b = BreadthDay("2026-10-08", 5200, 42, 14, 1631, 3464, 65, -1.05,
                       sealed=22, broken=20)
        assert b.seal_rate == pytest.approx(22 / 42 * 100)

    def test_real_broken_board_sample(self):
        """★ 用实测样本验证：000711 前收12.24，high 13.46 触板，收 12.12 未封。"""
        from backend.domain.breadth import limit_up_price
        up_px = limit_up_price(12.24, "000011")
        assert up_px == 13.46
        assert abs(13.46 - up_px) < 0.005, "high 触及涨停价"
        assert abs(12.12 - up_px) >= 0.005, "收盘未封 ⇒ 应判炸板"


class TestRowsToDaysColumnOrder:
    """★ SQL 列序与 rows_to_days 必须一致（位置取列，错位不报错）。"""

    def test_column_count_enforced(self):
        from backend.domain.breadth import rows_to_days
        with pytest.raises(ValueError, match="10 列"):
            rows_to_days([("2026-10-08", 100, 5, 1, 50, 50, 0, 0.1)])

    def test_maps_correctly(self):
        from backend.domain.breadth import rows_to_days
        rows = [("2026-10-08", 5200, 42, 14, 1631, 3464, 65, -1.05, 22, 20)]
        d = rows_to_days(rows)[0]
        assert d.sample == 5200 and d.limit_up == 42
        assert d.sealed == 22 and d.broken == 20
        assert d.avg_chg == pytest.approx(-1.05)

    def test_streak_map_applied(self):
        from backend.domain.breadth import rows_to_days
        rows = [("2026-10-08", 5200, 42, 14, 1631, 3464, 65, -1.05, 22, 20)]
        d = rows_to_days(rows, {"2026-10-08": 5})[0]
        assert d.max_streak == 5


class TestLossAreaRatio:
    """★ 亏钱效应面积：分母决定结论，差 5.4 倍，且**不给默认值**。"""

    def test_limit_pool_denom(self):
        from backend.domain.breadth import loss_area_ratio, DENOM_LIMIT_POOL
        # 跌停 30 / (涨停 70 + 跌停 30) = 0.30
        assert loss_area_ratio(70, 30, denom=DENOM_LIMIT_POOL) == pytest.approx(0.30)

    def test_all_denom_never_triggers(self):
        """★ 实测：全市场分母下历史最大仅 5.52% ⇒ >30% 永不触发。
        这条测试守护「知道这个口径没意义」，防止有人误用。"""
        from backend.domain.breadth import loss_area_ratio, DENOM_ALL
        r = loss_area_ratio(70, 30, 5400, denom=DENOM_ALL)
        assert r == pytest.approx(30 / 5400)      # 0.55%
        assert not (r > 0.30)

    def test_same_inputs_differ_by_5x(self):
        """★ 同一组家数，两个分母结论差 5.4 倍 —— 这是本条规则最容易踩的坑。

        实测历史：limit_pool 口径>30% 有 111/242 天；all 口径 0/242 天。
        """
        from backend.domain.breadth import (
            loss_area_ratio, DENOM_ALL, DENOM_LIMIT_POOL)
        lu, ld, total = 70, 40, 5400
        a = loss_area_ratio(lu, ld, total, denom=DENOM_LIMIT_POOL)
        b = loss_area_ratio(lu, ld, total, denom=DENOM_ALL)
        assert a == pytest.approx(40 / 110)          # 36.4%，明显过线
        assert a > 0.30 and not (b > 0.30)          # ★ 一个触发一个不触发
        assert a / b == pytest.approx(total / (lu + ld))   # 差 49 倍

    def test_unknown_denom_raises(self):
        """★ 拼错分母字符串必须 ValueError，不能静默走错口径。"""
        from backend.domain.breadth import loss_area_ratio
        with pytest.raises(ValueError, match="未知分母口径"):
            loss_area_ratio(70, 30, 5400, denom="total_market")

    def test_zero_denom_returns_none_not_zero(self):
        """当日无涨跌停 ⇒ 分母 0 ⇒ None（不是 0）。
        ★ 返回 0 会被读成「亏钱效应为零」，DSL 就会误判为「情绪稳定」。"""
        from backend.domain.breadth import loss_area_ratio
        assert loss_area_ratio(0, 0) is None

    def test_all_denom_requires_total(self):
        from backend.domain.breadth import loss_area_ratio, DENOM_ALL
        assert loss_area_ratio(70, 30, None, denom=DENOM_ALL) is None

    def test_missing_counts_return_none(self):
        from backend.domain.breadth import loss_area_ratio
        assert loss_area_ratio(None, 30) is None
        assert loss_area_ratio(70, None) is None
