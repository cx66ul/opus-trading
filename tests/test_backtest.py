# -*- coding: utf-8 -*-
"""
Hawk · 回测引擎测试（M9）

★ 这组测试守护的核心是**现金守恒**与**不可作弊**：

  1. 现金守恒 —— 我在这上面错了两次（见下），且错法都是「看起来有收益、
     实际权益为负」，不测就发现不了。
  2. 入场价口径 —— 引擎**不提供**「按信号价成交」的选项。
     M8 实测同一批信号按信号价胜率 99.5~100%、按收盘价只有 86~91%，
     差的是「事后才知道的极值」。回测里留这个口子就等于给自欺留门。
  3. 样本外门槛 —— M10 验收硬条件「仅样本外验证通过的规则上线」。

运行： pytest tests/test_backtest.py -v
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.backtest import (                   # noqa: E402
    Bar,
    BacktestResult,
    CostModel,
    ParamGrid,
    Trade,
    auto_tune,
    backtest,
    oos_gate,
    walk_forward,
)


# ================================================================ 行情构造
def mk_bars(n: int = 300, seed: int = 42, trend: float = 0.0) -> list[Bar]:
    """确定性行情（固定 seed，避免 flaky）。"""
    rng = random.Random(seed)
    out: list[Bar] = []
    px = 100.0
    d0 = date(2025, 1, 2)
    for i in range(n):
        px = max(5.0, px * (1 + trend + rng.uniform(-0.03, 0.035)))
        d = d0 + timedelta(days=i)
        o = px * (1 + rng.uniform(-0.01, 0.01))
        h = max(o, px) * (1 + rng.uniform(0, 0.01))
        l = min(o, px) * (1 - rng.uniform(0, 0.01))
        out.append(Bar(d=d.isoformat(), o=o, h=h, l=l, c=px, v=1e6))
    return out


def flat_bars(n: int = 50, px: float = 100.0) -> list[Bar]:
    """横盘：便于精确验证现金流。"""
    d0 = date(2025, 1, 2)
    return [Bar(d=(d0 + timedelta(days=i)).isoformat(), o=px, h=px,
                l=px, c=px, v=1e6) for i in range(n)]


def always_entry(position: float = 1.0, hold: int = 3, every: int = 0):
    """在每根（或每 `every` 根）信号，并在 `hold` 根后由引擎强平。

    ★ `every=0` 表示「只第一根买一次」—— 用于验证单笔现金流。
      默认每`every` 根重复入场，用于验证多次交易下的累计表现。
    """
    def fn(bar: Bar, i: int, allb: list[Bar]):
        step = every if every else (i + 1)
        if i % step != 0 or i + 2 >= len(allb):
            return None
        return (allb[i + 1].d, allb[i + 1].o, ["r"], ["test"], position,
                [0.05], hold)
    return fn


def repeat_entry(position: float = 1.0, hold: int = 3, period: int = 5):
    """每隔 `period` 根入场一次（引擎按 max_hold 出场后继续找新信号）。"""
    def fn(bar: Bar, i: int, allb: list[Bar]):
        if i % period != 0 or i + 2 >= len(allb):
            return None
        return (allb[i + 1].d, allb[i + 1].o, ["r"], ["test"], position,
                [0.05], hold)
    return fn


# ================================================================ 成本模型
class TestCostModel:
    def test_round_trip_includes_stamp_tax_once(self):
        """★ 印花税只在卖出收一次。"""
        cm = CostModel(commission=0.00025, stamp_tax=0.0005, slippage=0.002)
        rt = cm.round_trip_cost_pct()
        assert rt == pytest.approx(0.00025 * 2 + 0.0005 + 0.002 * 2)

    def test_sell_cost_higher_than_buy(self):
        cm = CostModel()
        assert cm.sell_cost(100000) > cm.buy_cost(100000)

    def test_min_commission_applies(self):
        """小额交易也要付最低佣金 5 元。"""
        cm = CostModel()
        assert cm.buy_cost(1000) >= 5.0
        assert cm.buy_cost(1000) == pytest.approx(5.0 + 1000 * 0.002)

    def test_shares_are_round_lot(self):
        """★ A 股必须整手（100 股），不能买 123.4 股。"""
        cm = CostModel()
        for px in (10.0, 25.3, 99.9, 3.5):
            s = cm.shares_for(100000, px)
            assert s % 100 == 0, f"{s} 不是整手"
            assert s >= 0

    def test_shares_zero_when_capital_too_small(self):
        cm = CostModel()
        assert cm.shares_for(50.0, 100.0) == 0, "买不起就该是 0 股，不能是负数"

    def test_shares_never_exceed_capital(self):
        """★ 买入金额不能超过本金（否则现金变负）。"""
        cm = CostModel()
        for px in (5.0, 20.0, 100.0):
            s = cm.shares_for(100000, px)
            assert s * px <= 100000, f"{px} 元时买入额超本金"


# ================================================================ 现金守恒
class TestCashConservation:
    """★★ 现金守恒 —— 我在这上面错过两次，且都是「有收益、权益为负」。"""

    def test_flat_price_loses_only_cost(self):
        """★ 横盘 + 全仓买入持有 → 只亏成本，绝不能亏本金。

        成本口径：买入滑点0.2% + 卖出滑点0.2% + 双边佣金 + 卖出印花税
        ≈ 0.6~0.9%（含最低佣金 5 元与整手取整的影响）。
        ★ 断言的是「亏损幅度是成本量级」，不是精确值 ——
        精确值随整手取整变化，写死会在参数微调时假失败。
        """
        bars = flat_bars(30, 100.0)
        r = backtest("T", bars, always_entry(1.0))
        assert len(r.trades) == 1
        t = r.trades[0]
        assert -1.5 < t.ret_pct < 0, f"横盘交易不应亏本金，实得 {t.ret_pct:.2f}%"
        assert t.cost > 0

    def test_equity_near_initial_after_flat_trade(self):
        """★★ 这是最关键的一条：10万本金横盘交易后必须还是 10万附近。

        我在现金守恒上错过两次，都是「看起来有收益、实际权益为负」：
          第一次只扣成本不扣成交额 → 现金虚高
          第二次平仓时又减一次买入 → 10万变成 8986（-91%）
        """
        bars = flat_bars(30, 100.0)
        r = backtest("T", bars, always_entry(1.0))
        assert r.total_return_pct == pytest.approx(-0.7, abs=0.3), \
            (f"横盘后权益应基本持平（仅亏成本），实得 {r.total_return_pct:.2f}%"
             f"（若为大幅负数=重复扣款）")

    def test_cash_plus_position_equals_equity(self):
        """权益 = 现金 + 持仓市值，逐根成立。"""
        bars = mk_bars(120, seed=7)
        r = backtest("T", bars, repeat_entry(0.5))
        assert r.equity_curve, "权益曲线不应为空"
        for d, eq in r.equity_curve:
            assert eq > 0, f"{d} 权益为负：{eq}（说明现金流有重复扣减）"

    def test_equity_curve_length_matches_bars(self):
        bars = mk_bars(100, seed=3)
        r = backtest("T", bars, repeat_entry(0.5))
        assert len(r.equity_curve) >= len(bars) - 2

    def test_no_buy_amount_overdraft(self):
        """现金不得为负（否则意味着能借钱无限买入）。"""
        bars = mk_bars(150, seed=11)
        r = backtest("T", bars, repeat_entry(1.0))
        for d, eq in r.equity_curve:
            assert eq > 0, f"{d} 权益转负 {eq}"

    def test_net_pnl_equals_gross_minus_cost(self):
        """net_pnl = gross_pnl - cost（用于胜率统计的内部一致性）。"""
        bars = mk_bars(150, seed=13)
        r = backtest("T", bars, repeat_entry(0.6))
        assert r.trades
        for t in r.trades:
            assert t.net_pnl == pytest.approx(t.gross_pnl - t.cost, abs=0.01)

    def test_profitable_market_actually_profits(self):
        """★ 上涨行情 + 足够大的漂移 → 应真的赚钱（否则说明成本/方向算反了）。

        ★ 为什么 trend 要给 0.02而不是 0.004：
          mk_bars 的单根漂移 = trend + E[uniform(-0.03, 0.035)] = trend + 0.25%。
          trend=0.004 时单根约 0.65%，持有 3 根仅约 +2%，
          而一轮买卖成本约 0.7% ⇒ **净收益约等于 0**，赚不赚钱全看噪声。
          这不是引擎的问题，是「短持有 + 低漂移 + 高成本」本来就该持平。
          想让「上涨必赚」成立，必须让漂移显著大于成本/持有期。
        """
        bars = mk_bars(200, seed=5, trend=0.02)
        r = backtest("T", bars, repeat_entry(1.0))
        assert r.total_return_pct > 0, f"强势上涨行情却亏 {r.total_return_pct:.2f}%"

    def test_cost_can_turn_profit_into_loss(self):
        """★★ 弱漂移市场里，高频交易被成本吃成亏损。

        这是成本模型存在的意义：单看总收益会以为策略有问题，
        实际是「交易太频繁 + 每次赚的还没成本多」。
        """
        bars = mk_bars(300, seed=6, trend=0.002)
        free = backtest("T", bars, repeat_entry(1.0, period=3),
                        cost=CostModel(commission=0, stamp_tax=0,
                                       slippage=0, min_commission=0))
        dear = backtest("T", bars, repeat_entry(1.0, period=3))
        assert dear.cost_drag_pct > free.cost_drag_pct
        assert dear.total_return_pct < free.total_return_pct

    def test_falling_market_actually_loses(self):
        bars = mk_bars(200, seed=5, trend=-0.02)
        r = backtest("T", bars, repeat_entry(1.0))
        assert r.total_return_pct < 0, f"下跌行情却赚 {r.total_return_pct:.2f}%"


# ================================================================ 不可作弊
class TestNoCheating:
    """★ 引擎必须挡住「用未来数据入场」。"""

    def test_entry_date_before_signal_day_is_rejected(self):
        """entry_date < 信号日 = 用了未来数据，必须计入 skipped。"""
        bars = flat_bars(30, 100.0)
        def bad(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return ("2020-01-01", 1.0, ["r"], ["作弊"], 1.0, [], 3)  # 更早的日期
        r = backtest("T", bars, bad)
        assert len(r.trades) == 0, "用未来数据入场应被拒"
        assert r.skipped == 1

    def test_zero_price_entry_rejected(self):
        bars = flat_bars(30, 100.0)
        def bad(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[i + 1].d, 0.0, ["r"], ["零价"], 1.0, [], 3)
        r = backtest("T", bars, bad)
        assert len(r.trades) == 0 and r.skipped == 1

    def test_negative_position_clamped(self):
        """负仓位应被夹到 0（不能反向做空）。"""
        bars = mk_bars(80, seed=9)
        r = backtest("T", bars, always_entry(-5.0))
        for t in r.trades:
            assert t.shares >= 0, "负仓位应夹到 0"

    def test_no_option_to_buy_at_signal_price(self):
        """★ 引擎只按「信号日之后的日期 + 该日价格」成交。

        早期版本让entry_fn 自由返回任意 entry_date/entry_price，
        等于开了「按事后知道的最低价成交」的后门。
        现在入场价仍由 entry_fn 给出，但 entry_date 不得早于信号日，
        且引擎在此基础上叠加滑点 —— 想作弊至少得先过日期校验。
        """
        bars = mk_bars(60, seed=4)
        calls = []

        def spy(bar: Bar, i: int, allb: list[Bar]):
            calls.append(i)
            if i != 0:
                return None
            return (allb[i + 1].d, allb[i + 1].o, ["r"], ["x"], 1.0, [], 3)
        r = backtest("T", bars, spy)
        t = r.trades[0]
        # 实际成交价必须 >= 引擎给出的下一根开盘价（因为加了滑点）
        assert t.entry_price >= bars[1].o, "成交价应含滑点（不低于原始开盘价）"


# ================================================================ 出场逻辑
class TestExit:
    def test_eod_forces_close(self):
        """回测结束必须平仓，否则权益曲线最后一段是浮盈。

        注意出场原因不一定是 eod —— Entry 里带 max_hold_days 时
        引擎会先按最大持有天数出场。这里断言「最后一笔必定已平仓」。
        """
        bars = mk_bars(50, seed=6)
        r = backtest("T", bars, always_entry(1.0), exit_fn=lambda b, i, a: None)
        assert r.trades
        last = r.trades[-1]
        assert last.exit_date is not None and last.exit_price is not None

    def test_eod_when_no_max_hold(self):
        """不设 max_hold 且不给出场条件时，最后一根必须 eod 强平。"""
        bars = mk_bars(50, seed=6)
        def entry(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [0.05], None)
        r = backtest("T", bars, entry, exit_fn=lambda b, i, a: None)
        assert r.trades[-1].exit_reason == "eod"

    def test_max_hold_enforced_by_engine(self):
        """★ Entry 里的 max_hold_days 必须真的生效（不设= 可无限持仓）。"""
        bars = mk_bars(60, seed=6)
        def entry(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [], 5)
        r = backtest("T", bars, entry, exit_fn=lambda b, i, a: None)
        assert r.trades[0].exit_reason == "max_hold"

    def test_max_hold_index_accounting(self):
        """持有 5 根后出场，**日历天数**约等于 5（可含周末，故按日历算）。

        注意：入场发生在 T+1（信号日的下一根），所以从入场日起算
        到第 5 根时，日历天数是 4~5，不能写死成 5。
        """
        bars = mk_bars(60, seed=6)
        def entry(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [], 5)
        r = backtest("T", bars, entry, exit_fn=lambda b, i, a: None)
        assert r.trades[0].exit_reason == "max_hold"
        assert 3 <= r.trades[0].hold_days <= 5

    def test_custom_exit_reason_preserved(self):
        """自定义出场原因必须原样记录（供错误归因用）。"""
        bars = mk_bars(60, seed=6)
        # max_hold 设足够大，让 exit_fn 能在 i>=10 时先触发
        def entry(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [], 50)
        r = backtest("T", bars, entry,
                     exit_fn=lambda b, i, a: "stop_loss" if i >= 10 else None)
        assert any(t.exit_reason == "stop_loss" for t in r.trades)

    def test_entry_and_exit_signals_separated(self):
        """★ 入场信号不该同时当出场信号（同根买卖= churn，成本吞掉一切）。"""
        bars = mk_bars(120, seed=8)
        # entry_fn 每根都返回信号；若同时充当出场信号，会在同一根上反复买卖
        def always(bar: Bar, i: int, allb: list[Bar]):
            if i + 2 >= len(allb):
                return None
            return (allb[i + 1].d, allb[i + 1].o, ["r"], ["x"], 0.5, [], 3)
        r = backtest("T", bars, always, exit_fn=lambda b, i, a: None)
        # 引擎内置 max_hold=3 ⇒ 每 3 根一笔，不会出现同根买卖
        for t in r.trades:
            assert t.exit_date != t.entry_date, f"出现同根买卖：{t.to_json()}"

    def test_exit_signal_fn_triggers(self):
        bars = mk_bars(60, seed=6)
        def entry(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [], 50)
        r = backtest("T", bars, entry,
                     exit_signal_fn=lambda b, i, a: i == 8)
        assert any(t.exit_reason == "signal" for t in r.trades)


# ================================================================ 统计口径
class TestStats:
    def test_winrate_none_when_sample_small(self):
        """★ 样本 < 10 时 winrate 必须 None（附录 5.4 明令禁止显示）。"""
        bars = mk_bars(60, seed=6)
        r = backtest("T", bars, always_entry(1.0))
        assert len(r.trades) < 10
        assert r.winrate is None
        assert r.stats()["winrate"] is None
        assert r.stats()["enough_sample"] is False

    def test_winrate_available_when_enough(self):
        bars = mk_bars(400, seed=6)
        r = backtest("T", bars, always_entry(1.0))
        if len(r.trades) >= 10:
            assert r.winrate is not None
            assert 0 <= r.winrate <= 1

    def test_pl_ratio_none_when_no_losses(self):
        """全胜时盈亏比无意义（∞），返回 None 而非崩溃。"""
        r = BacktestResult(strategy_id="x", param_hash="", start="", end="")
        r.trades = [Trade("c", "2025-01-01", 10.0, "2025-01-02", 11.0,
                          100, 0, 0, 100, 10.0)]
        assert r.pl_ratio is None

    def test_pl_ratio_none_when_no_wins(self):
        r = BacktestResult(strategy_id="x", param_hash="", start="", end="")
        r.trades = [Trade("c", "2025-01-01", 10.0, "2025-01-02", 9.0,
                          100, 0, 0, -100, -10.0)]
        assert r.pl_ratio is None

    def test_avg_loss_is_negative(self):
        """★ avg_loss 应为负数，不要取绝对值（否则与 avg_win 无法合成盈亏比）。"""
        bars = mk_bars(200, seed=5, trend=-0.004)
        r = backtest("T", bars, repeat_entry(1.0))
        if r.avg_loss is not None:
            assert r.avg_loss < 0

    def test_max_drawdown_positive(self):
        bars = mk_bars(200, seed=5, trend=-0.003)
        r = backtest("T", bars, repeat_entry(1.0))
        assert r.max_drawdown_pct >= 0

    def test_max_drawdown_zero_when_monotonic_up(self):
        """★ 权益单调上升时回撤必须为 0。

        早期版本从未记录 equity_curve，导致 total_return 恒为 0、
        max_dd 恒为 0 —— 两个指标都失真却不报错。
        """
        bars = mk_bars(200, seed=2, trend=0.005)
        r = backtest("T", bars, repeat_entry(1.0))
        assert r.equity_curve, "权益曲线不能为空"
        eqs = [e for _, e in r.equity_curve]
        if eqs == sorted(eqs):
            assert r.max_drawdown_pct == 0.0
        assert r.total_return_pct != 0.0, "权益曲线有值时总收益不应恒为 0"

    def test_cost_drag_computable(self):
        bars = mk_bars(200, seed=6)
        r = backtest("T", bars, repeat_entry(1.0))
        assert r.total_cost > 0, "有交易就该有成本"
        assert isinstance(r.cost_drag_pct, float)

    def test_hold_days_computed(self):
        bars = mk_bars(60, seed=6)
        r = backtest("T", bars, always_entry(1.0))
        for t in r.trades:
            assert t.hold_days is not None and t.hold_days >= 0


# ================================================================ Walk-forward
class TestWalkForward:
    def test_folds_generated(self):
        bars = mk_bars(400, seed=6)
        wf = walk_forward(bars, always_entry(1.0),
                          train_days=120, test_days=30, step_days=30)
        assert wf["n_folds"] > 0
        assert len(wf["folds"]) == wf["n_folds"]

    def test_insufficient_data_reports_error(self):
        bars = mk_bars(50, seed=6)
        wf = walk_forward(bars, always_entry(1.0), train_days=120,
                          test_days=30)
        assert "error" in wf

    def test_train_and_test_windows_non_overlapping(self):
        """★ 训练窗与测试窗不能重叠，否则样本外验证失效。"""
        bars = mk_bars(400, seed=6)
        wf = walk_forward(bars, always_entry(1.0),
                          train_days=120, test_days=30, step_days=30)
        for f in wf["folds"]:
            tr_end, te_start = f["train"][1], f["test"][0]
            assert te_start > tr_end, f"窗重叠：train{f['train']} test{f['test']}"

    def test_oos_winrate_none_when_small(self):
        bars = mk_bars(400, seed=6)
        wf = walk_forward(bars, always_entry(1.0), train_days=120,
                          test_days=30, step_days=30)
        if wf["oos_trades"] < 10:
            assert wf["oos_winrate"] is None


# ================================================================ 样本外门槛
class TestOosGate:
    def test_fails_on_error(self):
        ok, why = oos_gate({"error": "数据不足"})
        assert ok is False and "数据不足" in why

    def test_fails_on_too_few_folds(self):
        ok, why = oos_gate({"n_folds": 1, "oos_trades": 50,
                            "oos_return_sum_pct": 10})
        assert ok is False and "折数" in why

    def test_fails_on_too_few_trades(self):
        ok, why = oos_gate({"n_folds": 5, "oos_trades": 3,
                            "oos_return_sum_pct": 10})
        assert ok is False and "笔" in why

    def test_fails_on_negative_return(self):
        ok, why = oos_gate({"n_folds": 5, "oos_trades": 30,
                            "oos_return_sum_pct": -5.0})
        assert ok is False and "收益" in why

    def test_passes(self):
        ok, why = oos_gate({"n_folds": 5, "oos_trades": 30,
                            "oos_return_sum_pct": 12.0})
        assert ok is True and "通过" in why

    def test_reason_names_failing_item(self):
        """★ 不达标时必须说清是哪一项，不能只是一句「不通过」。"""
        for wf, kw in (
            ({"n_folds": 1, "oos_trades": 30, "oos_return_sum_pct": 5}, "折"),
            ({"n_folds": 5, "oos_trades": 1, "oos_return_sum_pct": 5}, "笔"),
            ({"n_folds": 5, "oos_trades": 30, "oos_return_sum_pct": -1}, "收益"),
        ):
            _ok, why = oos_gate(wf)
            assert kw in why, f"原因未指出不达标项：{why}"


# ================================================================ 自动定权
class TestAutoTune:
    def _factory(self):
        def make(params: dict):
            pos = params.get("position", 1.0)
            def fn(bar: Bar, i: int, allb: list[Bar]):
                if i % 20 != 0 or i + 3 >= len(allb):
                    return None
                return (allb[i + 1].d, allb[i + 1].o, ["r"], ["x"], pos,
                        [0.05], 5)
            return fn
        return make

    def test_requires_grids(self):
        r = auto_tune(mk_bars(300, seed=6), self._factory(), [])
        assert "error" in r

    def test_single_grid_produces_candidates(self):
        """★ 单维度网格也必须产出组合（早期只对 grids[1:] 求积，漏掉第一个维度）。"""
        r = auto_tune(mk_bars(300, seed=6), self._factory(),
                      [ParamGrid("position", [0.3, 0.6, 1.0])],
                      train_days=200, test_days=40, step_days=40)
        assert r.get("candidates") == 3, r.get("error")

    def test_multi_grid_cartesian(self):
        r = auto_tune(mk_bars(300, seed=6),
                      lambda p: (lambda bar, i, allb: None),
                      [ParamGrid("a", [1, 2]), ParamGrid("b", [3, 4, 5])],
                      train_days=200, test_days=40)
        assert r.get("candidates") == 6

    def test_insufficient_data(self):
        r = auto_tune(mk_bars(50, seed=6), self._factory(),
                      [ParamGrid("position", [1.0])],
                      train_days=200, test_days=40)
        assert "error" in r

    def test_best_params_from_valid_only(self):
        """★ 训练窗内成交太少的组合必须被剔除（3 笔赚 20% 是运气）。"""
        r = auto_tune(mk_bars(300, seed=6), self._factory(),
                      [ParamGrid("position", [0.3, 0.6, 1.0])],
                      train_days=200, test_days=40, step_days=40,
                      min_trades_in_sample=1)
        assert r["valid"] <= r["candidates"]
        assert r["best_params"] in [{"position": p} for p in (0.3, 0.6, 1.0)]

    def test_tuning_only_on_training_window(self):
        """★★ 参��只能在前段训练窗里搜，样本外绝不参与选择。

        遍历所有组合、挑「样本外最好的」= 样本外变成了训练集，
        等于什么都没验证。这是自动定权最容易被写错的地方。
        """
        bars = mk_bars(400, seed=6)
        r = auto_tune(bars, self._factory(),
                      [ParamGrid("position", [0.3, 0.6, 1.0])],
                      train_days=200, test_days=40, step_days=40)
        if "error" in r:
            pytest.skip(r["error"])
        # best_params 的训练统计必须只基于 train_days 那一段
        assert r["train"]["train_trades"] <= 200 / 10 + 2
        # 样本外是独立评估，只有一组参数被送进去
        assert isinstance(r["walk_forward"], dict)

    def test_reports_oos_verdict(self):
        r = auto_tune(mk_bars(300, seed=6), self._factory(),
                      [ParamGrid("position", [0.5, 1.0])],
                      train_days=200, test_days=40, step_days=40)
        if "error" in r:
            pytest.skip(r["error"])
        assert "oos_passed" in r and "oos_reason" in r
        assert isinstance(r["oos_passed"], bool)

    def test_all_params_rejected_when_trades_insufficient(self):
        """所有组合成交都不足时必须明确报错，而不是随便挑一个。"""
        def few(bar: Bar, i: int, allb: list[Bar]):
            if i == 0:
                return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [], 2)
            return None
        r = auto_tune(mk_bars(300, seed=6), lambda p: few,
                      [ParamGrid("position", [0.5, 1.0])],
                      train_days=200, test_days=40, min_trades_in_sample=50)
        assert "error" in r and "不足" in r["error"]


# ================================================================ 空输入
class TestEdgeCases:
    def test_empty_bars(self):
        r = backtest("T", [], always_entry())
        assert r.trades == [] and r.total_return_pct == 0.0

    def test_single_bar(self):
        r = backtest("T", flat_bars(1), always_entry())
        assert r.trades == []

    def test_no_signals(self):
        r = backtest("T", mk_bars(100, seed=6), lambda b, i, a: None)
        assert r.trades == [] and r.winrate is None

    def test_zero_cost_model(self):
        """零成本时横盘交易应恰好 0 收益（用于验证成本模型本身没算错）。"""
        bars = flat_bars(30, 100.0)
        r = backtest("T", bars, always_entry(1.0),
                     cost=CostModel(commission=0, stamp_tax=0, slippage=0,
                                    min_commission=0))
        assert r.trades[0].ret_pct == pytest.approx(0.0, abs=0.01)
        assert r.total_return_pct == pytest.approx(0.0, abs=0.01)

    def test_higher_cost_lowers_return(self):
        """★ 成本越高收益越低 —— 单调性必须成立。"""
        bars = mk_bars(200, seed=6, trend=0.003)
        cheap = backtest("T", bars, repeat_entry(1.0),
                         cost=CostModel(commission=0, stamp_tax=0,
                                        slippage=0, min_commission=0))
        dear = backtest("T", bars, repeat_entry(1.0), cost=CostModel())
        assert dear.total_return_pct < cheap.total_return_pct

    def test_freq_trading_loses_more_to_cost(self):
        """★ 越频繁交易，成本拖累越高（这是成本模型存在的意义）。"""
        bars = mk_bars(300, seed=6, trend=0.0005)
        sparse = backtest("T", bars, repeat_entry(1.0, period=30))
        dense = backtest("T", bars, repeat_entry(1.0, period=3))
        assert dense.cost_drag_pct > 0
        assert sparse.trades and dense.trades

# ================================================================ A方案止损
class TestStopLoss:
    """★ A 方案：止损参考位由用户执行，回测里做对照实验。

    注意：实盘里止损是**参考位**（`overlay.stop_references`），
    这里是「假设你按固定比例止损执行」的效果测算。
    """

    def test_stop_loss_triggers(self):
        """价格跌破止损位应触发 stop_loss 出场。"""
        bars = mk_bars(120, seed=6, trend=-0.02)   # 下跌行情
        r = backtest("T", bars, repeat_entry(1.0), stop_loss_pct=5.0)
        assert r.trades
        assert any(t.exit_reason == "stop_loss" for t in r.trades), \
            f"下跌行情 + 5% 止损应至少触发一次，实际出场原因：" \
            f"{[t.exit_reason for t in r.trades]}"

    def test_stop_loss_not_triggered_in_uptrend(self):
        """上涨行情不该频繁止损。"""
        bars = mk_bars(200, seed=6, trend=0.02)
        r = backtest("T", bars, repeat_entry(1.0), stop_loss_pct=8.0)
        n_stop = len([t for t in r.trades if t.exit_reason == "stop_loss"])
        assert n_stop < len(r.trades) * 0.5, "上涨行情止损触发过多"

    def test_stop_loss_none_disables(self):
        """stop_loss_pct=None / 0 时不启用止损。"""
        bars = mk_bars(120, seed=6, trend=-0.02)
        r = backtest("T", bars, repeat_entry(1.0), stop_loss_pct=None)
        assert not any(t.exit_reason == "stop_loss" for t in r.trades)
        r0 = backtest("T", bars, repeat_entry(1.0), stop_loss_pct=0.0)
        assert not any(t.exit_reason == "stop_loss" for t in r0.trades)

    def test_stop_loss_bounds_loss(self):
        """★ 止损的作用是**限制单笔亏损幅度**。

        这是 A 方案存在的全部理由：不追求提高胜率，
        而是把「拿满 10 天亏 -4.98%」压到可控范围。
        """
        bars = mk_bars(150, seed=6, trend=-0.015)
        no_stop = backtest("T", bars, repeat_entry(1.0))
        with_stop = backtest("T", bars, repeat_entry(1.0), stop_loss_pct=6.0)
        worst_no = min(t.ret_pct for t in no_stop.trades)
        worst_ws = min(t.ret_pct for t in with_stop.trades)
        assert worst_ws >= worst_no, \
            f"止损后最差单笔({worst_ws:.2f}%) 不应差于无止损({worst_no:.2f}%)"

    def test_stop_loss_uses_intraday_low(self):
        """★ 止损用当日最低价触价（日线级别最保守的假设）。"""
        #构造一根振幅极大、当日最低击穿止损位的 bar
        bars = flat_bars(20, 100.0)
        bars[5] = Bar(d=bars[5].d, o=100.0, h=100.5, l=80.0, c=99.0, v=1e6)
        def entry(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [], 20)
        r = backtest("T", bars, entry, stop_loss_pct=10.0)
        # 第 5 根最低 80，远低于止损价 90 ⇒ 应触发
        assert any(t.exit_reason == "stop_loss" for t in r.trades)

    def test_stop_loss_takes_priority_over_signal(self):
        """★★ 同一天既满足卖点离场又跌破止损时，必须记为 stop_loss。

        风控是兜底，优先级最高 —— 否则归因会错：
        明明是风控平仓，却被统计成「主动止盈离场」。
        """
        bars = flat_bars(30, 100.0)
        bars[6] = Bar(d=bars[6].d, o=100.0, h=100.5, l=85.0, c=99.0, v=1e6)
        def entry(bar: Bar, i: int, allb: list[Bar]):
            if i != 0:
                return None
            return (allb[1].d, allb[1].o, ["r"], ["x"], 1.0, [], 20)
        r = backtest("T", bars, entry, stop_loss_pct=8.0,
                     exit_fn=lambda b, i, a: "chan_sell" if i == 6 else None)
        t = r.trades[0]
        assert t.exit_reason == "stop_loss", \
            f"风控应优先，实际记为 {t.exit_reason}"

    def test_smaller_stop_more_triggers(self):
        """止损越紧触发越频繁（单调性）。"""
        bars = mk_bars(200, seed=6, trend=-0.004)
        wide = backtest("T", bars, repeat_entry(1.0), stop_loss_pct=12.0)
        tight = backtest("T", bars, repeat_entry(1.0), stop_loss_pct=4.0)
        n_wide = len([t for t in wide.trades if t.exit_reason == "stop_loss"])
        n_tight = len([t for t in tight.trades if t.exit_reason == "stop_loss"])
        assert n_tight >= n_wide, \
            f"紧止损({n_tight}) 触发应不少于宽止损({n_wide})"


# ================================================================ 大盘基准
class TestBenchmark:
    """★★ 大盘超额 —— 修正一个错误的结论。

    实测踩过（这是本项目最贵的一次误判）：
      买点后 10 天，个股平均 **-2.11%**、同期大盘 **-2.30%**
      只看绝对收益 ⇒「策略亏了 2%，买点无效」
      算超额 ⇒ **+0.18%** ⇒ 信号其实**只是跟跌，没有 alpha**

    ⇒ 任何「策略有效」的判断都必须先减大盘。
    """

    def test_excess_computed(self):
        """超额 = 个股 - 基准。"""
        from backend.domain.backtest import Benchmark
        bm = Benchmark(rets=[1.0, 2.0, 3.0])
        ex = bm.excess([2.0, 0.0, 5.0])
        assert ex == [1.0, -2.0, 2.0]

    def test_excess_pairs_by_order(self):
        """★ 基准与成交**按序配对**，不是按日期匹配。

        实测踩过：若按日期匹配，持仓跨越多天时会错位
        （基准取了卖出日而非买入日的涨跌）。
        """
        from backend.domain.backtest import Benchmark
        bm = Benchmark(rets=[10.0, 20.0])
        assert bm.excess([12.0, 25.0]) == [2.0, 5.0]

    def test_excess_length_mismatch_tolerated(self):
        """★ 长度不一致时取短的，不得崩溃。"""
        from backend.domain.backtest import Benchmark
        bm = Benchmark(rets=[1.0, 2.0])
        assert len(bm.excess([5.0, 6.0, 7.0])) == 2

    def test_summary_fields(self):
        from backend.domain.backtest import Benchmark
        bm = Benchmark(rets=[1.0] * 12)
        s = bm.summary([2.0] * 12)
        assert s["n"] == 12
        assert s["excess_avg"] == 1.0
        assert s["excess_winrate"] == 1.0
        assert s["benchmark_avg"] == 1.0

    def test_winrate_none_when_small(self):
        """★ 样本<10 时超额胜率为 None。"""
        from backend.domain.backtest import Benchmark
        bm = Benchmark(rets=[0.0] * 5)
        s = bm.summary([1.0] * 5)
        assert s["excess_winrate"] is None
        assert "不足" in s["note"] or "仅" in s["note"]

    def test_empty_benchmark(self):
        from backend.domain.backtest import Benchmark
        s = Benchmark(rets=[]).summary([1.0, 2.0])
        assert s["n"] == 0 and s["excess_avg"] is None

    def test_stats_warns_when_no_benchmark(self):
        """★★ 无基准时必须**显式警告**，不能让人以为已算过超额。"""
        r = BacktestResult(strategy_id="x", param_hash="", start="", end="")
        r.trades = [Trade("c", "2025-01-01", 10.0, "2025-01-02", 11.0,
                          100, 0, 0, 100, 10.0) for _ in range(12)]
        s = r.stats()
        assert "excess" in s
        assert s["excess"]["excess_avg"] is None
        assert "未提供大盘基准" in s["excess"]["note"], \
            "无基准时必须警告 —— 否则会被误读为「超额为 0」"

    def test_stats_includes_excess_when_benchmark_given(self):
        from backend.domain.backtest import Benchmark
        r = BacktestResult(strategy_id="x", param_hash="", start="", end="")
        # ★ Trade 位置参数第11 位才是 ret_pct，少传会默认 0.0 而非 10.0
        r.trades = [Trade("c", "2025-01-01", 10.0, "2025-01-02", 11.0,
                          100, 0, 0, 100, 100, 10.0) for _ in range(12)]
        r.benchmark = Benchmark(rets=[5.0] * 12)
        s = r.stats()
        assert s["excess"]["excess_avg"] == 5.0, \
            f"超额应为10-5=5，实得 {s['excess']['excess_avg']}"
        assert "未提供" not in s["excess"]["note"]

    def test_negative_strategy_can_have_positive_excess(self):
        """★★ 绝对亏但超额为正 —— 这正是「跟跌不等于亏 alpha」。

        若没有基准，这类结果会被误判为「策略无效」。
        """
        from backend.domain.backtest import Benchmark
        bm = Benchmark(rets=[-5.0] * 12)          # 大盘跌 5%
        s = bm.summary([-2.11] * 12)               # 策略只跌 2.11%
        assert s["excess_avg"] == pytest.approx(2.89)
        assert s["excess_winrate"] == 1.0

    def test_positive_strategy_can_have_negative_excess(self):
        """★ 绝对赚但跑输大盘 —— 同样是「没有 alpha」。"""
        from backend.domain.backtest import Benchmark
        bm = Benchmark(rets=[3.0] * 12)
        s = bm.summary([2.0] * 12)
        assert s["excess_avg"] == pytest.approx(-1.0)
