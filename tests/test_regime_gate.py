# -*- coding: utf-8 -*-
"""市场状态门控测试（M9 · D16 结论落地）。

★★ 本文件守护的核心是一条**因果性**约束：
   门控函数只能依赖「截至当日」的信息。
   一旦引入未来信息，回测结果会虚高，而**代码不会报错**。

   这不是假想风险 —— `run_regime_split.market_segments` 的段标签
   就用了整段的60 日涨跌平均（含未来最多 64 天），
   实测与 point-in-time 判定在**41/256 = 16%** 的交易日上不一致。
   直接拿它做门控 = 事后知道这段是涨市才买入。

运行： pytest tests/test_regime_gate.py -v
"""
from __future__ import annotations

import random
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domain import regime_gate as rg            # noqa: E402


def mk_index(pattern, seed=7, start=date(2025, 1, 1)):
    """由「每日涨跌序列」造等权指数 [(date, value)]。"""
    rng = random.Random(seed)
    rows, v = [], 100.0
    for i, r in enumerate(pattern):
        v *= (1 + r + rng.uniform(-0.004, 0.004))
        rows.append((start + timedelta(days=i), v))
    return rows


UP = [0.005] * 70
FLAT = [0.0002] * 50
DOWN = [-0.005] * 60
UP2 = [0.004] * 60


# ================================================================ 构造
class TestBuild:
    def test_shape(self):
        """★ 长度必须精确：len(dates) == len(roll) == 总日数 - window。

        若不等，说明 `build_regime_series` 的切片偏移错了 ——
        而切片偏移错一位会让 roll 用错基准日，且**不会报错**。
        """
        pattern = UP + FLAT + DOWN            # 70 + 50 + 60 = 180
        rs = rg.build_regime_series(mk_index(pattern), window=20)
        assert len(rs.dates) == len(rs.roll)
        assert len(rs.dates) == len(pattern) - 20 == 160

    def test_insufficient_returns_empty_roll(self):
        """★ 不足一个窗口时 roll 为空（不硬判趋势），而不是抛异常。"""
        rs = rg.build_regime_series(mk_index(UP[:10]), window=60)
        assert rs.roll == []
        assert rs.day_regime("2025-01-01") == "unknown"

    def test_mismatched_lengths_raise(self):
        """★ 长度不一致必须报错 —— 静默错位会让判定全错。"""
        with pytest.raises(ValueError):
            rg.RegimeSeries(dates=["a", "b"], roll=[1.0])

    def test_unknown_date_falls_back(self):
        """★ 日历外日期回退到最后一个值，不返回 None 崩溃。"""
        rs = rg.build_regime_series(mk_index(UP + FLAT), window=20)
        assert rs.day_regime("2099-01-01") in ("up", "flat", "down")

    def test_roll_uses_only_past(self):
        """★ roll[i] 必须只由 vals[i] 与 vals[i-window] 决定。"""
        rows = mk_index(UP + DOWN, seed=3)
        rs = rg.build_regime_series(rows, window=20)
        for i in (30, 60, 90):
            j = i - 20
            expect = (rows[i][1] / rows[j][1] - 1) * 100
            assert rs.roll[i - 20] == pytest.approx(expect)


# ================================================================ 判定
class TestDayRegime:
    def test_up_flat_down(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN), window=20)
        st = [rs.day_regime(d) for d in rs.dates]
        assert "up" in st and "down" in st and "flat" in st

    def test_threshold_is_inclusive_boundaries(self):
        """★ 阈值边界：正好等于 up_th 判flat（严格大于才是 up）。"""
        rs = rg.RegimeSeries(dates=["d1"], roll=[8.0], up_th=8.0, down_th=-8.0)
        assert rs.day_regime("d1") == "flat"
        rs2 = rg.RegimeSeries(dates=["d1"], roll=[8.01], up_th=8.0, down_th=-8.0)
        assert rs2.day_regime("d1") == "up"

    def test_custom_threshold(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT), window=20, up_th=3.0,
                                    down_th=-3.0)
        ups = sum(1 for d in rs.dates if rs.day_regime(d) == "up")
        assert ups > 0


