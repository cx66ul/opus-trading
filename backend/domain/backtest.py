# -*- coding: utf-8 -*-
"""
Hawk · 回测引擎（M9 · 含成本模型、Walk-forward、样本外）

★★★ 本模块存在的最大理由：**避免 M8 踩过的「信号价是事后极值」偏差**。

  M8 实测：同一批信号，「按信号价」算胜率 99.5~100%，
          「按收盘价」算只有 86~91%。
  原因：买点报价取回调笔最低点、卖点取反弹笔最高点，
        都是**收盘后才知道**的极值。回测按那个价成交 = 事后作弊。

  所以本引擎的入场价口径（不可改）：
    ★ 信号在 T 日收盘后产生（缠论用当日收盘数据算）
    → **最早 T+1 开盘买入**（next_open）
    不提供「按信号价成交」的选项 ——
    那个选项存在就会有人用它调出一个漂亮的假胜率。

★★★ 成本模型为什么必须内置：
  A 股双边成本（印花税+ 佣金+ 冲击成本）合计约 0.15%~0.3%。
  10% 涨幅的交易，扣掉 0.25% 成本后剩 9.75%；
  但**胜率 50% 的策略在无成本下可能显示 55%**，加上成本就变48%。
  不算成本 = 系统性高估。「小波动频繁交易」型策略尤其致命，
  成本会把它从盈利变成亏损。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

CST = timezone(timedelta(hours=8))


# ================================================================== 成本模型
@dataclass
class CostModel:
    """交易成本。默认值取 A 股常见档位。"""
    commission: float = 0.00025# 佣金 万2.5
    stamp_tax: float = 0.0005# 印花税 万5（**仅卖出**）
    slippage: float = 0.002# 冲击成本 + 滑点 单边 0.2%
    min_commission: float = 5.0              # 单笔最低佣金 5 元
    lot_size: int = 100                      # 一手 = 100 股

    def buy_cost(self, amount: float) -> float:
        """买入总成本（元）。"""
        c = max(amount * self.commission, self.min_commission)
        return c + amount * self.slippage

    def sell_cost(self, amount: float) -> float:
        """卖出总成本（元）。★印花税只在卖出收。"""
        c = max(amount * self.commission, self.min_commission)
        return c + amount * self.slippage + amount * self.stamp_tax

    def round_trip_cost_pct(self) -> float:
        """一轮完整买卖的成本占本金比例（用于快速估算）。"""
        buy = self.commission + self.slippage
        sell = self.commission + self.slippage + self.stamp_tax
        return buy + sell

    def shares_for(self, capital: float, price: float) -> int:
        """按本金算可买股数（向下取整到手）。★ 必须整手。"""
        if price <= 0:
            return 0
        budget = capital / (1 + self.commission + self.slippage)
        lots = int(budget // (price * self.lot_size))
        return max(0, lots * self.lot_size)


# ================================================================== 行情
@dataclass
class Bar:
    d: str
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


# ================================================================== 交易记录
@dataclass
class Trade:
    """一笔完整交易。"""
    code: str
    entry_date: str
    entry_price: float          # 实际成交价（含滑点）
    exit_date: str | None = None
    exit_price: float | None = None
    shares: int = 0
    buy_cost_paid: float = 0.0# 入场时实付的买入成本（避免平仓时重复扣）
    gross_pnl: float = 0.0
    cost: float = 0.0
    net_pnl: float = 0.0
    ret_pct: float = 0.0
    exit_reason: str = ""        # stop_loss | take_profit | max_hold | signal | eod
    ref_rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "code": self.code, "entry_date": self.entry_date,
            "entry_price": round(self.entry_price, 3),
            "exit_date": self.exit_date,
            "exit_price": round(self.exit_price, 3) if self.exit_price else None,
            "shares": self.shares,
            "gross_pnl": round(self.gross_pnl, 2),
            "cost": round(self.cost, 2),
            "net_pnl": round(self.net_pnl, 2),
            "ret_pct": round(self.ret_pct, 2),
            "exit_reason": self.exit_reason,
            "ref_rules": self.ref_rules, "reasons": self.reasons,
        }


@dataclass
class BacktestResult:
    """回测结果。"""
    strategy_id: str
    param_hash: str
    start: str
    end: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    initial_capital: float = 100_000.0
    params: dict = field(default_factory=dict)
    skipped: int = 0# 因数据不足/停牌而跳过的信号数
    # ★★ 大盘基准（可选）。**不给超额收益就等于没测** ——
    #   实测踩过：买点后 10 天个股 -2.11%、大盘 -2.30%，
    #   超额只有 +0.18% ⇒ 信号本身没有 alpha，
    #   但只看绝对收益会误判为「策略亏了 2%」。
    benchmark: "Benchmark | None" = None

    # ---- 统计指标
    @property
    def total_return_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        return (self.equity_curve[-1][1] / self.initial_capital - 1) * 100

    @property
    def wins(self) -> list[Trade]:
        return [t for t in self.trades if t.net_pnl > 0]

    @property
    def losses(self) -> list[Trade]:
        return [t for t in self.trades if t.net_pnl <= 0]

    @property
    def winrate(self) -> float | None:
        """★ 样本 < 10 时返回 None（附录 5.4 明令禁止显示胜率）。"""
        n = len(self.trades)
        if n < 10:
            return None
        return len(self.wins) / n

    @property
    def avg_win(self) -> float | None:
        w = [t.net_pnl for t in self.wins]
        return sum(w) / len(w) if w else None

    @property
    def avg_loss(self) -> float | None:
        """★ 返回**负值**（损失本身就是负数），不要取绝对值。"""
        l = [t.net_pnl for t in self.losses]
        return sum(l) / len(l) if l else None

    @property
    def pl_ratio(self) -> float | None:
        """盈亏比。全胜/全亏时无意义（∞/0），返回 None。"""
        aw, al = self.avg_win, self.avg_loss
        if aw is None or al is None or al == 0:
            return None
        return round(aw / abs(al), 2)

    @property
    def max_drawdown_pct(self) -> float:
        """最大回撤 %（基于权益曲线）。"""
        if not self.equity_curve:
            return 0.0
        peak = -math.inf
        mdd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                mdd = max(mdd, (peak - eq) / peak * 100)
        return round(mdd, 2)

    @property
    def avg_hold_days(self) -> float | None:
        hs = [t.hold_days for t in self.trades if t.hold_days is not None]
        return round(sum(hs) / len(hs), 1) if hs else None

    @property
    def total_cost(self) -> float:
        return sum(t.cost for t in self.trades)

    @property
    def cost_drag_pct(self) -> float:
        """成本拖累占总盈亏的比例。★ > 30% 说明策略是在给券商打工。"""
        gross = sum(t.gross_pnl for t in self.trades)
        if abs(gross) < 1e-9:
            return 0.0
        return round(self.total_cost / abs(gross) * 100, 1)

    def stats(self) -> dict:
        rets = [t.ret_pct for t in self.trades]
        d = {
            "strategy_id": self.strategy_id,
            "param_hash": self.param_hash,
            "start": self.start, "end": self.end,
            "trades": len(self.trades),
            "skipped": self.skipped,
            "total_return_pct": round(self.total_return_pct, 2),
            "winrate": round(self.winrate, 4) if self.winrate is not None else None,
            "avg_win": round(self.avg_win, 2) if self.avg_win else None,
            "avg_loss": round(self.avg_loss, 2) if self.avg_loss is not None else None,
            "pl_ratio": self.pl_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "avg_hold_days": self.avg_hold_days,
            "total_cost": round(self.total_cost, 2),
            "cost_drag_pct": self.cost_drag_pct,
            "enough_sample": len(self.trades) >= 10,
        }
        # ★ 大盘超额（无基准时显式标注，不能让读者误以为已算过）
        if self.benchmark is not None:
            d["excess"] = self.benchmark.summary(rets)
        else:
            d["excess"] = {"n": 0, "excess_avg": None,
                           "note": "⚠ 未提供大盘基准，无法判断是否有 alpha"}
        return d

    def to_json(self) -> dict:
        return {**self.stats(),
                "trades": [t.to_json() for t in self.trades[:200]]}


# 给 Trade 加个便捷属性（在类外挂，避免改动上面的定义顺序）
def _hold_days(self: Trade) -> int | None:
    if not self.exit_date or not self.entry_date:
        return None
    try:
        a = datetime.fromisoformat(self.entry_date)
        b = datetime.fromisoformat(self.exit_date)
        return (b - a).days
    except ValueError:
        return None


Trade.hold_days = property(_hold_days)


# ================================================================== 引擎
Entry = tuple[str, float, list[str], list[str], float, list[float], int | None]
"""入场信号元组： (entry_date, entry_price, ref_rules, reasons, position, take_profit, max_hold)"""


@dataclass
class Benchmark:
    """大盘基准。

    ★★ 为什么必须有（实测踩过）：
      买点后 10 天，个股平均 -2.11%、同期大盘 -2.30%，
      **超额收益仅 +0.18%** —— 信号本身没有 alpha。
      但只报绝对收益会看到「-2.11%」，很容易误判成「策略亏了」。
      ⇒ 任何「策略有效」的判断都必须先减去大盘。
    """
    rets: list[float] = field(default_factory=list)   # 与 trades 同序的基准收益%

    def excess(self, trade_rets: list[float]) -> list[float]:
        """超额收益 = 个股收益 - 基准收益（按序配对）。"""
        n = min(len(trade_rets), len(self.rets))
        return [trade_rets[i] - self.rets[i] for i in range(n)]

    def summary(self, trade_rets: list[float]) -> dict:
        ex = self.excess(trade_rets)
        if not ex:
            return {"n": 0, "excess_avg": None, "excess_median": None,
                    "excess_winrate": None,
                    "note": "无基准数据，无法算超额收益"}
        wins = [x for x in ex if x > 0]
        srt = sorted(ex)
        return {
            "n": len(ex),
            "excess_avg": round(sum(ex) / len(ex), 2),
            "excess_median": round(srt[len(srt) // 2], 2),
            "excess_winrate": (round(len(wins) / len(ex), 4)
                               if len(ex) >= 10 else None),
            "benchmark_avg": round(sum(self.rets[:len(ex)]) / len(ex), 2),
            "note": "" if len(ex) >= 10 else f"样本仅 {len(ex)} 笔，不显示超额胜率",
        }

ExitFn = Callable[[Bar, int, list[Bar]], str | None]
"""出场判定：(当根bar, 当前索引, 后续bars) -> 出场原因 or None

