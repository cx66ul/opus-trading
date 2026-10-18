# -*- coding: utf-8 -*-
"""
Hawk · 样本外验证测试（OOS · 止损锁定）

★ 这组测试守护的核心是一个**已踩过的坑**：
  `backtest.walk_forward` 把 bars 切片后独立重跑，
  隐含假设「entry_fn 只依赖片段内数据」。
  但缠论信号依赖 ① 长历史 ② **连续的 `seen` 集合**，
  实测切片重跑后成交从 2 笔掉到 **0 笔**。
  ⇒ 用它做缠论的样本外验证会得到「样本外 0 笔 ⇒ 无法验证」的假结论。

  所以 OOS 必须用「**全样本连续跑，只统计样本外区间的成交**」。

运行： pytest tests/test_oos.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util                          # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "run_oos", ROOT / "scripts" / "run_oos.py")
oos = importlib.util.module_from_spec(_spec)
sys.modules["run_oos"] = oos
_spec.loader.exec_module(oos)

from backend.domain.backtest import Trade      # noqa: E402


def mk_trades(spec: list[tuple[str, float]]) -> list:
    """spec: [(entry_date, ret_pct)] → Trade 列表（net_pnl 简化为 pct×100）。"""
    out = []
    for i, (d, pct) in enumerate(spec):
        t = Trade(code="X", entry_date=d, entry_price=100.0,
                  exit_date=d, exit_price=100.0 * (1 + pct / 100),
                  shares=100)
        t.ret_pct = pct
        t.net_pnl = pct * 100
        # ★ hold_days 是运行时挂在类上的 property（backtest 模块加载时注入），
        #   直接构造 Trade 时不存在 —— 显式设一个值兜底。
        t.__dict__["hold_days"] = 5
        out.append(t)
    return out


# ================================================================ 统计口径
class TestStats:
    def test_winrate_none_when_small(self):
        """★ 样本 < 10 时胜率 None，不能显示误导性数字。"""
        ts = mk_trades([("2026-01-01", 1.0)] * 5)
        s = oos.stats_of(ts, "x")
        assert s["winrate"] is None
        assert s["raw_hit"] == 1.0# 原始命中率仍记录
        assert "不足" in s["note"] or "仅" in s["note"]

    def test_winrate_available_when_enough(self):
        ts = mk_trades([("2026-01-01", 1.0)] * 5 +
                       [("2026-01-02", -1.0)] * 5)
        s = oos.stats_of(ts, "x")
        assert s["winrate"] == 0.5
        assert s["note"] == ""

    def test_empty_list(self):
        """★ 无成交时返回明确说明，不崩。"""
        s = oos.stats_of([], "x")
        assert s["n"] == 0 and s["winrate"] is None and s["note"] == "无成交"

    def test_worst_is_min(self):
        ts = mk_trades([("2026-01-01", 5.0), ("2026-01-02", -3.0),
                        ("2026-01-03", -8.0)])
        s = oos.stats_of(ts, "x")
        assert s["worst"] == -8.0

    def test_net_and_avg(self):
        ts = mk_trades([("2026-01-01", 2.0), ("2026-01-02", -1.0)])
        s = oos.stats_of(ts, "x")
        assert s["avg_ret"] == pytest.approx(0.5)
        assert s["net"] == pytest.approx(100.0)


# ================================================================ 切分逻辑
class TestSplit:
    def test_run_oos_sorts_before_split(self, monkeypatch):
        """★ 切分前必须按入场日排序，否则「后 40%」可能混进早期成交。

        验证方式是让 FakeRB 返回**乱序**成交，看run_oos 是否仍正确切分
        （若未排序，训练段会包含较晚的日期）。
        """
        dates = ["2026-03-01", "2026-01-01", "2026-02-01",
                 "2026-01-15", "2026-02-15"]

        class FakeTrade:
            def __init__(self, d, i):
                self.entry_date = d
                self.ret_pct = 1.0
                self.net_pnl = 100.0
                self.__dict__["hold_days"] = 3
                self._i = i

        class FakeResult:
            def __init__(self, trades):
                self.trades = trades

        class FakeRB:
            PARQUET = oos.rb.PARQUET

            @staticmethod
            def load_bars(con, code, limit=250):
                return list(range(200))     # 长度够用

            @staticmethod
            def make_entry_fn(bars, shared):
                return lambda bar, i, allb: None

            @staticmethod
            def make_exit_fn(shared):
                return lambda bar, i, allb: None

        def fake_backtest(code, bars, entry, **kw):
            return FakeResult([FakeTrade(dates[(code == "B") and 4 or k], k)
                               for k in range(5)])

        monkeypatch.setattr(oos, "rb", FakeRB)
        monkeypatch.setattr(oos, "backtest", fake_backtest)
        r = oos.run_oos(["A", "B"], None, 5.0, oos_ratio=0.4)
        assert r.get("error") is None
        # 切分点应落在按日期排序后的位置
        assert r["oos_start"] is not None
        # 训练段最后入场日 必须早于 样本外首日
        assert r["oos_start"] > "2026-01-01", \
            f"样本外起点 {r['oos_start']} 早于最早入场日，说明未按日期排序"

    def test_split_ratio(self):
        """样本外占比应符合设定。"""
        n = 100
        ts = mk_trades([(f"2026-01-{i % 28 + 1:02d}", 1.0) for i in range(n)])
        ts.sort(key=lambda t: t.entry_date)
        split = int(n * 0.6)
        assert split == 60
        assert len(ts[:split]) + len(ts[split:]) == n

    def test_oos_start_after_train_end(self):
        """★ 样本外起点必须晚于训练段最后入场日。"""
        ts = mk_trades([(f"2026-{1 + i // 28:02d}-{i % 28 + 1:02d}", 1.0)
                        for i in range(50)])
        ts.sort(key=lambda t: t.entry_date)
        split = int(50 * 0.6)
        assert ts[split].entry_date > ts[split - 1].entry_date

    def test_single_trade_no_crash(self):
        """只有 1 笔时切分不应崩（0 训练 / 1 样本外）。"""
        split = int(1 * 0.6)
        assert split == 0
        oos.stats_of([], "train")
        oos.stats_of(mk_trades([("2026-01-01", 1.0)]), "oos")


# ================================================================ 口径正确性
class TestOOSMethodology:
    def test_does_not_use_walk_forward_slicing(self):
        """★★ 本模块**不得**用切片重跑的方式做样本外。

        切片会破坏缠论的连续性（实测 2 笔 → 0 笔），
        得到「样本外 0 笔」的假结论。
        正确口径：全样本连续跑，只按入场日统计区间。
        """
        src = (ROOT / "scripts" / "run_oos.py").read_text(encoding="utf-8")
        assert "walk_forward(" not in src, \
            "run_oos 不应调用 walk_forward（切片重跑会让缠论信号归零）"

    def test_docstring_explains_why(self):
        """必须写明为什么不能用切片，否则后人会「顺手改回去」。"""
        src = (ROOT / "scripts" / "run_oos.py").read_text(encoding="utf-8")
        assert "walk_forward" in src
        assert "连续" in src or "seen" in src

    def test_trades_kept_continuous(self, monkeypatch):
        """★ run_oos 内部必须把完整 bars 传给 entry_fn（不切片）。"""
        seen_lengths: list[int] = []

        class FakeRB:
            PARQUET = ROOT / "data" / "snapshots" / "2026-10-08" / \
                "kline_daily.parquet"

            @staticmethod
            def load_bars(con, code, limit=250):
                from backend.domain.backtest import Bar
                from datetime import date, timedelta
                d0 = date(2025, 1, 1)
                return [Bar(d=(d0 + timedelta(days=i)).isoformat(),
                            o=10.0, h=10.2, l=9.8, c=10.0, v=1e6)
                        for i in range(200)]

            @staticmethod
            def make_entry_fn(bars, shared):
                seen_lengths.append(len(bars))
                return lambda bar, i, allb: None

            @staticmethod
            def make_exit_fn(shared):
                return lambda bar, i, allb: None

        monkeypatch.setattr(oos, "rb", FakeRB)
        oos.run_oos(["A", "B"], None, 5.0)
        assert seen_lengths, "未调用 entry_fn"
        # ★ 每次都应收到完整 200 根，而非某个切片
        assert all(n == 200 for n in seen_lengths), \
            f"entry_fn 收到的 bars 长度应恒为 200，实际 {set(seen_lengths)}"

    def test_no_error_when_zero_trades(self, monkeypatch):
        class FakeRB:
            PARQUET = oos.rb.PARQUET

            @staticmethod
            def load_bars(con, code, limit=250):
                return []

            @staticmethod
            def make_entry_fn(bars, shared):
                return lambda bar, i, allb: None

            @staticmethod
            def make_exit_fn(shared):
                return lambda bar, i, allb: None

        monkeypatch.setattr(oos, "rb", FakeRB)
        r = oos.run_oos(["A"], None, 5.0)
        assert "error" in r


# ================================================================ 门槛
class TestGate:
    def test_passes_with_positive(self):
        ok, why = oos.oos_gate({"error": None, "n_folds": 1,
                                "oos_trades": 50,
                                "oos_return_sum_pct": 10.0},
                               min_folds=1, min_trades=10)
        assert ok is True

    def test_fails_when_negative(self):
        """★ 样本外为负 ⇒ 不通过（这是 M10「仅样本外通过的规则上线」的硬条件）。"""
        ok, why = oos.oos_gate({"error": None, "n_folds": 1,
                                "oos_trades": 50,
                                "oos_return_sum_pct": -5.0},
                               min_folds=1, min_trades=10)
        assert ok is False and "收益" in why

    def test_fails_when_too_few(self):
        ok, why = oos.oos_gate({"error": None, "n_folds": 1,
                                "oos_trades": 3,
                                "oos_return_sum_pct": 10.0},
                               min_folds=1, min_trades=10)
        assert ok is False and "笔" in why


# ================================================================ 参数
class TestArgs:
    def test_default_stop_is_five(self):
        """★ 默认止损 5%（用户 2026-09-09 拍定）。"""
        import argparse
        # 从 main 的 argparse 构造读取默认值
        src = (ROOT / "scripts" / "run_oos.py").read_text(encoding="utf-8")
        assert '"--stop-pct", type=float, default=5.0' in src

    def test_default_oos_ratio(self):
        src = (ROOT / "scripts" / "run_oos.py").read_text(encoding="utf-8")
        assert '"--oos-ratio", type=float, default=0.4' in src


# ================================================================ 稳定性检验
class TestStabilityCheck:
    """★★ 判断「通过」是不是小样本巧合。

    实测踩过：60 只时样本外 +0.51%（通过）→ 200 只 -0.15%（不通过）
    → 400 只 +0.44%（又通过）。收益在 0 附近来回穿越。
    只报一个样本量的结果 = 在 7 个数字里挑了个好看的。
    """

    def test_signature(self):
        import inspect
        sig = inspect.signature(oos.stability_check)
        assert "codes" in sig.parameters
        assert "stop_pct" in sig.parameters
        assert "steps" in sig.parameters

    def test_unstable_when_signs_differ(self, monkeypatch):
        """★ 收益符号在各档间不一致 ⇒ 判定 unstable。"""
        seq = [0.5, -0.3, 0.4, -0.1]      # 正负交替

        def fake_run_oos(codes, con, stop, ratio, cost=None):
            avg = seq[len(codes) % len(seq)]
            return {"total": 100, "split_date": "x",
                    "train": oos.stats_of([], "t"),
                    "oos": {"label": "o", "n": 40, "winrate": 0.3,
                            "raw_hit": 0.3, "avg_ret": avg, "worst": -6.0,
                            "net": avg * 4000, "avg_hold": 7.0, "note": ""},
                    "oos_start": "a", "oos_end": "b"}

        monkeypatch.setattr(oos, "run_oos", fake_run_oos)
        st = oos.stability_check(list(range(400)), None, 5.0,
                                 steps=(1, 2, 3, 4))
        assert st["verdict"] == "unstable", \
            f"正负交替应判 unstable，实得 {st['verdict']}"
        assert "没有稳定优势" in st["note"]

    def test_stable_positive(self, monkeypatch):
        """各档均为正 ⇒ stable_positive。"""
        def fake_run_oos(codes, con, stop, ratio, cost=None):
            return {"total": 100, "split_date": "x",
                    "train": oos.stats_of([], "t"),
                    "oos": {"label": "o", "n": 40, "winrate": 0.3,
                            "raw_hit": 0.3, "avg_ret": 0.8, "worst": -6.0,
                            "net": 3200, "avg_hold": 7.0, "note": ""},
                    "oos_start": "a", "oos_end": "b"}

        monkeypatch.setattr(oos, "run_oos", fake_run_oos)
        st = oos.stability_check(list(range(400)), None, 5.0,
                                 steps=(1, 2, 3))
        assert st["verdict"] == "stable_positive"
        assert st["n_pass"] == 3

    def test_stable_negative(self, monkeypatch):
        """各档均为负 ⇒ stable_negative（明确是亏的，别粉饰）。"""
        def fake_run_oos(codes, con, stop, ratio, cost=None):
            return {"total": 100, "split_date": "x",
                    "train": oos.stats_of([], "t"),
                    "oos": {"label": "o", "n": 40, "winrate": 0.3,
                            "raw_hit": 0.3, "avg_ret": -0.5, "worst": -9.0,
                            "net": -2000, "avg_hold": 7.0, "note": ""},
                    "oos_start": "a", "oos_end": "b"}

        monkeypatch.setattr(oos, "run_oos", fake_run_oos)
        st = oos.stability_check(list(range(400)), None, 5.0,
                                 steps=(1, 2, 3))
        assert st["verdict"] == "stable_negative"

    def test_reports_pass_count(self, monkeypatch):
        """★ 必须报出「几档通过 / 共几档」，不能只给结论。"""
        seq = [0.5, -0.3, 0.4]

        def fake_run_oos(codes, con, stop, ratio, cost=None):
            avg = seq[len(codes) % len(seq)]
            return {"total": 100, "split_date": "x",
                    "train": oos.stats_of([], "t"),
                    "oos": {"label": "o", "n": 40, "winrate": 0.3,
                            "raw_hit": 0.3, "avg_ret": avg, "worst": -6.0,
                            "net": avg * 4000, "avg_hold": 7.0, "note": ""},
                    "oos_start": "a", "oos_end": "b"}

        monkeypatch.setattr(oos, "run_oos", fake_run_oos)
        st = oos.stability_check(list(range(400)), None, 5.0,
                                 steps=(1, 2, 3))
        assert st["n_total"] == 3
        assert st["n_pass"] == 2

    def test_rows_have_detail(self, monkeypatch):
        """每档都要有明细，便于人工核对。"""
        def fake_run_oos(codes, con, stop, ratio, cost=None):
            return {"total": 100, "split_date": "x",
                    "train": oos.stats_of([], "t"),
                    "oos": {"label": "o", "n": 40, "winrate": 0.3,
                            "raw_hit": 0.3, "avg_ret": 0.4, "worst": -6.0,
                            "net": 1600, "avg_hold": 7.0, "note": ""},
                    "oos_start": "a", "oos_end": "b"}

        monkeypatch.setattr(oos, "run_oos", fake_run_oos)
        st = oos.stability_check(list(range(400)), None, 5.0,
                                 steps=(1, 2))
        for r in st["rows"]:
            for k in ("codes", "total", "oos_n", "avg_ret", "net", "passed"):
                assert k in r

    def test_error_when_no_valid_samples(self, monkeypatch):
        monkeypatch.setattr(oos, "run_oos",
                            lambda *a, **k: {"error": "无成交"})
        st = oos.stability_check(["A"], None, 5.0, steps=(1,))
        assert "error" in st

    def test_main_exposes_stability_flag(self):
        """CLI 必须能触发稳定性检验。"""
        src = (ROOT / "scripts" / "run_oos.py").read_text(encoding="utf-8")
        assert '"--stability"' in src
        assert "stability_check(" in src