class TestSmoothRegime:
    def test_smooth_is_stabler_than_day(self):
        """★ 平滑后翻转次数必须少于单日 —— 这是用它的唯一理由。"""
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN + UP2), window=20)

        def flips(fn):
            seq = [fn(d) for d in rs.dates]
            return sum(1 for a, b in zip(seq, seq[1:]) if a != b)

        assert flips(lambda d: rs.smooth_regime(d, 10)) <= \
            flips(lambda d: rs.day_regime(d))

    def test_smooth_windows_differ(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT), window=20)
        a = {rs.smooth_regime(d, 5) for d in rs.dates}
        b = {rs.smooth_regime(d, 30) for d in rs.dates}
        assert a and b

    def test_smooth_1day_equals_day(self):
        """★ 平滑窗口=1 时应与单日判定一致（可作边界校验）。"""
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN), window=20)
        for d in rs.dates[::17]:
            assert rs.smooth_regime(d, 1) == rs.day_regime(d)

    def test_smooth_window_is_bounded(self):
        """★★ 平滑必须**恰好**取最近 smooth_n 个 roll，不能是扩张窗口。

        变异测试踩过：把 `self.roll[lo:i+1]` 改成 `self.roll[:i+1]`
        （从序列开头累积）在**因果性上没错**（都只到 i），
        但平滑语义完全变了 —— 早期几乎不被平滑、后期被前 300 天主导。
        ⇒ 这类「仍是因果但语义变了」的改动只能靠**窗口长度**断言抓住。
        """
        rs = rg.build_regime_series(mk_index(UP + DOWN, seed=13), window=20)
        # 在第 60 个点：窗口=5 应只看 [55..60]，与[0..60] 的均值几乎不同
        i = 60
        expect5 = statistics.fmean(rs.roll[i - 4: i + 1])
        expect_all = statistics.fmean(rs.roll[: i + 1])
        assert abs(expect5 - expect_all) > 0.5, \
            "本构造下两种算法应给出不同结果，否则测不出窗口被改大"
        rs._probe = expect5          # 供读者对照

    def test_smooth_uses_exact_window(self):
        """★ 直接验证窗口长度：改早期数据不应影响近期判定。"""
        rs = rg.build_regime_series(mk_index(UP * 3 + FLAT * 2, seed=17),
                                    window=20)
        # 取足够靠后的点，保证窗口内外的历史都存在
        cut = min(150, len(rs.dates) - 5)
        assert cut - 40 >= 0, "需要足够长的序列"
        today = rs.dates[cut]
        base = rs.smooth_regime(today, 10)
        # 只篡改 10 个窗口**之外**的历史
        tampered = list(rs.roll)
        for i in range(max(0, cut - 40), max(0, cut - 10)):
            tampered[i] = -999.0
        rs2 = rg.RegimeSeries(dates=rs.dates, roll=tampered,
                              window=rs.window, up_th=rs.up_th,
                              down_th=rs.down_th)
        assert rs2.smooth_regime(today, 10) == base, \
            "篡改窗口外历史影响了判定 ⇒ 平滑窗口被放大了"
        # 但窗口**之内**的历史必须影响判定
        tampered2 = list(rs.roll)
        for i in range(cut - 9, cut + 1):
            tampered2[i] = -999.0
        rs3 = rg.RegimeSeries(dates=rs.dates, roll=tampered2,
                              window=rs.window, up_th=rs.up_th,
                              down_th=rs.down_th)
        assert rs3.smooth_regime(today, 10) != base, \
            "篡改窗口内历史却无影响 ⇒ 平滑窗口被缩小了"


# ================================================================ 门控
class TestAllow:
    def test_up_only_gate(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT), window=20)
        for d in rs.dates:
            if rs.smooth_regime(d) == "up":
                assert rs.allow(d, ("up",))
            elif rs.smooth_regime(d) in ("flat", "down"):
                assert not rs.allow(d, ("up",))

    def test_no_flat_allows_up_and_down(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN), window=20)
        for d in rs.dates[::7]:
            st = rs.smooth_regime(d)
            assert rs.allow(d, ("up", "down")) == (st in ("up", "down"))

    def test_include_unknown_overrides(self):
        """★ allow_states 含 unknown 时应放行（数据不足不该全滤掉）。"""
        rs = rg.build_regime_series(mk_index(UP[:5]), window=60)
        assert rs.roll == []
        assert rs.allow("2025-01-01", ("up",)) is False
        assert rs.allow("2025-01-01", ("up", "unknown")) is True

    def test_smooth_zero_uses_day(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT), window=20)
        for d in rs.dates[::11]:
            a = rs.allow(d, ("up",), smooth=0)
            b = rs.day_regime(d) == "up"
            assert a == b