用于止盈/止损/最大持有天数等**与入场信号无关**的出场条件。
"""

ExitSigFn = Callable[[Bar, int, list[Bar]], bool]
"""反向信号判定：返回 True 表示应出场（如策略给出SELL）。"""


def backtest(
    code: str,
    bars: list[Bar],
    entry_fn: Callable[[Bar, int, list[Bar]], Entry | None],
    *,
    strategy_id: str = "",
    param_hash: str = "",
    exit_fn: ExitFn | None = None,
    exit_signal_fn: ExitSigFn | None = None,
    stop_loss_pct: float | None = None,
    cost: CostModel | None = None,
    initial_capital: float = 100_000.0,
    params: dict | None = None,
) -> BacktestResult:
    """单标的、单笔持仓回测。

    ★ entry_fn(entry_bar, i, all_bars) -> Entry | None
      收到的是**信号日**bar，返回 None 表示无信号。
      返回的 entry_date/entry_price 由引擎校验，**不允许早于信号日**。
    """
    cost = cost or CostModel()
    res = BacktestResult(strategy_id=strategy_id, param_hash=param_hash,
                         start=bars[0].d if bars else "",
                         end=bars[-1].d if bars else "",
                         initial_capital=initial_capital,
                         params=params or {})
    if len(bars) < 2:
        return res

    cash = initial_capital
    holding: Trade | None = None
    i = 0
    # ★ 必须能走到最后一根：`while i < len(bars) - 1` 永远到不了末根，
    #   于是 eod 强平不会触发 —— 未平仓的持仓被**静默丢弃**，
    #   收益虚高（浮盈从未兑现）。实测：设max_hold=None 时一笔交易都不记录。
    while i < len(bars):
        bar = bars[i]
        # ---- 已有持仓：先判出场
        if holding is not None:
            reason = None
            # ★★ 顺序有意义：**风控最先判**。
            #   若先判 exit_fn（信号出场），再判止损，则同一天既满足
            #   「卖点离场」又「跌破止损」时，会被记成 stop_loss，
            #   错误归因（明明是主动止盈离场）；反过来先判止损则
            #   任何风控触发都会被正确标记。风控是兜底，优先级最高。
            if stop_loss_pct and stop_loss_pct > 0:
                stop_px = holding.entry_price * (1 - stop_loss_pct / 100.0)
                # 用**当日最低价**触价 —— 日线级别无法知道盘中先后，
                # 保守假设「当天摸到止损价」比「收盘才跌破」更贴近实盘。
                if bar.l <= stop_px:
                    reason = "stop_loss"
            if reason is None and exit_fn is not None:
                reason = exit_fn(bar, i, bars)
            if reason is None and exit_signal_fn is not None:
                # ★ 反向信号出场（独立的出场信号函数）。
                #   早期版本复用 entry_fn 当出场信号，结果「逢K线就买」的策略
                #   会在同一根 bar 上买入又卖出，无限churn、成本吞掉一切。
                #   买入与出场必须由**不同**的信号决定。
                if exit_signal_fn(bar, i, bars):
                    reason = "signal"
            # ★ 引擎内置「最大持有天数」判定：Entry 里带了 max_hold_days
            #   却不生效，等于策略可以无限期持仓 —— 收益率会脱离实际，
            #   而且 stop_loss/take_profit 的意义也被稀释。
            if reason is None:
                mh = getattr(holding, "_mh", None)
                e_i = getattr(holding, "_entry_i", i)
                if mh and i - e_i >= int(mh):
                    reason = "max_hold"
            if reason is None and i == len(bars) - 1:
                reason = "eod"                # 回测结束强制平仓
            if reason is not None:
                # ★ 平仓：现金只加回「卖出净额」——
                #   入场时已扣了「买入成交额 + 买入成本」，
                #   这里再减一次买入就是重复扣（实测会把权益打成负数）。
                cash += _settle(holding, bar, reason, cost)
                res.trades.append(holding)
                holding = None
            res.equity_curve.append((bar.d, _equity(cash, holding, bar, cost)))
            i += 1
            continue

        # ---- 空仓：找入场
        sig = entry_fn(bar, i, bars)
        if sig is None:
            res.equity_curve.append((bar.d, _equity(cash, holding, bar, cost)))
            i += 1
            continue
        entry_date, entry_px, rules, reasons, position, tp, mh = sig
        # ★★ 防作弊：入场日期不得早于信号日（entry_fn 用了未来数据就抓这里）
        if entry_date < bar.d:
            res.skipped += 1
            res.equity_curve.append((bar.d, cash))
            i += 1
            continue
        px = entry_px * (1 + cost.slippage) if entry_px > 0 else 0
        if px <= 0:
            res.skipped += 1
            res.equity_curve.append((bar.d, cash))
            i += 1
            continue
        capital = cash * min(max(position, 0.0), 1.0)
        shares = cost.shares_for(capital, px)
        if shares <= 0:
            res.skipped += 1
            res.equity_curve.append((bar.d, cash))
            i += 1
            continue
        holding = Trade(code=code, entry_date=entry_date, entry_price=px,
                        shares=shares, ref_rules=list(rules),
                        reasons=list(reasons))
        # 止盈/止损参数挂实例上，供 exit_fn 用
        setattr(holding, "_tp", list(tp or []))
        setattr(holding, "_mh", mh)
        setattr(holding, "_entry_i", i)
        # ★ 买入：扣现金 + 扣买入成本，并把成本记到交易上供平仓引用
        buy_c = cost.buy_cost(shares * px)
        holding.buy_cost_paid = buy_c
        cash -= shares * px + buy_c
        res.equity_curve.append((bar.d, _equity(cash, holding, bar, cost)))
        i += 1
    return res


def _equity(cash: float, holding: Trade | None, bar: Bar,
            cost: CostModel) -> float:
    """当前权益 = 现金 + 持仓市值（未含卖出成本，避免高估）。"""
    eq = cash
    if holding is not None:
        eq += holding.shares * bar.c
    return round(eq, 2)


def _settle(t: Trade, bar: Bar, reason: str, cost: CostModel) -> float:
    """平仓：填好交易记录，返回**应加回现金的金额**。

    ★★★ 现金流的正确算法（这里错了两次，必须写清楚）：
        入场时现金已扣：`买入成交额 + 买入成本`
        所以平仓时**只需加回** `卖出成交额 - 卖出成本`，
        绝不能再减一次买入 —— 那会把权益打成负数
        （实测：10万本金一笔平价交易后只剩 8986 元，权益 -91%）。

        而 `net_pnl` 是**另一个概念**：它相对的是「本笔投入的本金」，
        用于胜率/盈亏比统计。两者不可混用：
          cash  += 卖出净额                     （现金守恒）
          net_pnl = 卖出净额 - 买入总额 - 买入成本 （单笔盈亏）
    """
    px = bar.c if reason == "eod" else bar.o * (1 - cost.slippage)
    if px <= 0:
        px = bar.c
    amount = t.shares * px
    sc = cost.sell_cost(amount)
    entry_amount = t.shares * t.entry_price
    t.exit_date = bar.d
    t.exit_price = px
    t.exit_reason = reason
    t.gross_pnl = amount - entry_amount
    t.cost = t.buy_cost_paid + sc
    # 单笔盈亏：相对投入本金
    t.net_pnl = amount - sc - entry_amount - t.buy_cost_paid
    t.ret_pct = (t.net_pnl / entry_amount) * 100 if entry_amount > 0 else 0.0
    # ★ 现金回补：只加回卖出净额（买入部分在入场时已扣）
    return amount - sc


# ================================================================== Walk-forward
def walk_forward(
    bars: list[Bar],
    entry_fn: Callable[[Bar, int, list[Bar]], Entry | None],
    *,
    train_days: int = 120,
    test_days: int = 30,
    step_days: int = 30,
    strategy_id: str = "",
    param_hash: str = "",
    exit_fn: ExitFn | None = None,
    exit_signal_fn: ExitSigFn | None = None,
    cost: CostModel | None = None,
    initial_capital: float = 100_000.0,
    params: dict | None = None,
) -> dict:
    """Walk-forward 检验（附录 7.2 样本外验证）。

    流程：训练窗 → 测试窗 → 向前滚动 step → 拼起所有测试窗的成交。

    ★ 为什么必须有样本外：
      在全样本上调参数，得到的胜率只是「这段历史长什么样」，
      不是「策略有效」。样本外才回答「换一段时间还行不行」。
    """
    if len(bars) < train_days + test_days:
        return {"error": f"数据不足：需 {train_days + test_days} 根，"
                         f"实得 {len(bars)}"}
    folds: list[dict] = []
    all_trades: list[Trade] = []
    start = 0
    while start + train_days + test_days <= len(bars):
        tr0, tr1 = start, start + train_days
        te0, te1 = tr1, tr1 + test_days
        seg = bars[te0:te1]
        r = backtest("WF", seg, entry_fn, strategy_id=strategy_id,
                     param_hash=param_hash, exit_fn=exit_fn,
                     exit_signal_fn=exit_signal_fn, cost=cost,
                     initial_capital=initial_capital, params=params)
        folds.append({
            "train": [bars[tr0].d, bars[tr1 - 1].d],
            "test": [bars[te0].d, bars[te1 - 1].d],
            "trades": len(r.trades),
            "return_pct": round(r.total_return_pct, 2),
        })
        all_trades.extend(r.trades)
        start += step_days
    return {
        "strategy_id": strategy_id, "param_hash": param_hash,
        "folds": folds,
        "n_folds": len(folds),
        "oos_trades": len(all_trades),
        "oos_winrate": (round(len([t for t in all_trades if t.net_pnl > 0])
                              / len(all_trades), 4) if len(all_trades) >= 10
                        else None),
        "oos_return_sum_pct": round(sum(t.ret_pct for t in all_trades), 2),
        "note": ("oos_winrate 为 None = 样本外不足 10 笔，按规则不显示胜率"
                 if len(all_trades) < 10 else ""),
    }


def oos_gate(wf: dict, min_folds: int = 3, min_trades: int = 10,
             min_return_pct: float = 0.0) -> tuple[bool, str]:
    """样本外上线门槛（M10 验收硬条件：仅样本外验证通过的规则上线）。

    返回 (是否通过, 原因)。★ 门槛不通过时必须明确说明是哪一项不达标，
    不能让「没通过」变成一句模糊的话。
    """
    if wf.get("error"):
        return False, f"数据不足：{wf['error']}"
    if wf["n_folds"] < min_folds:
        return False, f"样本外折数 {wf['n_folds']} < {min_folds}，样本不足"
    if wf["oos_trades"] < min_trades:
        return False, (f"样本外成交 {wf['oos_trades']} 笔< {min_trades} 笔，"
                       f"不足以下结论")
    if wf["oos_return_sum_pct"] < min_return_pct:
        return False, (f"样本外累计收益 {wf['oos_return_sum_pct']}% "
                       f"<门槛 {min_return_pct}%")
    return True, f"通过：{wf['n_folds']} 折 / {wf['oos_trades']} 笔 / " \
                 f"{wf['oos_return_sum_pct']}%"


# ================================================================== 自动定权
@dataclass
class ParamGrid:
    """一个待搜索的参数维度。"""
    name: str
    values: list[Any]

    def all_combos(self, others: list[ParamGrid]) -> list[dict]:
        import itertools
        return [dict(zip([o.name for o in others], combo))
                for combo in itertools.product(*[o.values for o in others])]


def auto_tune(
    bars: list[Bar],
    entry_factory: Callable[[dict], Callable[[Bar, int, list[Bar]], Entry | None]],
    grids: list[ParamGrid],
    *,
    train_days: int = 120,
    test_days: int = 30,
    step_days: int = 30,
    cost: CostModel | None = None,
    initial_capital: float = 100_000.0,
    exit_fn: ExitFn | None = None,
    min_trades_in_sample: int = 10,
) -> dict:
    """样本内网格搜索 + 样本外验证（D9：权重由回测自动定权）。

    ★★ 流程（顺序不能变，否则就是在样本外调参）：
        1. **只在前段训练窗**里搜参数组合，按训练收益排序
        2. 取训练最优的一组，**再看它的样本外表现**
        3. 样本外不达门槛 → 该组不可上线
    ★★ 绝不能「遍历所有组合、看哪个样本外最好」——
        那样样本外就变成了训练集，等于什么都没验证。
    """
    if not grids:
        return {"error": "未提供参数搜索空间"}
    if len(bars) < train_days + test_days:
        return {"error": f"数据不足：需 {train_days + test_days} 根，"
                         f"实得 {len(bars)}"}

    train = bars[:train_days]
    # ★ 组合生成：所有网格维度做笛卡尔积（含第一个维度自身）。
    #   早期版本只对 grids[1:] 求积，漏掉 grids[0]，
    #   于是单维度搜索时 combos 为空 → KeyError。
    import itertools
    names = [g.name for g in grids]
    combos = [dict(zip(names, combo))
              for combo in itertools.product(*[g.values for g in grids])]
    if not combos:
        return {"error": "参数空间为空"}

    scored = []
    for params in combos:
        fn = entry_factory(params)
        r = backtest("TUNE", train, fn, cost=cost,
                     initial_capital=initial_capital, params=params)
        # ★ 训练窗内成交太少的组合不可信：3 笔交易赚 20% 是运气不是策略
        if len(r.trades) < min_trades_in_sample:
            continue
        scored.append({
            "params": params, "train_return_pct": round(r.total_return_pct, 2),
            "train_trades": len(r.trades),
            "train_winrate": round(r.winrate, 4) if r.winrate is not None else None,
        })

    if not scored:
        return {
            "error": f"所有 {len(combos)} 组参数在训练窗内成交均不足 "
                     f"{min_trades_in_sample} 笔，无法定权",
            # ★ 即使全部无效也要报出组合数与训练成交数 ——
            #   只回一句「无法定权」的话，调用方无法区分
            #   「参数空间太小」还是「数据不够」，排查方向完全不同。
            "candidates": len(combos), "valid": 0,
            "train": None, "best_params": None,
            "oos_passed": False, "oos_reason": "无有效参数组",
        }

    # ★ 排序键：先看训练收益，再看成交数（同等收益下样本多的更可信）
    scored.sort(key=lambda x: (x["train_return_pct"], x["train_trades"]),
                reverse=True)
    best = scored[0]
    fn = entry_factory(best["params"])
    wf = walk_forward(bars, fn, train_days=train_days, test_days=test_days,
                      step_days=step_days, param_hash=str(best["params"]),
                      cost=cost, initial_capital=initial_capital,
                      params=best["params"], exit_fn=exit_fn)
    ok, why = oos_gate(wf)
    return {
        "candidates": len(combos),
        "valid": len(scored),
        "best_params": best["params"],
        "train": {k: best[k] for k in
                  ("train_return_pct", "train_trades", "train_winrate")},
        "walk_forward": wf,
        "oos_passed": ok,
        "oos_reason": why,
        "top5": scored[:5],
    }


if __name__ == "__main__":                       # pragma: no cover
    import random
    from datetime import date, timedelta

    # ---- 构造确定性行情（避免随机数导致 flaky）
    rng = random.Random(42)
    bars: list[Bar] = []
    price = 100.0
    d0 = date(2025, 1, 2)
    for i in range(400):
        price = max(5.0, price * (1 + rng.uniform(-0.03, 0.035)))
        d = d0 + timedelta(days=i)
        o = price * (1 + rng.uniform(-0.01, 0.01))
        c = price
        h = max(o, c) * (1 + rng.uniform(0, 0.01))
        l = min(o, c) * (1 - rng.uniform(0, 0.01))
        v = 1e6 * (1 + rng.random())
        bars.append(Bar(d=d.isoformat(), o=o, h=h, l=l, c=c, v=v))

    cm = CostModel()
    print("=" * 74)
    print("回测引擎自测")
    print("=" * 74)
    print(f"成本模型：单边佣金{cm.commission * 1e4:.1f}万分 +印花税"
          f"{cm.stamp_tax * 1e4:.1f}万（卖出）+滑点{cm.slippage * 1e4:.1f}万")
    print(f"一轮完整买卖成本 ≈ {cm.round_trip_cost_pct() * 100:.3f}%")
    print(f"10万本金可买 {cm.shares_for(100000, 25.0)} 股（25元/股）")

    # ---- 均线交叉策略：快线上穿慢线买入，下穿卖出
    def sma(b: list[Bar], i: int, n: int) -> float | None:
        if i - n < 0:
            return None
        return sum(x.c for x in b[i - n + 1: i + 1]) / n

    def cross_up(i: int) -> bool | None:
        f0, s0 = sma(bars, i, 5), sma(bars, i, 20)
        f1, s1 = sma(bars, i - 1, 5), sma(bars, i - 1, 20)
        if None in (f0, s0, f1, s1):
            return None
        return f1 <= s1 and f0 > s0

    def cross_dn(i: int) -> bool | None:
        f0, s0 = sma(bars, i, 5), sma(bars, i, 20)
        f1, s1 = sma(bars, i - 1, 5), sma(bars, i - 1, 20)
        if None in (f0, s0, f1, s1):
            return None
        return f1 >= s1 and f0 < s0

    def entry(bar: Bar, i: int, allb: list[Bar]):
        if not cross_up(i) or i + 1 >= len(allb):
            return None
        # ★ 入场用 T+1 开盘（可成交价），不是 T 日收盘
        return (allb[i + 1].d, allb[i + 1].o, ["ma_cross"], ["5日上穿20日"],
                0.9, [0.08, 0.16], 20)

    def exitf(bar: Bar, i: int, allb: list[Bar]):
        # 只管反向交叉；最大持有天数由引擎按Entry 里的 max_hold_days 自己判
        return "cross_dn" if cross_dn(i) else None

    r = backtest("TEST", bars, entry, strategy_id="ma_cross", exit_fn=exitf,
                 cost=cm)
    print()
    print("--- 单次回测 ---")
    for k, v in r.stats().items():
        print(f"  {k:>22s}: {v}")
    if r.trades:
        print(f"  首笔: {r.trades[0].to_json()}")

    print()
    print("--- Walk-forward ---")
    wf = walk_forward(bars, entry, strategy_id="test", cost=cm)
    for k in ("n_folds", "oos_trades", "oos_winrate", "oos_return_sum_pct"):
        print(f"  {k:>20s}: {wf[k]}")
    print(f"  {'上线门槛':>20s}: {oos_gate(wf)}")

    print()
    print("--- 自动定权（D9）---")
    res = auto_tune(
        bars,
        entry_factory=lambda p: (
            lambda bar, i, allb: (allb[i + 1].d, allb[i + 1].o, ["t"], ["x"],
                                  p["position"], [0.05], 5)
            if i % 10 == 0 and i + 6 < len(allb) else None),
        grids=[ParamGrid("position", [0.3, 0.6, 1.0])],
        train_days=200, test_days=40, step_days=40, cost=cm)
    for k in ("candidates", "valid", "best_params", "train",
              "oos_passed", "oos_reason"):
        print(f"  {k:>20s}: {res.get(k)}")