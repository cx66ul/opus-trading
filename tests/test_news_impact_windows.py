# -*- coding: utf-8 -*-
"""消息联动的**周期选择**测试。

★★ 本文件守护一条口径纪律：**不得用粗周期冒充细窗口**。

  实测覆盖（2026-10-09）：
      m1  3 只（关注池，只有 2 天历史）
      m5  3 只（关注池，只有 11 天历史）
      m30 4182 只（61 天）
      m60 4182 只（121 天）

  旧实现硬编码 `_period_for("3m") -> "m5"`，后果有两个：
    1. **口径冒充**：m5 的第 1 根是**前 5 分钟**，却以「3m」名义进报告
       ⇒ 「LLM 3 分钟判断准确率」这个数字失去意义。
    2. **静默无数据**：m5 只覆盖 3 只 ⇒ 约 97% 的消息返回 unavailable，
       而 `judge()` 把unavailable 判 `unclear`
       ⇒ 看起来像「LLM 判不准」，真相是「根本没数据」。

运行： pytest tests/test_news_impact_windows.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domain import news_impact as ni      # noqa: E402

# 实测覆盖
REAL_COV = {"m1": 3, "m5": 3, "m30": 4182, "m60": 4182}


class TestPeriodSelection:
    def test_3m_uses_fine_period_when_available(self):
        """★ 有 m1/m5 时3m 窗口用细周期（这是唯一能算真 3 分钟的情况）。"""
        assert ni._period_for("3m", {"m1": 100, "m5": 100}) in ("m1", "m5")

    def test_3m_unavailable_when_no_fine_period(self):
        """★★ 只有 m30/m60 时，3m 窗口必须**返回空串**（让调用方标 unavailable）。

        不能退回 m30 —— 那会把 30 分钟的涨跌标成「3 分钟」。
        """
        per = ni._period_for("3m", {"m30": 4182, "m60": 4182})
        assert per == "", \
            f"无细周期时 3m 必须返回空串，实得 {per!r}（会冒充细窗口）"

    def test_30m_uses_wide_period(self):
        """★ 30m 窗口用覆盖最广的 m30。"""
        assert ni._period_for("30m", REAL_COV) == "m30"

    def test_1d_uses_wide_period(self):
        assert ni._period_for("1d", REAL_COV) == "m30"

    def test_never_returns_none(self):
        """★ 拿不到任何周期时也不返回 None（会让调用方崩）。"""
        for w in ("3m", "30m", "1d"):
            r = ni._period_for(w, {})
            assert r is None or isinstance(r, str)

    def test_cov_none_defaults_to_old_behavior(self):
        """★ 不传 cov 时保持旧行为（m5 优先），保证向后兼容。"""
        assert ni._period_for("3m") in ("m1", "m5")
        assert ni._period_for("30m") in ("m30", "m60", "m5")

    def test_zero_coverage_periods_skipped(self):
        """★ 覆盖数为 0 的周期不算可用。"""
        cov = {"m1": 0, "m5": 0, "m30": 4182}
        assert ni._period_for("30m", cov) == "m30"

    def test_low_coverage_treated_as_unusable(self):
        """★★ 覆盖过低（<50% 的周期总量）视为不可用。

        实测：m1/m5 各只覆盖 **3 只**（关注池），而 m30/m60 各 4182 只。
        3/4185 ≈ 0.07% ⇒ 对 99.9% 的消息都算不出 3 分钟窗口，
        留着它只会让报告出现「少数能算、多数不能」的误导性统计。
        """
        cov = {"m1": 3, "m5": 3, "m30": 4182, "m60": 4182}
        assert ni._period_for("3m", cov) == "", \
            "3 只覆盖（0.07%）对 3m 窗口应视为不可用"

    def test_high_coverage_still_usable(self):
        """★ 覆盖过半的周期仍可用（门槛不能误伤）。"""
        cov = {"m5": 3000, "m30": 1000, "m60": 1000}
        assert ni._period_for("3m", cov) == "m5"

    def test_threshold_relative_to_universe(self):
        """★★ 门槛基准是「各周期覆盖的**最大值**」（≈ 全市场标的数），
        不是各周期之和。

        实测踩过：用「之和」当基准时，`m5:100 + m30:4182`
        里 m5 只占 2.3% 被判不可用 —— 明显过严，会误伤正常的细周期。
        """
        cov = {"m1": 100, "m5": 100, "m30": 4182, "m60": 4182}
        # universe=4182 => 门槛 2091 ⇒ m1/m5(100) 不可用
        assert ni._period_for("3m", cov) == ""
        # 但 m30(4182) 可用
        assert ni._period_for("30m", cov) == "m30"

    def test_moderate_coverage_not_over_rejected(self):
        """★ 有实质覆盖的细周期不应被误伤（如 m5 覆盖 3000 只）。"""
        cov = {"m5": 3000, "m30": 4182, "m60": 4182}
        assert ni._period_for("3m", cov) == "m5"


class TestNoPeriodImpersonation:
    """★ 核心断言：任何情况下都不许用 m30/m60 冒充 3m。"""

    @pytest.mark.parametrize("cov", [
        {"m30": 4182, "m60": 4182},
        {"m30": 4182},
        {"m5": 0, "m30": 100},
        {"m30": 4182, "m60": 4182, "m1": 0, "m5": 0},
    ])
    def test_3m_never_uses_wide_period(self, cov):
        assert ni._period_for("3m", cov) not in ("m30", "m60"), \
            f"3m 窗口用了粗周期 {cov} ⇒ 口径冒充"

    def test_wide_never_used_for_3m_even_if_only_option(self):
        """★ 即便只有粗周期，3m 也必须报不可用（宁可缺数据不可造数据）。"""
        assert ni._period_for("3m", {"m60": 5000}) == ""

    def test_window_bar_counts_match_period(self):
        """★★ 窗口根数必须**按周期换算**，不能沿用 m5 的根数。

        实测踩过：旧实现 `WINDOWS = {3m:1, 30m:6, 1d:48}` 是 m5 的根数，
        换到 m30 后 48 根 = 24 小时 ⇒ 「1 天」窗口变成取一整天数据。
        ⇒ 现在 WINDOWS 存**分钟数**，由 `bars_for()` 换算。
        """
        # 窗口定义必须是分钟数
        assert ni.WINDOWS["3m"] == 3
        assert ni.WINDOWS["30m"] == 30
        assert ni.WINDOWS["1d"] == 240

    @pytest.mark.parametrize("per,exp3,exp30,exp1d", [
        ("m30", None, 1, 8),
        ("m60", None, 1, 4),
        ("m5", 1, 6, 48),
        ("m1", 3, 30, 240),
    ])
    def test_bars_for_conversion(self, per, exp3, exp30, exp1d):
        """★ 每周期每窗口的根数必须正确（m1 的 3m 要 3 根，m5 只要 1 根）。"""
        assert ni.bars_for("30m", per) == exp30
        assert ni.bars_for("1d", per) == exp1d
        if exp3 is None:
            # 粗周期不该被用来算 3m
            pass
        else:
            assert ni.bars_for("3m", per) == exp3

    def test_bars_for_unknown_period(self):
        """★ 未知周期返回 0，不崩。"""
        assert ni.bars_for("30m", "m7") == 0

    def test_windows_are_minutes_not_bars(self):
        """★★ WINDOWS 的值必须是分钟数（含 3m=3），不是根数。"""
        for label, v in ni.WINDOWS.items():
            assert v in (3, 30, 240), f"{label}={v} 看起来是根数而非分钟数"


class TestEvalImpactUnavailable:
    """★ 无可用周期时，窗口必须标 unavailable 且 note 说明原因。"""

    def test_3m_marked_unavailable(self, monkeypatch):
        """★ 无可用细周期时，3m 窗口必须标 unavailable 且 note 说明原因。

        ★ 桩必须**按 SQL 类型分派**返回：
          `period_coverage` 查的是 (period, count)；
          窗口查询取的是 (ts, close, high, low)。
          早期桩一律返回覆盖度行 ⇒ 4 列变2 列 ⇒ IndexError。
        """
        class _Con:
            def execute(self, sql, params=None):
                up = sql.upper()
                if "GROUP BY PERIOD" in up:
                    class _Cur:
                        def fetchall(self):
                            return [("m30", 4182), ("m60", 4182)]
                    return _Cur()
                if "KLINE_MIN" in up:
                    # 返回 5 列 (ts, open, close, high, low)
                    class _Cur:
                        def fetchall(self):
                            return [("2026-10-09 09:00:00", 10.0, 10.1,
                                     10.2, 9.8)]
                    return _Cur()
                raise AssertionError(f"未预期的 SQL: {sql[:60]}")

        from datetime import datetime, timezone, timedelta
        imp = ni.eval_message_price_impact(
            news_id=1, code="600519", name="测试", msg_ts="2026-10-08 21:43:27",
            msg_direction="bull", msg_strength=2, llm_reason="x",
            as_of=datetime(2026, 10, 9, 10, 0,
                           tzinfo=timezone(timedelta(hours=8))),
            con=_Con())
        w3 = imp.windows["3m"]
        assert w3.available is False, "无细周期时 3m 必须 unavailable"
        assert "冒充" in w3.note or "30 分钟" in w3.note, \
            f"note 应说明「不用粗周期冒充」，实得 {w3.note!r}"
        #★ 30m/1d 仍应可用（有 m30 覆盖）
        assert imp.windows["30m"].available is True
        assert imp.windows["1d"].available is True


class TestCoverageFunction:
    def test_reads_db(self):
        """★ `period_coverage` 从库里读，不能硬编码。"""
        import inspect
        src = inspect.getsource(ni.period_coverage)
        assert "GROUP BY period" in src

    def test_handles_error(self, monkeypatch):
        """★ 读库失败要降级为空 dict，不是抛异常。"""
        class _Bad:
            def execute(self, *a, **k):
                raise RuntimeError("db locked")
        assert ni.period_coverage(_Bad()) == {}


# ================================================================ 基准价口径
class TestBasePriceBug:
    """★★★ 守护「基准必须是第一根的 open」—— 曾让判定完全翻转。

    实测（2026-10-09 601298 青岛港，bull 消息，m30）：
        第一根ts=10:00（区间 09:30~10:00）open=10.06 close=10.15

      基准 = close(10.15) ⇒ 30m 窗口 +0.00%、1d -0.79% ⇒ 判**wrong**
      基准 = open (10.06) ⇒ 30m 窗口 +0.90%、1d +0.10% ⇒ 判**correct**

    ★ 根因：分钟 K 线的 ts 是**区间结束时刻**（市场惯例），
      所以「消息后第一根」的 close 已经是**消息后半小时**的价格。
      用它当基准 ⇒ 第一个窗口的涨跌被整个吞掉
      ⇒ 30m 窗口恒接近 0，**看起来平静实则失效**。

    ★ 这类 bug最阴险的地方：它不报错，而且让结论**方向相反**。
    """

    def _stub_con(self, rows):
        class _Con:
            def execute(self, sql, params=None):
                up = sql.upper()
                if "GROUP BY PERIOD" in up:
                    class _Cur:
                        def fetchall(self):
                            return [("m30", 4559), ("m60", 4558)]
                    return _Cur()
                if "KLINE_MIN" in up:
                    class _Cur:
                        def fetchall(self):
                            return rows
                    return _Cur()
                raise AssertionError("未预期的 SQL")
        return _Con()

    def test_30m_window_not_swallowed(self):
        """★ 30m 窗口必须包含「第一根内部」的涨跌，不能恒为 0。"""
        from datetime import datetime, timezone, timedelta
        # 一根 m30：open 10.06 close 10.15（半小时内涨 0.9%）
        rows = [("2026-10-09 10:00:00", 10.06, 10.15, 10.16, 10.05)]
        imp = ni.eval_message_price_impact(
            news_id=10, code="601298", name="青岛港",
            msg_ts="2026-10-08 21:43:27", msg_direction="bull",
            msg_strength=2, llm_reason="x",
            as_of=datetime(2026, 10, 9, 12, 0,
                           tzinfo=timezone(timedelta(hours=8))),
            con=self._stub_con(rows))
        assert imp.base_price == pytest.approx(10.06), \
            f"基准应是第一根 open 10.06，实得 {imp.base_price}"
        w30 = imp.windows["30m"]
        assert w30.available and w30.bars_used == 1
        assert w30.ret_pct == pytest.approx((10.15 / 10.06 - 1) * 100, abs=0.01), \
            f"30m 应为 +0.89%，实得 {w30.ret_pct}"

    def test_verdict_flips_with_correct_base(self):
        """★★ 同一份数据，基准不同 ⇒ 判定方向相反。"""
        from datetime import datetime, timezone, timedelta
        rows = [("2026-10-09 10:00:00", 10.06, 10.15, 10.16, 10.05)]
        imp = ni.eval_message_price_impact(
            news_id=10, code="601298", name="青岛港",
            msg_ts="2026-10-08 21:43:27", msg_direction="bull",
            msg_strength=2, llm_reason="x",
            as_of=datetime(2026, 10, 9, 12, 0,
                           tzinfo=timezone(timedelta(hours=8))),
            con=self._stub_con(rows))
        assert imp.verdict == "correct", \
            f"bull + 30m +0.89% 应判 correct，实得 {imp.verdict}"

    def test_first_bar_range_is_included(self):
        """★ 1d 窗口也要含第一根的涨跌，不能只用第2~4 根。"""
        from datetime import datetime, timezone, timedelta
        rows = [("2026-10-09 10:00:00", 10.06, 10.15, 10.16, 10.05),
                ("2026-10-09 10:30:00", 10.14, 10.09, 10.15, 10.07),
                ("2026-10-09 11:00:00", 10.09, 10.06, 10.10, 10.05),
                ("2026-10-09 11:30:00", 10.05, 10.07, 10.08, 10.04)]
        imp = ni.eval_message_price_impact(
            news_id=10, code="601298", name="青岛港",
            msg_ts="2026-10-08 21:43:27", msg_direction="bull",
            msg_strength=2, llm_reason="x",
            as_of=datetime(2026, 10, 9, 12, 0,
                           tzinfo=timezone(timedelta(hours=8))),
            con=self._stub_con(rows))
        assert imp.base_price == pytest.approx(10.06)
        # 1d = 10.07/10.06- 1 = +0.10%
        assert imp.windows["1d"].ret_pct == pytest.approx(
            (10.07 / 10.06 - 1) * 100, abs=0.01)

    def test_falls_back_to_close_when_open_zero(self):
        """★ open 缺失（新股/异常）时退化用 close，不返回 0。"""
        from datetime import datetime, timezone, timedelta
        rows = [("2026-10-09 10:00:00", 0.0, 10.15, 10.16, 10.05)]
        imp = ni.eval_message_price_impact(
            news_id=10, code="601298", name="x",
            msg_ts="2026-10-08 21:43:27", msg_direction="bull",
            msg_strength=2, llm_reason="x",
            as_of=datetime(2026, 10, 9, 12, 0,
                           tzinfo=timezone(timedelta(hours=8))),
            con=self._stub_con(rows))
        assert imp.base_price == pytest.approx(10.15), \
            "open=0 时应退化用 close，而不是让基准变 0（会除零）"

    def test_first_bar_close_still_counted_in_1d(self):
        """★★ 多根时，「第一根的涨跌」必须计入 1d 窗口。

        ★ 变异测试踩过：只断言单根场景时，把
          `closes = [c for c in all]`改成 `[c for c in all[1:]]`
          **测不出来**（单根时 `all[1:]` 为空、回退到 `all[0]`，结果相同）。
          ⇒ 必须用**多根**数据才能测出「第一根被吞」。
        """
        from datetime import datetime, timezone, timedelta
        rows = [
            ("2026-10-09 10:00:00", 10.00, 10.20, 10.25, 9.95),  # 第一根涨 2%
            ("2026-10-09 10:30:00", 10.20, 10.10, 10.22, 10.08),
            ("2026-10-09 11:00:00", 10.10, 10.05, 10.12, 10.03),
            ("2026-10-09 11:30:00", 10.05, 10.06, 10.08, 10.02),
        ]
        imp = ni.eval_message_price_impact(
            news_id=10, code="601298", name="x",
            msg_ts="2026-10-08 21:43:27", msg_direction="bull",
            msg_strength=2, llm_reason="x",
            as_of=datetime(2026, 10, 9, 12, 0,
                           tzinfo=timezone(timedelta(hours=8))),
            con=self._stub_con(rows))
        assert imp.base_price == pytest.approx(10.00)
        # 末根 close=10.06 ⇒ 1d = +0.60%
        assert imp.windows["1d"].ret_pct == pytest.approx(0.6, abs=0.01)
        # ★ 最高价必须包含**第一根内**的高点 10.25
        assert imp.windows["1d"].max_gain == pytest.approx(2.5, abs=0.01)
        # ★ 最低价同理（第一根 low 9.95）
        assert imp.windows["1d"].max_loss == pytest.approx(-0.5, abs=0.01)