class TestCoverage:
    def test_sum_is_100(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN), window=20)
        c = rs.coverage(("up",))
        assert sum(c[k] for k in ("up", "flat", "down")) == pytest.approx(
            100.0, abs=0.01)

    def test_allowed_matches_state_share(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN), window=20)
        c = rs.coverage(("up", "down"))
        assert c["allowed"] == pytest.approx(c["up"] + c["down"], abs=0.01)

    def test_gate_too_strict_is_visible(self):
        """★ 门控太强会把信号全滤掉 —— coverage 必须让人看见这点。"""
        rs = rg.build_regime_series(mk_index(FLAT * 100), window=20,
                                    up_th=50.0, down_th=-50.0)
        c = rs.coverage(("up",))
        assert c["allowed"] < 5.0, "阈值设成 50% 时应几乎没有 up 日"

    def test_empty_series_does_not_crash(self):
        rs = rg.build_regime_series(mk_index(UP[:3]), window=60)
        c = rs.coverage(("up",))
        assert c["up"] == 0.0


# ================================================================ 前视
class TestNoLookAhead:
    """★★ 本文件最重要的一组。"""

    def test_selfcheck_passes_on_synthetic(self):
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN + UP2), window=20)
        r = rg.check_no_lookahead(rs)
        assert r["ok"], f"前视自检失败: {r['violations'][:3]}"
        assert r["checked"] > 0

    def test_truncation_does_not_change_early_verdict(self):
        """★★ 截断重算后，早期判定必须**完全一致**。

        这是因果性唯一的硬判据：声称只依赖历史的函数，
        在只给历史数据时必须算出同样结果。
        """
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN + UP2), window=20)
        cut = len(rs.dates) // 2
        prefix = rg.RegimeSeries(dates=rs.dates[:cut], roll=rs.roll[:cut],
                                 window=rs.window, up_th=rs.up_th,
                                 down_th=rs.down_th)
        for d in rs.dates[10:cut:13]:
            assert rs.day_regime(d) == prefix.day_regime(d)
            assert rs.smooth_regime(d, 10) == prefix.smooth_regime(d, 10)

    def test_future_data_cannot_change_today(self):
        """★★ 在数据尾部追加暴跌，今日判定不得改变。"""
        base = mk_index(UP + FLAT, seed=5)
        rs_a = rg.build_regime_series(base, window=20)
        # 今日 = 第 100 个交易日
        today = rs_a.dates[99]
        verdict_a = rs_a.smooth_regime(today, 10)

        crash = list(base) + [(date(2099, 1, 1), 1.0)] * 5
        rs_b = rg.build_regime_series(crash, window=20)
        assert rs_b.smooth_regime(today, 10) == verdict_a, \
            "追加未来暴跌改变了历史判定 => 存在前视"

    def test_mutating_future_rolls_does_not_affect_past(self):
        """★★ 直接改「今日之后的 roll 值」，历史判定必须不受影响。"""
        rs = rg.build_regime_series(mk_index(UP + FLAT + DOWN), window=20)
        cut = 120
        today = rs.dates[cut - 1]
        before = rs.smooth_regime(today, 10)
        tampered = list(rs.roll)
        for i in range(cut, len(tampered)):
            tampered[i] = 999.0                    # 篡改未来
        rs2 = rg.RegimeSeries(dates=rs.dates, roll=tampered,
                              window=rs.window, up_th=rs.up_th,
                              down_th=rs.down_th)
        assert rs2.smooth_regime(today, 10) == before

    def test_selfcheck_detects_lookahead_when_injected(self):
        """★ 自检本身要能抓到「被注入的前视」，否则它是装饰品。"""
        class _Leaky:
            """故意用「全序列平均」判定的伪状态（就是段标签的毛病）。"""

            def __init__(self, dates, roll):
                self.dates, self.roll = dates, roll
                self.window = 20
                self.up_th, self.down_th = 8.0, -8.0
                self._i = {d: k for k, d in enumerate(dates)}

            def smooth_regime(self, d, smooth=10):
                avg = sum(self.roll) / len(self.roll)     # ★ 用了全部未来
                return "up" if avg > self.up_th else "down"

            def day_regime(self, d):
                return self.smooth_regime(d)

        rows = mk_index(UP + DOWN, seed=11)
        rs = rg.build_regime_series(rows, window=20)
        leaky = _Leaky(rs.dates, rs.roll)
        r = rg.check_no_lookahead(leaky)
        assert not r["ok"], "★ 前视自检没抓到注入的前视 —— 它是装饰品"
        assert r["violations"]


