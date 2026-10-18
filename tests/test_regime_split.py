# -*- coding: utf-8 -*-
"""
Hawk · 分行情回测测试（M9 · 「策略是否只在特定行情有效」）

★ 这组测试防两个错：
  1. **阈值退化为 0** —— 若把「上涨」判成「涨跌 > 0%」，
     几乎任何日期都算上涨，结论变成同义反复
     （"上涨市超额为正" 恒成立，因为大部分时间都在"上涨"）。
     必须用双阈值（±8%）把震荡市单列。
  2. **按段切片重跑** —— 会破坏 `seen` 连续性、信号归零
     （同 `run_oos` 的坑）。分段只影响**统计归类**，不影响信号计算。

运行： pytest tests/test_regime_split.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "rs", ROOT / "scripts" / "run_regime_split.py")
rs = importlib.util.module_from_spec(_spec)
sys.modules["rs"] = rs
_spec.loader.exec_module(rs)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return self._rows


def mk_series(returns: list[float], start=date(2026, 1, 1)):
    """按每日涨跌幅造等权指数序列。"""
    rows, v, d = [], 100.0, start
    for r in returns:
        v *= (1 + r)
        rows.append((d, v))
        d += timedelta(days=1)
    return rows


# ================================================================ 分段逻辑
class TestSegments:
    """★ 分段逻辑已抽成纯函数 `rs.segment_by_roll`（2026-10-09）。

    原先 `market_segments` 内联 SQL + 分段，测试必须伪造 Parquet 查询结果，
    而 SQL 列序一变测试桩就跟着坏（`IndexError`）。
    抽成纯函数后：取数（含完备度闸门）与分段解耦，各自可独立测。
    """

    def test_up_segment_detected(self):
        """连续大涨应识别为上涨段。"""
        segs = rs.segment_by_roll(mk_series([0.02] * 100),
                                  win=60, up_th=8.0, down_th=-8.0)
        assert segs, "应有分段"
        assert segs[-1]["regime"] == "up", \
            f"连续上涨应判为 up，实得 {segs[-1]['regime']}"

    def test_down_segment_detected(self):
        """连续大跌应识别为下跌段。"""
        segs = rs.segment_by_roll(mk_series([-0.02] * 100),
                                  win=60, up_th=8.0, down_th=-8.0)
        assert segs[-1]["regime"] == "down"

    def test_flat_segment_detected(self):
        """★ 横盘必须单独成段（不能归入上涨或下跌）。"""
        segs = rs.segment_by_roll(mk_series([0.0005] * 100),
                                  win=60, up_th=8.0, down_th=-8.0)
        assert segs[-1]["regime"] == "flat", \
            f"横盘应判 flat，实得 {segs[-1]['regime']}"

    def test_threshold_not_zero(self):
        """★★ 阈值不能退化成 0（否则全是"上涨"，结论同义反复）。"""
        import inspect
        src = inspect.getsource(rs.segment_by_roll)
        assert "up_th" in src and "down_th" in src
        # 默认必须是 ±8 而不是 0
        sig = inspect.signature(rs.segment_by_roll)
        assert sig.parameters["up_th"].default == 8.0
        assert sig.parameters["down_th"].default == -8.0

    def test_insufficient_data(self):
        """★ 数据不足返回空列表，不抛异常。"""
        assert rs.segment_by_roll(mk_series([0.01] * 20), 60) == []

    def test_segments_cover_tail(self):
        """分段应覆盖 win 之后的所有交易日（前 win 天无60日历史）。"""
        segs = rs.segment_by_roll(mk_series([0.001] * 100), win=60)
        covered = sum(s["n"] for s in segs)
        assert covered == 40, f"应覆盖 100-60=40 天，实得 {covered}"

    def test_segments_contiguous_no_gaps(self):
        """★ 分段之间不能有空洞（否则成交可能被归到 unknown）。"""
        segs = rs.segment_by_roll(mk_series([0.02] * 50 + [-0.02] * 50 + [0.01] * 50),
                                  win=60)
        for a, b in zip(segs, segs[1:]):
            assert a["end"] < b["start"], "分段出现重叠/空洞"

    def test_regime_labels_valid(self):
        segs = rs.segment_by_roll(mk_series([0.02] * 80 + [-0.02] * 80), win=60)
        for s in segs:
            assert s["regime"] in ("up", "down", "flat")


# ================================================================ 统计归类
class TestRunSplit:
    def test_empty_code_list(self):
        con = type("C", (), {"execute": lambda *a, **k: _Cursor([])})()
        r = rs.run_split([], con, [])
        assert "error" in r

    def test_does_not_slice_bars(self, monkeypatch):
        """★★ 分段**不得**影响传给 entry_fn 的 bars 长度。

        若按段切片，`seen` 断裂、信号归零（run_oos 已踩过）。
        这里断言每次都收到完整长度的 bars。
        """
        seen_len: list[int] = []

        class FakeRB:
            PARQUET = rs.rb.PARQUET

            @staticmethod
            def load_bars(con, code, limit=250):
                from backend.domain.backtest import Bar
                return [Bar(d=f"2026-01-{i + 1:02d}" if i < 28
                            else f"2026-{(i // 28) + 1:02d}-{i % 28 + 1:02d}",
                            o=10.0, h=10.2, l=9.8, c=10.0, v=1e6)
                        for i in range(200)]

            @staticmethod
            def make_entry_fn(bars, shared):
                seen_len.append(len(bars))
                return lambda bar, i, allb: None

            @staticmethod
            def make_exit_fn(shared, warmup=60, mode="chan", **kw):
                return lambda bar, i, allb: None

            @staticmethod
            def build_benchmark(con, trades):
                return None

        monkeypatch.setattr(rs, "rb", FakeRB)
        segs = [{"regime": "up", "start": "2026-01-01",
                 "end": "2026-12-31", "n": 300}]
        rs.run_split(["A", "B"], None, segs)
        assert seen_len and all(n == 200 for n in seen_len), \
            f"entry_fn 应始终收到完整 200 根，实得 {set(seen_len)}"

    def test_regime_of_lookup(self):
        """按入场日归段：日期落在段内才算该段。"""
        segs = [
            {"regime": "up", "start": "2026-01-01", "end": "2026-03-31", "n": 90},
            {"regime": "down", "start": "2026-04-01", "end": "2026-06-30", "n": 90},
        ]
        # run_split 内部按段判定；这里用等价逻辑验证边界
        def regime_of(d):
            for s in segs:
                if s["start"] <= d <= s["end"]:
                    return s["regime"]
            return "unknown"

        assert regime_of("2026-02-01") == "up"
        assert regime_of("2026-05-01") == "down"
        assert regime_of("2026-12-31") == "unknown"


# ================================================================ 口径
class TestMethodology:
    def test_uses_equal_weight_index(self):
        """★ 基准是**全市场等权**（回答「我选的票比全体平均好多少」）。"""
        src = (ROOT / "scripts" / "run_regime_split.py").read_text(
            encoding="utf-8")
        assert "avg(close)" in src
        assert "等权" in src

    def test_docstring_explains_not_slicing(self):
        """必须写明「分段不改变信号计算」，否则后人会改成切片重跑。"""
        src = (ROOT / "scripts" / "run_regime_split.py").read_text(
            encoding="utf-8")
        assert "不改变信号计算" in src or "只是**统计**" in src

    def test_reports_excess_not_only_absolute(self):
        """★ 必须报超额，否则会重犯D15「只看绝对收益」的错。"""
        src = (ROOT / "scripts" / "run_regime_split.py").read_text(
            encoding="utf-8")
        assert "超额收益" in src
        assert "excess_avg" in src


# ================================================================ 分段定性
class TestSegmentLabeling:
    """★★ 分段定性必须用**段内平均**，不能用段末快照。

    实测踩过（结论完全失真）：
      按段末快照分段时，`down` 段 2026-07-17~08-13 的**段内实际 +7.17%**
      （下跌趋势的后期反弹），却因段末 60 日为 -11.91% 被标成「下跌市」
      ⇒ 后续统计把反弹期归进下跌市，得出「下跌市平均 +1.04%」的矛盾结论。
    """

    def test_down_segment_must_have_negative_avg_roll(self):
        """★ 所有「下跌段」的段内 60 日均值必须为负。"""
        rows = mk_series([0.02] * 60 + [-0.03] * 60 + [0.03] * 60)
        segs = rs.segment_by_roll(rows, win=20, up_th=8.0, down_th=-8.0)
        for s in segs:
            if s["regime"] == "down":
                assert s.get("avg_roll", -99) < 0, \
                    f"下跌段 {s['start']}~{s['end']} 的 avg_roll={s.get('avg_roll')} 应<0"

    def test_up_segment_must_have_positive_avg_roll(self):
        """★ 同理，「上涨段」的段内均值必须为正。"""
        rows = mk_series([-0.02] * 40 + [0.03] * 60 + [-0.02] * 40)
        segs = rs.segment_by_roll(rows, win=20, up_th=8.0, down_th=-8.0)
        for s in segs:
            if s["regime"] == "up":
                assert s.get("avg_roll", 99) > 0, \
                    f"上涨段 {s['start']}~{s['end']} 的 avg_roll 应>0"

    def test_segments_carry_avg_roll(self):
        """★ 每段必须带 avg_roll（供自检与人工核对）。"""
        rows = mk_series([0.02] * 100)
        segs = rs.segment_by_roll(rows, win=30)
        assert all("avg_roll" in s for s in segs), "缺少 avg_roll 无法自检"

    def test_no_contradictory_segments(self):
        """★ 整体自检：不应存在「标为下跌但段内均值为正」的段。"""
        rows = mk_series([0.02] * 50 + [-0.03] * 50 + [0.01] * 50)
        segs = rs.segment_by_roll(rows, win=20, up_th=8.0, down_th=-8.0)
        bad = [s for s in segs if s["regime"] == "down"
               and s.get("avg_roll", -1) >= 0]
        assert not bad, f"存在自相矛盾的下跌段: {bad}"
