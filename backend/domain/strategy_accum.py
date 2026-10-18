# -*- coding: utf-8 -*-
"""★ 第二个交易策略：S1→S7 吸筹确认（把 `accumulation.py` 接进策略框架）。

★★★ 为什么现在做这个
-------------------
组合回测的前提是「有多个策略」，但项目里长期只有 `chanlun_base` 一个。
而 `accumulation.py`（S1→S7 吸筹状态机，538 行 + 14 条测试）
**从未被任何策略调用** —— 它是一个孤岛模块。

★ 它的对照价值特别高：两套逻辑几乎正交
--------------------------------------
  · `chanlun_base` —— **结构**派：靠中枢/笔/背驰找买点
  · `accum`       —— **量价行为**派：靠放量/上影/缩量/换手识别主力吸筹
两者同时命中时，独立信号被验证；只命中其一说明是另一套逻辑的噪声。
组合回测要回答的正是「两者叠加能否互相削峰填谷」。

★★ 铁律二：`decide` 是纯函数，禁止查库/取系统时间
-----------------------------------------------
`StrategyContext` 已提供因子快照（含 `bars`），
本策略**只用 ctx 里的数据** + `ctx.as_of`，不碰数据库。
因此组合回测与实盘走的是同一条代码路径。

★ 为什么 S1~S6 不出信号
----------------------
`AccResult.is_buy` 只在 **S7 确认**时为 True ——
S1~S6 都是「观察态」，此时买入等于赌后续。
这是 `accumulation.py` 的既有语义，本策略**严格遵守**，
不为了「多出信号」而放宽 —— 放宽会让回测样本变多但结论失真。

运行：
    python -m backend.domain.strategy_accum --demo
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.domain.accumulation import AccParams, Stage, evaluate  # noqa: E402
from backend.domain.strategy import (                        # noqa: E402
    StrategyContext, StrategyDecision, TraderStrategy, register,
)


def _rows_from_ctx(ctx: StrategyContext) -> list[dict]:
    """`StrategyContext` → `accumulation.evaluate` 需要的 rows。

    ★ `StrategyContext` 存的是**分列的升序序列**（closes/highs/lows/
      volumes/dates），不是 Bar 对象列表 —— 必须按下标对齐成行。

    ★ `turnover`/`pct_chg` 从ctx 的**标量**属性取（那是「当日」值），
      缺失时**传 None 而不是 0** ——
      换手率为 0 会被读成「一字板零换手」，让状态机误判 S3 缩量洗盘。
      历史序列的换手率/涨跌幅 ctx 未提供 ⇒ 只有最后一行有值，
      这会削弱 S4「换手趋稳」的判定，**必须如实标注**而不是补 0。
    """
    n = len(ctx.dates)
    rows: list[dict] = []
    for i in range(n):
        rows.append({
            "date": ctx.dates[i],
            "open": ctx.highs[i] if i < len(ctx.highs) else ctx.closes[i],
            "high": ctx.highs[i] if i < len(ctx.highs) else ctx.closes[i],
            "low": ctx.lows[i] if i < len(ctx.lows) else ctx.closes[i],
            "close": ctx.closes[i],
            "volume": ctx.volumes[i] if i < len(ctx.volumes) else None,
            # ★ 只有**最后一行**（当日）有 turnover/pct_chg；
            #   历史行留None ⇒ S4「换手趋稳」会因数据不足而不成立。
            "turnover": ctx.turnover if i == n - 1 else None,
            "pct_chg": ctx.pct_chg_today if i == n - 1 else None,
        })
    return rows


@register
class AccumulationStrategy(TraderStrategy):
    """S1→S7 吸筹确认策略。"""

    strategy_id = "accum_s7"
    name = "吸筹确认（S7）"
    version = "1"
    timeframe = "1d"

    def default_params(self) -> dict[str, Any]:
        # 阈值全部走 AccParams（accumulation.py 的既有约定），此处只放
        # 「策略层」的参数：历史长度与是否允许非确认态入场。
        return {
            "history_bars": 60,      # 状态机需要的最小历史
            "allow_early_entry": False,   # ★ 默认 False：只认S7 确认
            "acc": {},
        }

    def decide(self, ctx: StrategyContext) -> StrategyDecision:
        """纯函数判定。★ 不查库、不取系统时间。"""
        need = int(self.params.get("history_bars", 60))
        n = len(ctx.dates)
        if n < need:
            return StrategyDecision(
                strategy_id=self.strategy_id, code=ctx.code,
                as_of=ctx.as_of.isoformat(timespec="seconds"),
                action="HOLD",
                reasons=[f"历史不足 {n}/{need} 根"])

        rows = _rows_from_ctx(ctx)
        acc_params = AccParams(**(self.params.get("acc") or {}))
        res = evaluate(ctx.code, rows, ctx.as_of, acc_params)

        base = dict(strategy_id=self.strategy_id, code=ctx.code,
                    as_of=ctx.as_of.isoformat(timespec="seconds"))
        if res.is_buy:
            #★ position 用 0.5：吸筹确认是「观察后入场」，不是满仓信号。
            #   仓位大小是风险决策，不该由形态强度单方面决定。
            #★ stop_loss 填**语义名**而非价格（StrategyDecision 约定）：
            #   这里用护盘底线，由组合层换算成价格。
            lo, hi = res.entry_low, res.entry_high
            return StrategyDecision(
                action="BUY", score=0.6, position=0.5,
                reasons=[f"★ S7 吸筹确认（{res.confirm_date}）："
                         f"入场区间 {lo:.2f}~{hi:.2f}，"
                         f"护盘底线 {res.support:.2f}"],
                ref_rules=["accum.S7"],
                risk_note="S1~S6 为观察态，仅 S7 确认入场；"
                          "跌破护盘底线视为吸筹失败",
                stop_loss="accum_support",
                max_hold_days=20,
                **base)

        # ★ 非确认态一律 hold —— 即使放宽 allow_early_entry 也不给 buy。
        #   S1~S6 是「观察态」，此时买入是赌后续，会让回测样本虚增、
        #   结论失真（用「形态刚出现」当买点，实盘是接盘）。
        note = f"当前阶段 {res.stage.value}"
        if res.tag:
            note += f"（{res.tag}）"
        return StrategyDecision(action="HOLD", reasons=[note], **base)


if __name__ == "__main__":                       # pragma: no cover
    from backend.domain.strategy import all_strategies

    print("=" * 70)
    print("已注册策略")
    print("=" * 70)
    for sid, cls in sorted(all_strategies().items()):
        print(f"  {sid:16s} {cls.name}（v{cls.version}）")
    print()
    s = AccumulationStrategy()
    print("参数:", s.describe()["params"])