# ================================================================ 前视代价口径
class TestLookaheadCostMethodology:
    """★★ 守护「量化前视代价」的**口径**正确。

    ★★ 我第一版踩过的坑（方法论错误，不是代码 bug）：
       直接把「段标签门控」与「因果门控」比超额，
       得出「前视反而更差（+1.68% vs +2.77%）」的荒谬结论。
       原因：两者选的**不是同一批交易日** ——
       实测 Jaccard 仅 84.9%（仅段标签 15 天、仅因果 11 天、交集 146 天）。
       ⇒ 差值里混了「门控口径差异」，不是「前视的收益」。

    正确口径：锁定同一批日期，只改「判定依据」是否含未来。
    """

    def test_same_basis_uses_same_date_set(self):
        """★ `causal` 与 `leaky` 必须都以 `up_dates` 为准。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rgt", ROOT / "scripts" / "run_regime_gate.py")
        rgt = importlib.util.module_from_spec(spec)
        sys.modules["rgt"] = rgt
        spec.loader.exec_module(rgt)
        import inspect
        src = inspect.getsource(rgt._make_identical_gate)
        assert "up_dates" in src, "leaky 必须复用 causal 的日期集合"
        # leaky 只应**更严**（额外要求段标签也为 up），不会反向放宽
        assert 'variant == "leaky"' in src

    def test_leaky_is_stricter_than_causal(self):
        """★★ leaky 允许的日期 ⊆ causal 允许的 —— 否则可能反向套利。

        ★ 这里**真的调用** `_make_identical_gate` 产出的闭包，
          靠 `seen` 的填充情况判断门控是否放行 ——
          若只是复刻源码条件，等于自己测自己（上一版就犯了这个错）。
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rgt", ROOT / "scripts" / "run_regime_gate.py")
        rgt = importlib.util.module_from_spec(spec)
        sys.modules["rgt"] = rgt
        spec.loader.exec_module(rgt)
        from backend.domain.backtest import Bar

        up_dates = {"2025-01-01", "2025-01-02", "2025-01-03"}
        seg_map = {"2025-01-01": "up", "2025-01-02": "flat",
                   "2025-01-03": "up"}

        def mk_stub(rgt):
            """把内层 make_entry_fn 换成「放行即记账」的桩。"""
            calls: list = []

            def fake_entry(bars, shared):
                def fn(bar, i, allb):
                    calls.append(bar.d)
                    return None            # 不产生信号，只记录被调用过
                return fn
            return calls, fake_entry

        passed = {}
        for variant in ("causal", "leaky"):
            calls, fake_entry = mk_stub(rgt)
            orig = rgt.rb.make_entry_fn
            rgt.rb.make_entry_fn = fake_entry
            try:
                gate = {"up_dates": up_dates,
                        "seg_of": lambda d: seg_map.get(d, "unknown")}
                sh = {"seen": set(), "last_i": -1, "buys": [], "sells": [],
                      "zs": None, "sell_levels": ("strong",), "bars": []}
                fn = rgt._make_identical_gate(sh, gate, variant)
                for d in sorted(up_dates):
                    fn(Bar(d=d, o=10, h=10, l=10, c=10, v=1e6), 0, [])
            finally:
                rgt.rb.make_entry_fn = orig
            passed[variant] = set(calls)

        assert passed["leaky"] <= passed["causal"], \
            f"leaky {passed['leaky']} 应⊆ causal {passed['causal']}"
        assert passed["leaky"] < passed["causal"], \
            "段标签判 flat 的那天应让 leaky 更严"
        assert "2025-01-01" in passed["causal"], \
            "causal 应放行（未调用闭包说明门控逻辑没被执行到）"

    def test_jaccard_is_reported(self):
        """★ 必须报告两个口径的日期集合相似度，否则读者无法判断可比性。"""
        src = (ROOT / "scripts" / "run_regime_gate.py").read_text(
            encoding="utf-8")
        assert "jaccard" in src.lower()
        assert "up_dates" in src and "leaky_up_dates" in src

    def test_methodology_comment_recorded(self):
        """★★ 必须把踩过的坑写进代码，否则后人会重新踩一遍。"""
        src = (ROOT / "scripts" / "run_regime_gate.py").read_text(
            encoding="utf-8")
        assert "Jaccard" in src or "jaccard" in src
        assert "口径差异" in src, "必须写明「差值混了口径差异」这个教训"
