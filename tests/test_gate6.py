# -*- coding: utf-8 -*-
"""★ 六态 `market_regime` 门控的测试（本轮修掉的几个 bug 的守护）。

★★★ 这组测试防的是「结论看起来合理、实际口径全错」
---------------------------------------------------
写这个脚本时被三个 bug 绊住，症状**全部**表现为
「门控毫无影响」或「数字很荒唐」，而不是报错：

1. **止损单位**：`--stop-loss` 默认写了 `0.05` 而 `stop_loss_pct`
   要的是百分数（`5.0`）⇒ 变成 **0.05% 止损**，买入当天必然触价
   ⇒ 328 笔全部 `stop_loss`，每笔收益恒等于**往返成本 -0.9%**
   ⇒ 不同门控的统计值**完全相同**。
   ★ 这是本项目至今最隐蔽的一个 bug：**看到「门控全都一样」就该起疑**，
     我却先怀疑了三次别的（SQL 列名、连接类型、py 缓存）。

2. **SQL 选了不存在的列**：`board_strength` 无涨跌幅列，
   `strength AS pct` 会把排名百分位（0~100）显示成「电池 +98.4%」。

3. **胜率重复乘 100**：`stats_of` 返回的 `winrate` 已是 0~1 比例，
   再乘 100 ⇒ 「3058%」。

另有两个「样本代表性」问题：
4. `ORDER BY code LIMIT n`取到的全是 `920xxx` 北交所
5. **先 `ORDER BY code LIMIT scan` 再随机** —— 随机救不了偏池子
   （实测 scan=800 时池子 100% 是深市）

运行： pytest tests/test_gate6.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
SRC_PATH = ROOT / "scripts" / "run_regime_gate6.py"


@pytest.fixture(scope="module")
def g6():
    spec = importlib.util.spec_from_file_location("gate6_t", SRC_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["gate6_t"] = m
    spec.loader.exec_module(m)
    return m


# ================================================================== 止损单位
class TestStopLossUnits:
    """★★★ `stop_loss_pct` 的单位是**百分数**，不是比例。

    `backtest(stop_loss_pct=0.05)` = **0.05% 止损**，
    买入当天几乎必然触价 ⇒ 全部当止损出局、
    每笔收益恒等于往返成本（实测佣金0.05 + 滑点 0.4 + 印花税 0.05 ≈ 0.9%）。
    """

    def test_default_is_five_not_zero_point_five(self):
        """默认值必须是 5.0（D14 锁定 5% 止损），不是 0.05。"""
        import re

        src = SRC_PATH.read_text(encoding="utf-8")
        m = re.search(r'add_argument\("--stop-loss"[^)]*default=([\d.]+)',
                      src, re.S)
        assert m, "找不到 --stop-loss 定义"
        default = float(m.group(1))
        assert default == 5.0, (
            f"止损默认值应是 5.0（5%），实得 {default}。"
            f"若为 0.05 则变成 0.05% 止损 ⇒ 每笔都是当日止损、"
            f"收益恒等于往返成本，且不同门控统计值会完全相同。"
        )

    def test_help_text_warns_about_unit(self):
        src = SRC_PATH.read_text(encoding="utf-8")
        assert "不是 0.05" in src or "不是 0.05%" in src, \
            "帮助文本必须警告单位（这是本bug 的唯一入口）"


class TestBacktestStopLossSemantics:
    """★ 顺带确认引擎侧：`stop_loss_pct` 按百分数解释。"""

    def test_five_percent_stop_needs_five_percent_drop(self):
        """5% 止损不该在缓跌里触发。

        ★ 数据要造对：日跌 1% 时**第 5 天就会累计跌 5%**，止损理应触发。
        用 0.3%/天 ⇒ 需 17 天才跌到 5%，才能验证「不该触发」。
        """
        from backend.domain.backtest import Bar, CostModel, backtest

        # ★ 日期必须**单调递增且唯一**（原用 `i % 28 + 1` 会重复，
        #   于是 80 根只有 24 个交易日，序列被截断）。
        #   本项目多个地方按日期排序/取窗口，重复日期会静默出错。
        bars, px = [], 100.0
        d0 = datetime(2026, 1, 1, tzinfo=CST)
        for i in range(80):
            px *= 0.997
            bars.append(Bar(d=(d0 + timedelta(days=i)).strftime("%Y-%m-%d"),
                            o=px, h=px * 1.002, l=px * 0.998, c=px, v=1e6))

        held = {"i": 60}

        def entry(bar, i, allb):
            if i == 60 and held["i"] == 60:
                held["i"] = 61
                # ★ entry_date 用 bar.d：引擎有「入场日不得早于信号日」
                #   的防作弊守卫，写死日期会被 skipped
                return (bar.d, bar.o, (), ("test",), 1.0, None, None)
            return None

        r = backtest("X", bars, entry, stop_loss_pct=5.0, cost=CostModel())
        assert r.trades, "应产生交易"
        t0 = r.trades[0]
        # 5% 止损若触发，**实际跌幅必须接近 5%**（而不是 0.05%）
        drop = (1 - t0.exit_price / t0.entry_price) * 100
        assert drop > 4.5, (
            f"5% 止损的成交跌幅应≈5%，实得 {drop:.2f}%"
            f"（若只有 0.05% 左右，说明止损被当成了比例）"
        )
        assert t0.hold_days > 3, \
            f"缓跌 0.3%/天时 5% 止损该持有数天才触发，实得 {t0.hold_days} 天"

    def test_half_percent_stop_triggers_immediately(self):
        """★ 反证：0.05% 止损**必然**当天触发 —— 这就是原 bug 的机理。"""
        from backend.domain.backtest import Bar, CostModel, backtest

        bars, px = [], 100.0
        d0 = datetime(2026, 1, 1, tzinfo=CST)
        for i in range(80):
            px *= 0.99
            bars.append(Bar(d=(d0 + timedelta(days=i)).strftime("%Y-%m-%d"),
                            o=px, h=px * 1.002, l=px * 0.998, c=px, v=1e6))
        fired = {"i": 60}

        def entry(bar, i, allb):
            if i == 60 and fired["i"] == 60:
                fired["i"] = 61
                return (bar.d, bar.o, (), ("test",), 1.0, None, None)
            return None

        r = backtest("X", bars, entry, stop_loss_pct=0.05, cost=CostModel())
        assert r.trades, "应产生交易"
        t0 = r.trades[0]
        assert t0.exit_reason == "stop_loss", \
            ("0.05% 止损必然当天触发（滑点 0.2% 已超过它）"
             f"—— 实得 {t0.exit_reason}")
        # ★ 核心症状：收益恒等于**往返成本**，而不是「跌了 0.05%」
        #   （后者约 -0.3%：-0.05 止损 - 0.4 滑点）。恒定值是辨识特征。
        assert t0.ret_pct < -0.5, \
            f"0.05% 止损的一笔收益应接近往返成本（约 -0.9%），实得 {t0.ret_pct}"


# ================================================================== 前视偏差
class TestNoLookahead:
    """★★ 门控必须只用**决策日之前**的状态。

    `market_regime` 每行 ts 是当日 **15:00**（收盘后算出），
    而买点决策在 **09:30 开盘** ⇒ 当天的状态此刻尚未知。
    用`ts <= 当天` 就是「拿收盘后才知道的信息做开盘决策」。
    """

    def test_state_on_excludes_same_day(self, g6):
        gate = g6.RegimeGate([("2026-01-01", "start"),
                              ("2026-01-02", "frozen"),
                              ("2026-01-03", "repair")])
        # 2026-01-02 决策时，能用的是 01-01 的start
        assert gate.state_on("2026-01-02") == "start", \
            "决策日当天的状态不得使用（15:00 才算出，09:30 时未知）"
        assert gate.state_on("2026-01-03") == "frozen"

    def test_uses_most_recent_prior_day(self, g6):
        gate = g6.RegimeGate([("2026-01-01", "start"),
                              ("2026-01-05", "ferment")])
        assert gate.state_on("2026-01-04") == "start", "取最近的有状态日"
        assert gate.state_on("2026-01-06") == "ferment"

    def test_no_state_before_first_date(self, g6):
        gate = g6.RegimeGate([("2026-01-10", "start")])
        assert gate.state_on("2026-01-05") is None

    def test_missing_state_allows_trading(self, g6):
        """★ 查不到状态时**放行**，不是拦截。

        「没数据」≠「环境差」。若return False，
        状态表覆盖不足会变成「某些日子完全不出信号」——
        而原因不是市场不好，是数据缺失。这类静默的信号萎缩极难察觉。
        """
        gate = g6.RegimeGate([("2026-01-10", "start")])
        assert gate.allow("2026-01-05", ("start", "ferment")) is True
        assert gate.allow("2026-01-11", ("ferment",)) is False


# ================================================================== 覆盖率
class TestCoverage:
    """★ 门控太强会把信号全滤掉 —— 覆盖率必须能看到。"""

    def test_coverage_reported(self, g6):
        # 构造 30 天：前 20 天 start、后 10 天 frozen
        rows = []
        for i in range(30):
            rows.append((f"2026-01-{i + 1:02d}",
                         "start" if i < 20 else "frozen"))
        gate = g6.RegimeGate(rows)
        c = gate.coverage(("start",))
        assert c["allowed_pct"] == pytest.approx(200 / 3, abs=0.5), \
            f"start 应占 20/30 = 66.7%，实得 {c['allowed_pct']}"
        assert c["n_days"] == 30
        c2 = gate.coverage(("frozen",))
        assert c2["allowed_pct"] == pytest.approx(100 / 3, abs=0.5)

    def test_none_states_allows_everything(self, g6):
        gate = g6.RegimeGate([("2026-01-01", "start")])
        assert gate.coverage(None)["allowed_pct"] == 100.0


# ================================================================== 取样代表性
class TestSampleRepresentativeness:
    """★★ 取样必须代表市场，否则回测结论无意义。

    实测两个坑：
      1. `ORDER BY code LIMIT n` → 前 200 只全是 `920xxx` 北交所
      2. **先 `ORDER BY code LIMIT scan` 再随机** → 随机救不了偏池子
         （scan=800 时池子 100% 是深市 000/001/002）
    """

    def test_no_order_by_before_limit(self):
        """★ 只查**真正传给 execute 的 SQL**，不扫全文。

        本文件的 docstring 里就写着 `ORDER BY code LIMIT` 这个反例，
        朴素全文匹配会永远失败 —— 或反过来被注释骗过。
        """
        src = SRC_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)

        def _sql_of(node):
            """把「字符串常量」与 **f-string** 都还原成 SQL 文本。

            ★★ 必须处理 `ast.JoinedStr`（f-string）：
               本项目的 SQL 几乎都是 f-string（内插路径），
               只认 `ast.Constant` 会**完全抓不到** ——
               实测变异验证时 SQL 真的被改坏了，测试却仍然 pass。
               这是第二类假阳性测试（第一类是 `or` 短路恒真）。
            """
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                out = []
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        out.append(v.value)
                    elif isinstance(v, ast.FormattedValue):
                        out.append("{path}")
                return "".join(out)
            return None

        sqls = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executemany")
                    and node.args):
                s = _sql_of(node.args[0])
                if s:
                    sqls.append(s)
        assert sqls, "找不到 SQL 字面量"
        for s in sqls:
            up = s.upper()
            # ★ 逻辑必须是「两者同时出现才违规」，
            #   写 `A not in x or B not in x` 会短路恒真 ⇒ **假阳性测试**
            #   （变异验证发现：本断言在 SQL 真的被改坏时仍然 pass）。
            if "ORDER BY CODE" in up and "LIMIT" in up:
                pytest.fail(f"取码 SQL 不得先 ORDER BY code LIMIT：{s[:80]}")

    def test_pool_is_filtered_then_shuffled(self, g6):
        """★ 必须是「全库过滤 → 打乱 → 取 limit」。"""
        import duckdb

        pcon = duckdb.connect()
        try:
            if not g6.rb.PARQUET.exists():
                pytest.skip("Parquet 快照不存在")
            codes = g6.pick_codes(pcon, 800, 120)
            assert len(codes) > 0
            buckets = {}
            for c in codes:
                buckets[c[:3]] = buckets.get(c[:3], 0) + 1
            top = max(buckets.values()) / len(codes)
            assert top <= 0.6, (
                f"样本市场分布过度集中：{buckets}"
                f"（单一前缀占 {top:.0%}）⇒ 池子有偏"
            )
        finally:
            pcon.close()


# ================================================================== 门控方案
class TestGatePresets:
    """★ 门控方案必须**事先写死**，不做穷举。

    6 档两两组合 30 种，若「遍历所有组合挑样本外最好的」
    就等于把样本外当训练集（`run_oos` 已踩过）。
    """

    def test_gates_are_few_and_fixed(self, g6):
        assert len(g6.GATES) <= 6, \
            f"门控方案过多（{len(g6.GATES)} 个）⇒ 有「挑样本外最优」的风险"

    def test_none_baseline_exists(self, g6):
        assert "none" in g6.GATES
        assert g6.GATES["none"] is None

    def test_has_reverse_control(self, g6):
        """★ 必须有一个「预期最差」的对照档，用于验证门控确实在起作用。

        原用「高潮」做对照，但阈值重校准后 251 天里**高潮 0 天**
        ⇒ 放行 0%、无成交，对照形同虚设。改用「退潮」（4 天）。
        """
        ctl = [k for k in g6.GATES if k.endswith("_only")]
        assert ctl, "必须保留反向对照档"
        for name in ctl:
            states = g6.GATES[name]
            if states is None:
                continue
            assert len(states) <= 2, \
                f"{name} 放行 {len(states)} 档，不像反向对照"


# ================================================================== 显示口径
class TestDisplayFormatting:
    """★ `stats_of` 的 `winrate` 已是 0~1 比例，格式化时不能再乘 100。"""

    def test_no_double_multiplication(self):
        src = SRC_PATH.read_text(encoding="utf-8")
        # 允许差异展示里的 *100（那是 pp），但不允许单值展示再乘
        assert "wr * 100 if wr is not None else 0" not in src, \
            "单值胜率不得再乘 100（stats_of 已返回 0~1）"
        assert ":.1%" in src, "应用 {:.1%} 格式化比例"


if __name__ == "__main__":                        # pragma: no cover
    pytest.main([__file__, "-v"])