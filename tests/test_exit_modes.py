# -*- coding: utf-8 -*-
"""
Hawk · 离场触发源测试（D15 · 提高卖点覆盖率）

★ 背景（实测 25 只 × 4750 根bar）：
    缠论卖点累计出现 5553 次，但「本日新可见」仅 **122 次（2.2%）**
    而「收盘跌破最近中枢下沿」占 **46.4%** 的天数
  ⇒ 卖点天然稀疏（结构确认需要时间），靠放宽 level 提升有限；
    **中枢破位是每天都可能发生的结构状态，天然高覆盖**。

★ 本文件守护三件事：
    1. 离场模式选择确实改变出场分布（覆盖率真的提高了吗）
    2. **`zs_break` 不能吃掉缠论卖点**（优先级：缠论卖点仍是首选离场）
    3. `zs_buffer` 缓冲生效（避免 1 分钱假破位）

运行： pytest tests/test_exit_modes.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "rb", ROOT / "scripts" / "run_backtest.py")
rb = importlib.util.module_from_spec(_spec)
sys.modules["rb"] = rb
_spec.loader.exec_module(rb)

from backend.domain.backtest import Bar           # noqa: E402


class _Cursor:
    """模拟 duckdb 连接：execute() 返回带 fetchall() 的游标。

    ★ duckdb 的 `con.execute(...)` 返回 Cursor，**不是 list** ——
      测试桩直接返回 list 会报 'list' object has no attribute 'fetchall'。
    """
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def mk_shared(levels=("strong",)) -> dict:
    return {"seen": set(), "last_i": -1, "buys": [], "sells": [], "zs": None,
            "sell_levels": tuple(levels)}


def mk_bars(n: int = 100) -> list[Bar]:
    d0 = "2026-01-01"
    return [Bar(d=f"{d0}_{i:03d}", o=10.0, h=10.2, l=9.8, c=10.0, v=1e6)
            for i in range(n)]


class _StubRes:
    """模拟 analyze() 的返回。"""
    def __init__(self, signals, zhongshus):
        self.signals = signals
        self.zhongshus = zhongshus


class _Sig:
    def __init__(self, kind, level, date):
        self.kind, self.level, self.date = kind, level, date
        self.price, self.reason, self.confidence = 10.0, "r", 0.8


class _ZS:
    def __init__(self, low, high):
        self.low, self.high = low, high


# ================================================================ 模式定义
class TestExitModes:
    def test_all_modes_exist(self):
        """★ 五个离场模式都要可用。"""
        for m in ("chan", "chan_all", "zs", "zs_ma", "none"):
            sh = mk_shared()
            assert callable(rb.make_exit_fn(sh, mode=m))

    def test_mode_none_never_exits(self):
        """★ mode=none 是对照组：只靠 max_hold / 止损。"""
        sh = mk_shared()
        fn = rb.make_exit_fn(sh, mode="none")
        bars = mk_bars()
        assert fn(bars[70], 70, bars) is None

    def test_cli_exposes_modes(self):
        src = (ROOT / "scripts" / "run_backtest.py").read_text(encoding="utf-8")
        for m in ("chan_all", "zs_ma"):
            assert f'"{m}"' in src, f"CLI 未暴露 {m}"
        assert '"--exit-mode"' in src
        assert '"--compare-exit-modes"' in src


# ================================================================ 中枢破位
class TestZhongshuBreak:
    def test_zs_break_triggers(self, monkeypatch):
        """收盘跌破最近中枢下沿 ⇒ zs_break。"""
        bars = mk_bars()
        # 造一个中枢下沿 9.0，当前收盘 8.5（已跌破）
        bars[70] = Bar(d=bars[70].d, o=8.5, h=8.6, l=8.4, c=8.5, v=1e6)
        sh = mk_shared()
        monkeypatch.setattr(rb, "_scan_new",
                            lambda *a, **k: ([], [], {"low": 9.0, "high": 11.0}))
        fn = rb.make_exit_fn(sh, mode="zs", warmup=0)
        assert fn(bars[70], 70, bars) == "zs_break"

    def test_zs_no_break_when_above(self, monkeypatch):
        """收盘在中枢内 ⇒ 不触发。"""
        bars = mk_bars()
        sh = mk_shared()
        monkeypatch.setattr(rb, "_scan_new",
                            lambda *a, **k: ([], [], {"low": 9.0, "high": 11.0}))
        fn = rb.make_exit_fn(sh, mode="zs", warmup=0)
        assert fn(bars[70], 70, bars) is None

    def test_zs_buffer_gives_room(self, monkeypatch):
        """★ zs_buffer 生效：下探 1% 内不算破位。

        否则收盘价擦着中枢下沿就会离场，过于敏感。
        """
        bars = mk_bars()
        # 收盘 8.96，中枢下沿 9.0 → 破了 0.44%
        bars[70] = Bar(d=bars[70].d, o=8.96, h=9.0, l=8.9, c=8.96, v=1e6)
        sh = mk_shared()
        monkeypatch.setattr(rb, "_scan_new",
                            lambda *a, **k: ([], [], {"low": 9.0, "high": 11.0}))
        # 无缓冲 → 触发
        fn0 = rb.make_exit_fn(sh, mode="zs", warmup=0, zs_buffer=0.0)
        assert fn0(bars[70], 70, bars) == "zs_break"
        # 2% 缓冲 → 不触发
        fn2 = rb.make_exit_fn(sh, mode="zs", warmup=0, zs_buffer=0.02)
        assert fn2(bars[70], 70, bars) is None

    def test_zs_missing_is_safe(self, monkeypatch):
        """★ 无中枢（zs=None）时不能崩，也不能瞎触发。"""
        bars = mk_bars()
        sh = mk_shared()
        monkeypatch.setattr(rb, "_scan_new", lambda *a, **k: ([], [], None))
        fn = rb.make_exit_fn(sh, mode="zs", warmup=0)
        assert fn(bars[70], 70, bars) is None

    def test_mode_chan_ignores_zs(self, monkeypatch):
        """★ mode=chan **不因中枢破位离场**（只认缠论卖点）。

        这是基线口径；若chan 也吃中枢破位，「仅缠论卖点」的对照就失效了。
        """
        bars = mk_bars()
        bars[70] = Bar(d=bars[70].d, o=8.0, h=8.1, l=7.9, c=8.0, v=1e6)
        sh = mk_shared()
        monkeypatch.setattr(rb, "_scan_new",
                            lambda *a, **k: ([], [], {"low": 9.0, "high": 11.0}))
        fn = rb.make_exit_fn(sh, mode="chan", warmup=0)
        assert fn(bars[70], 70, bars) is None

    def test_chan_sell_takes_priority_over_zs(self, monkeypatch):
        """★★ 同时有缠论卖点和中枢破位时 ⇒记 chan_sell。

        缠论卖点是「结构确认」，中枢破位是「状态」，
        归因上前者更准确 —— 若记成 zs_break，
        统计上会把「主动止盈离场」误归为「破位止损」。
        """
        bars = mk_bars()
        bars[70] = Bar(d=bars[70].d, o=8.0, h=8.1, l=7.9, c=8.0, v=1e6)
        sh = mk_shared()
        monkeypatch.setattr(rb, "_scan_new",
                            lambda *a, **k: ([], [_Sig("3sell", "strong", "x")],
                                             {"low": 9.0, "high": 11.0}))
        fn = rb.make_exit_fn(sh, mode="zs", warmup=0)
        assert fn(bars[70], 70, bars) == "chan_sell"


# ================================================================ 卖点档位
class TestSellLevels:
    def test_sell_levels_default_is_none(self):
        """★ sell_levels 默认 None —— 真实值由 shared 提供。

        之前默认 ("strong",) 时，调用方漏传就静默丢掉 normal 档卖点，
        表现像「没信号」，极难定位。现在唯一事实来源是 shared。
        """
        import inspect
        sig = inspect.signature(rb._scan_new)
        assert "sell_levels" in sig.parameters
        assert sig.parameters["sell_levels"].default is None, \
            "默认应为 None（由 shared 提供），否则漏传会静默退回 strong"

    def test_normal_included_when_requested(self, monkeypatch):
        """sell_levels 含 normal 时，normal 档卖点应被纳入。"""
        bars = mk_bars()
        sh = mk_shared(levels=("strong", "normal"))
        got = []

        def fake_analyze(code, recs, as_of):
            return _StubRes([_Sig("2sell", "normal", "2026-01-01"),
                             _Sig("1sell", "strong", "2026-01-02")], [])

        monkeypatch.setattr(rb, "analyze", fake_analyze)
        _b, sells, _z = rb._scan_new(bars, 70, 60, sh["seen"], sh)
        kinds = {s.kind for s in sells}
        assert "2sell" in kinds, "sell_levels 含 normal 时应纳入 2sell"

    def test_normal_excluded_by_default(self, monkeypatch):
        """★ 默认（strong）不应纳入 normal 档。"""
        bars = mk_bars()
        sh = mk_shared(levels=("strong",))

        def fake_analyze(code, recs, as_of):
            return _StubRes([_Sig("2sell", "normal", "2026-01-01")], [])

        monkeypatch.setattr(rb, "analyze", fake_analyze)
        _b, sells, _z = rb._scan_new(bars, 70, 60, sh["seen"], sh)
        assert sells == [], "默认不应纳入 normal 档卖点"


# ================================================================ scan_new 契约
class TestScanNewContract:
    def test_returns_zhongshu(self, monkeypatch):
        """★ 必须返回最后中枢（出场判破位要用）。"""
        bars = mk_bars()
        sh = mk_shared()

        class _ZS2:
            low, high = 9.0, 11.0

        monkeypatch.setattr(rb, "analyze",
                            lambda c, r, as_of: _StubRes([], [_ZS2()]))
        _b, _s, zs = rb._scan_new(bars, 70, 60, sh["seen"], sh)
        assert zs == {"low": 9.0, "high": 11.0}

    def test_cache_reused(self, monkeypatch):
        """★ 同一根 bar 二次调用应命中缓存，不重复扫描。"""
        bars = mk_bars()
        sh = mk_shared()
        calls = []

        def fake_analyze(code, recs, as_of):
            calls.append(1)
            return _StubRes([], [])

        monkeypatch.setattr(rb, "analyze", fake_analyze)
        rb._scan_new(bars, 70, 60, sh["seen"], sh)
        rb._scan_new(bars, 70, 60, sh["seen"], sh)
        assert len(calls) == 1, "缓存未生效，同一根被重复扫描"

    def test_zhongshu_not_cached_across_bars(self, monkeypatch):
        """★ 中枢是结构状态，**不能**被 seen 缓存冻结。

        早期把 zs 放进 cache 后，跨 bar 读到的是旧中枢，
        会用几周前的中枢下沿判破位。
        """
        bars = mk_bars()
        sh = mk_shared()
        vals = iter([9.0, 8.0])

        class _ZS2:
            def __init__(self, v):
                self.low, self.high = v, 11.0

        def fake_analyze(code, recs, as_of):
            return _StubRes([], [_ZS2(next(vals))])

        monkeypatch.setattr(rb, "analyze", fake_analyze)
        _b1, _s1, zs1 = rb._scan_new(bars, 70, 60, sh["seen"], sh)
        _b2, _s2, zs2 = rb._scan_new(bars, 71, 60, sh["seen"], sh)
        assert zs1["low"] == 9.0 and zs2["low"] == 8.0, \
            f"不同 bar 应读到不同中枢，实得 {zs1} / {zs2}"


# ================================================================ 大盘基准构造
class TestBuildBenchmark:
    """★★ 基准构造 —— 修正「只看绝对收益」的误判。"""

    def test_function_exists(self):
        assert callable(rb.build_benchmark)

    def test_empty_trades(self):
        assert rb.build_benchmark(_Cursor([]), []) is None

    def test_no_pct_chg_returns_none(self):
        """★ 缺 pct_chg 时返回 None（不猜、不用 0 代替）。"""
        t = type("T", (), {"entry_date": "2026-01-02",
                            "exit_date": "2026-01-10"})()
        assert rb.build_benchmark(_Cursor([]), [t]) is None

    def test_benchmark_pairs_by_trade_dates(self, monkeypatch):
        """★ 每笔成交用**自己的**入场/出场区间取基准。"""
        import datetime as _dt
        from backend.domain.backtest import Trade

        import datetime as _dt
        d0 = _dt.date(2026, 1, 2)
        rows = [(d0 + _dt.timedelta(days=i), 1.0) for i in range(5)]
        t = Trade("c", "2026-01-02", 10.0, "2026-01-04", 10.5,
                  100, 0, 0, 0, 1, 1.0)
        bm = rb.build_benchmark(_Cursor(rows), [t])
        assert bm is not None
        # 入场 1/2、出场 1/4 ⇒ 基准区间是1/3、1/4 两天 = +2%
        assert bm.rets[0] == 2.0, f"基准应为 +2%，实得 {bm.rets[0]}"

    def test_trade_date_outside_benchmark_skipped(self):
        """★ 日期不在基准序列里就跳过，不得抛异常或猜值。"""
        import datetime as _dt
        d0 = _dt.date(2026, 1, 2)
        rows = [(d0 + _dt.timedelta(days=i), 1.0) for i in range(5)]

        class _T:
            entry_date = "2099-01-01"
            exit_date = "2099-01-05"

        assert rb.build_benchmark(_Cursor(rows), [_T()]) is None
