# -*- coding: utf-8 -*-
"""Hawk · 主力吸筹/控盘量价模型（S1→S7 显式状态机）

★ 本模块补齐的是一个**此前完全未实现**的核心策略。
  全项目 grep「吸筹/控盘/洗盘/护盘/上影/下影」在 backend/ 下零命中，
  而这条量价逻辑是本系统最看重的主力行为模型 —— 它解释「主力想拿货时
  盘面长什么样」，与缠论（结构）互补：缠论给位置，本模型给意图。

交易逻辑原文（逐条工程化，阈值全部可配）：

  S1 放量爆拉   某日放量，主力要拿货必须放量 → 量 ≥ 前5日均量×2 且 涨幅 ≥ 5%
  S2 上影线堆叠 S1 后 1–3 日，因前一日放量，次日惯性上冲但幅度不大
                （主力砸盘拿更低价筹码）→ 收上影线；可能连收一两天
  S3 缩量洗盘   之后缩量且连收短上影 → 较 S1 日缩量 ≥50% 且连续 ≥2 日，
                小实体短上影（实体振幅 ≤3%）→ 主力控盘洗盘吸筹
  S4 阴跌+换手趋稳  股价阴跌但换手率趋于稳定 → 近5日换手率极差 ≤ 阈值
                （抛压出清）
  S5 长下影护盘 突然收长下影 → 主力护盘底线出现
                （下影 ≥ 实体×2 且 下影/振幅 ≥50%）→ 记录底线价位
  S6 量价齐升   S5 后 1–2 日，量价齐升且换手率呈倍数扩大
                （量 ≥ 前5日均量×1.5 且 换手率 ≥ 前一阶段×2）
  S7 确认进场   次日换手率持续伴随股价走高 → 确定性买点 B2

设计约束（与项目铁律一致）：
  · 纯函数：不取系统时间，as_of 由调用方注入。
  · 每股独立状态：evaluate() 一次只处理一只股票的一段日线，返回完整流转记录。
  · 阈值全部来自 AccParams，可由配置覆盖。
  · 任一阶段破位 → 回到 IDLE 重新等待，不做「硬扛」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Stage(str, Enum):
    """状态机阶段。IDLE 表示当前没有任何进行中的吸筹形态。"""

    IDLE = "IDLE"
    S1_SURGE = "S1"          # 放量爆拉
    S2_UPPER_SHADOW = "S2"   # 上影线堆叠
    S3_SHRINK_WASH = "S3"    # 缩量洗盘
    S4_STABLE_TURNOVER = "S4"  # 阴跌 + 换手率趋稳
    S5_SUPPORT = "S5"        # 长下影护盘（护盘底线已现）
    S6_VOLUME_PRICE = "S6"   # 量价齐升
    S7_CONFIRMED = "S7"      # 确认进场（B2 强买）


STAGE_CN = {
    Stage.IDLE: "无形态",
    Stage.S1_SURGE: "放量爆拉",
    Stage.S2_UPPER_SHADOW: "上影线堆叠",
    Stage.S3_SHRINK_WASH: "缩量洗盘",
    Stage.S4_STABLE_TURNOVER: "换手趋稳",
    Stage.S5_SUPPORT: "护盘底线",
    Stage.S6_VOLUME_PRICE: "量价齐升",
    Stage.S7_CONFIRMED: "确认进场",
}

# 自选股列表上要打的标签（规格要求 S3/S4/S5 必须有标签）
STAGE_TAG = {
    Stage.S3_SHRINK_WASH: "洗盘中",
    Stage.S4_STABLE_TURNOVER: "换手趋稳",
    Stage.S5_SUPPORT: "护盘底线",
    Stage.S6_VOLUME_PRICE: "待确认",
    Stage.S7_CONFIRMED: "确认进场",
}


@dataclass(frozen=True)
class AccParams:
    """全部阈值（可由 config 覆盖，禁止硬编码在判定逻辑里）。"""

    # --- S1 放量爆拉
    s1_vol_mult: float = 2.0          # 量 / 前5日均量
    s1_pct_min: float = 5.0           # 涨幅下限 %
    s1_pct_relax: float = 3.0         # 放宽分支的涨幅下限 %
    s1_close_near_high: float = 0.8   # 放宽分支：收盘位于当日振幅的此分位以上
    # --- S2 上影线堆叠
    s2_max_days: int = 3              # S1 后观察窗口（交易日）
    s2_upper_ratio: float = 0.30      # 上影 / 振幅 下限
    s2_next_pct_max: float = 3.0      # 次日涨幅上限（惯性但幅度不大）
    # --- S3 缩量洗盘
    s3_shrink_ratio: float = 0.5      # 相对 S1 日缩量比例（≤ 此值算缩量）
    s3_min_days: int = 2              # 连续缩量天数下限
    s3_body_max: float = 3.0          # 实体振幅上限 %
    # --- S4 换手率趋稳
    s4_window: int = 5                # 换手率观察窗口
    s4_turnover_range: float = 1.5    # 窗口内换手率极差上限（百分点）
    # --- S5 长下影护盘
    s5_lower_body_mult: float = 2.0   # 下影 / 实体
    s5_lower_ratio: float = 0.50      # 下影 / 振幅 下限
    # --- S6 量价齐升
    s6_vol_mult: float = 1.5          # 量 / 前5日均量
    s6_turnover_mult: float = 2.0     # 换手率 / 上一阶段均换手
    s6_max_days: int = 2              # S5 后观察窗口
    # --- S7 确认
    s7_turnover_keep: float = 1.0     # 换手率 / S6 日换手（≥ 此值算维持）
    # --- 破位
    break_below_support: float = 0.97  # 收盘低于护盘底线×此值 → 破位重置


@dataclass
class Bar:
    """状态机用的一根日线。turnover 为换手率 %（缺失按 None 处理）。"""

    d: str
    o: float
    h: float
    l: float
    c: float
    v: float
    turnover: float | None = None
    pct: float | None = None

    # ---- K 线解剖（本模型的基础量，单位与价格一致）
    @property
    def rng(self) -> float:
        return max(self.h - self.l, 0.0)

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def upper(self) -> float:
        return max(self.h - max(self.o, self.c), 0.0)

    @property
    def lower(self) -> float:
        return max(min(self.o, self.c) - self.l, 0.0)

    @property
    def is_up(self) -> bool:
        return self.c >= self.o


@dataclass
class Transition:
    """一次状态流转。写日志、UI 展示「历史流转」都用它。"""

    d: str
    frm: Stage
    to: Stage
    reason: str
    price: float
    turnover: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.d,
            "from": self.frm.value,
            "to": self.to.value,
            "stage_cn": STAGE_CN[self.to],
            "reason": self.reason,
            "price": round(self.price, 2),
            "turnover": None if self.turnover is None else round(self.turnover, 2),
        }


@dataclass
class AccResult:
    """一只股票的状态机结果。"""

    code: str
    as_of: str
    stage: Stage = Stage.IDLE
    support: float | None = None          # 护盘底线价（S5 确认后才有）
    entry_low: float | None = None        # S7 建议入场区间下沿
    entry_high: float | None = None       # S7 建议入场区间上沿
    stop: float | None = None             # 止损位（跌破护盘底线）
    confirm_date: str | None = None       # S7 确认日
    tag: str = ""                          # 自选股列表标签
    transitions: list[Transition] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_buy(self) -> bool:
        """是否构成确定性买点（S7）。"""
        return self.stage is Stage.S7_CONFIRMED and self.entry_low is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "as_of": self.as_of,
            "stage": self.stage.value,
            "stage_cn": STAGE_CN[self.stage],
            "tag": self.tag,
            "is_buy": self.is_buy,
            "support": None if self.support is None else round(self.support, 2),
            "entry": (None if self.entry_low is None
                      else [round(self.entry_low, 2), round(self.entry_high, 2)]),
            "stop": None if self.stop is None else round(self.stop, 2),
            "confirm_date": self.confirm_date,
            "notes": self.notes,
            "transitions": [t.to_dict() for t in self.transitions],
        }


# ================================================================== 工具函数
def _to_bars(rows: list[dict]) -> list[Bar]:
    """原始行 -> Bar，剔除停牌/脏数据。保持输入顺序（调用方应已按日期升序）。"""
    out: list[Bar] = []
    for r in rows:
        try:
            o, h = float(r["open"]), float(r["high"])
            low, c = float(r["low"]), float(r["close"])
            v = float(r.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if h <= 0 or low <= 0 or c <= 0 or h < low:
            continue
        tv = r.get("turnover")
        try:
            tv = None if tv is None else float(tv)
        except (TypeError, ValueError):
            tv = None
        pc = r.get("pct_chg")
        try:
            pc = None if pc is None else float(pc)
        except (TypeError, ValueError):
            pc = None
        out.append(Bar(d=str(r.get("trade_date") or r.get("d") or ""),
                       o=o, h=h, l=low, c=c, v=v, turnover=tv, pct=pc))
    return out


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pct_change(prev_c: float, c: float) -> float:
    return (c / prev_c - 1) * 100 if prev_c > 0 else 0.0


def _avg_vol(bars: list[Bar], i: int, n: int = 5) -> float | None:
    """截至 i（不含 i）的前 n 日均量。"""
    seg = bars[max(0, i - n):i]
    return _mean([b.v for b in seg]) if seg else None


def _avg_turnover(bars: list[Bar], i: int, n: int = 5) -> float | None:
    seg = [b.turnover for b in bars[max(0, i - n):i] if b.turnover is not None]
    return _mean(seg) if seg else None


# ================================================================== 状态机
def evaluate(code: str, rows: list[dict], as_of: datetime | str,
             params: AccParams | None = None) -> AccResult:
    """对一只股票的日线序列跑 S1→S7 状态机。

    :param rows: 日线，**按交易日升序**，每项含
                 open/high/low/close/volume/turnover/pct_chg/trade_date
    :param as_of: 时点（铁律二：时间由调用方注入，本函数不取系统时间）
    """
    p = params or AccParams()
    as_of_str = (as_of.isoformat(timespec="seconds")
                 if isinstance(as_of, datetime) else str(as_of))
    bars = _to_bars(rows)
    res = AccResult(code=code, as_of=as_of_str)
    if len(bars) < 6:
        res.notes.append(f"日线不足 6 根（实得 {len(bars)}），无法判定")
        return res

    stage = Stage.IDLE
    s1_i: int | None = None          # S1 所在下标
    s2_i: int | None = None          # 上影线确认下标
    s3_i: int | None = None          # 缩量洗盘确认下标
    s5_i: int | None = None          # 护盘底线确认下标
    s6_i: int | None = None          # 量价齐升确认下标

    def to(new: Stage, i: int, reason: str) -> None:
        nonlocal stage
        res.transitions.append(Transition(
            d=bars[i].d, frm=stage, to=new, reason=reason,
            price=bars[i].c, turnover=bars[i].turnover))
        stage = new

    def reset(i: int, reason: str) -> None:
        nonlocal stage, s1_i, s2_i, s3_i, s5_i, s6_i
        if stage is not Stage.IDLE:
            res.transitions.append(Transition(
                d=bars[i].d, frm=stage, to=Stage.IDLE, reason=reason,
                price=bars[i].c, turnover=bars[i].turnover))
        stage = Stage.IDLE
        s1_i = s2_i = s3_i = s5_i = s6_i = None

    for i in range(1, len(bars)):
        b = bars[i]
        prev = bars[i - 1]
        pct = b.pct if b.pct is not None else _pct_change(prev.c, b.c)
        av5 = _avg_vol(bars, i, 5)
        at5 = _avg_turnover(bars, i, 5)

        # ---------------- 仍在形态中：先检查破位 ------------------
        if stage is not Stage.IDLE and stage is not Stage.S7_CONFIRMED:
            if res.support is not None and b.c < res.support * p.break_below_support:
                reset(i, f"收盘 {b.c:.2f} 跌破护盘底线 "
                         f"{res.support:.2f}（>{1 - p.break_below_support:.0%}），形态破坏")
                continue
            # S1 基准价失守（跌破 S1 日最低）同样视为破位
            if stage in (Stage.S2_UPPER_SHADOW,) and s1_i is not None:
                if b.c < bars[s1_i].l:
                    reset(i, f"收盘跌破 S1 日最低 {bars[s1_i].l:.2f}，形态破坏")
                    continue

        # ---------------- IDLE：寻找 S1 ------------------
        if stage is Stage.IDLE:
            if av5 and av5 > 0 and b.v >= av5 * p.s1_vol_mult:
                near_high = (b.rng <= 0) or \
                    ((b.c - b.l) / b.rng >= p.s1_close_near_high)
                if pct >= p.s1_pct_min:
                    s1_i = i
                    res.transitions.append(Transition(
                        d=b.d, frm=Stage.IDLE, to=Stage.S1_SURGE,
                        reason=f"放量爆拉：量 {b.v:,.0f} 达前5日均量 {av5:,.0f} 的 "
                               f"{b.v / av5:.1f} 倍，涨 {pct:.1f}%",
                        price=b.c, turnover=b.turnover))
                    stage = Stage.S1_SURGE
                elif pct >= p.s1_pct_relax and near_high:
                    s1_i = i
                    res.transitions.append(Transition(
                        d=b.d, frm=Stage.IDLE, to=Stage.S1_SURGE,
                        reason=f"放量拉升（放宽分支）：量 {b.v / av5:.1f} 倍，"
                               f"涨 {pct:.1f}% 且收盘接近最高价",
                        price=b.c, turnover=b.turnover))
                    stage = Stage.S1_SURGE
            continue

        # ---------------- S1 -> S2：上影线堆叠 ------------------
        if stage is Stage.S1_SURGE:
            if s1_i is not None and i - s1_i > p.s2_max_days:
                reset(i, f"S1 后 {p.s2_max_days} 日内未见上影线堆叠，形态失效")
                continue
            has_upper = (b.upper >= b.body and b.rng > 0
                         and b.upper / b.rng >= p.s2_upper_ratio)
            capped = pct <= p.s2_next_pct_max
            if has_upper and capped:
                s2_i = i
                res.transitions.append(Transition(
                    d=b.d, frm=Stage.S1_SURGE, to=Stage.S2_UPPER_SHADOW,
                    reason=f"上影线堆叠：上影 {b.upper:.2f} ≥ 实体 {b.body:.2f}，"
                           f"占振幅 {b.upper / b.rng:.0%}；涨 {pct:.1f}% 受限",
                    price=b.c, turnover=b.turnover))
                stage = Stage.S2_UPPER_SHADOW
            continue

        # ---------------- S2 -> S3：缩量洗盘 ------------------
        if stage is Stage.S2_UPPER_SHADOW:
            if s1_i is None:
                reset(i, "S1 基准丢失")
                continue
            base_v = bars[s1_i].v
            shrunk = base_v > 0 and b.v <= base_v * p.s3_shrink_ratio
            small_body = b.rng > 0 and (b.body / b.c * 100) <= p.s3_body_max
            short_upper = b.rng > 0 and b.upper / b.rng <= 0.5
            if shrunk and small_body and short_upper:
                # 连续天数检查：从当前往前数连续满足的交易日
                run = 0
                j = i
                while j > (s1_i or 0):
                    bb = bars[j]
                    ok = (base_v > 0 and bb.v <= base_v * p.s3_shrink_ratio
                          and bb.rng > 0
                          and (bb.body / bb.c * 100) <= p.s3_body_max)
                    if not ok:
                        break
                    run += 1
                    j -= 1
                if run >= p.s3_min_days:
                    s3_i = i
                    res.transitions.append(Transition(
                        d=b.d, frm=Stage.S2_UPPER_SHADOW, to=Stage.S3_SHRINK_WASH,
                        reason=f"缩量洗盘：较 S1 日缩量至 {b.v / base_v:.0%}"
                               f"（连续 {run} 日），实体振幅 "
                               f"{b.body / b.c * 100:.1f}% 且短上影",
                        price=b.c, turnover=b.turnover))
                    stage = Stage.S3_SHRINK_WASH
            continue

        # ---------------- S3 -> S4：换手率趋稳 ------------------
        if stage is Stage.S3_SHRINK_WASH:
            seg = [x.turnover for x in bars[max(0, i - p.s4_window + 1):i + 1]
                   if x.turnover is not None]
            if len(seg) >= min(3, p.s4_window):
                rng_tv = max(seg) - min(seg)
                declining = b.c <= prev.c or (i >= 2 and bars[i - 2].c >= b.c)
                if rng_tv <= p.s4_turnover_range and declining:
                    res.transitions.append(Transition(
                        d=b.d, frm=Stage.S3_SHRINK_WASH, to=Stage.S4_STABLE_TURNOVER,
                        reason=f"换手率趋稳：近 {len(seg)} 日极差 {rng_tv:.2f} 个百分点"
                               f"（≤ {p.s4_turnover_range}），股价阴跌但抛压出清",
                        price=b.c, turnover=b.turnover))
                    stage = Stage.S4_STABLE_TURNOVER
            continue

        # ---------------- S4 -> S5：长下影护盘 ------------------
        if stage is Stage.S4_STABLE_TURNOVER:
            long_lower = (b.body > 0 and b.lower >= b.body * p.s5_lower_body_mult
                          and b.rng > 0
                          and b.lower / b.rng >= p.s5_lower_ratio)
            if long_lower:
                s5_i = i
                res.support = b.l
                res.stop = b.l * p.break_below_support
                res.transitions.append(Transition(
                    d=b.d, frm=Stage.S4_STABLE_TURNOVER, to=Stage.S5_SUPPORT,
                    reason=f"长下影护盘：下影 {b.lower:.2f} ≥ 实体×"
                           f"{p.s5_lower_body_mult:g}，占振幅 "
                           f"{b.lower / b.rng:.0%}；护盘底线 {b.l:.2f}",
                    price=b.c, turnover=b.turnover))
                stage = Stage.S5_SUPPORT
            continue

        # ---------------- S5 -> S6：量价齐升 ------------------
        if stage is Stage.S5_SUPPORT:
            if s5_i is not None and i - s5_i > p.s6_max_days:
                reset(i, f"S5 后 {p.s6_max_days} 日内未量价齐升，形态失效")
                continue
            vol_ok = av5 is not None and av5 > 0 and b.v >= av5 * p.s6_vol_mult
            price_ok = b.c > prev.c and pct > 0
            tv_ok = True
            if at5 is not None and at5 > 0 and b.turnover is not None:
                tv_ok = b.turnover >= at5 * p.s6_turnover_mult
            if vol_ok and price_ok and tv_ok:
                s6_i = i
                tv_txt = (f"，换手 {b.turnover:.2f}% 达前5日均 {at5:.2f}% 的 "
                          f"{b.turnover / at5:.1f} 倍"
                          if (at5 and b.turnover is not None) else "")
                res.transitions.append(Transition(
                    d=b.d, frm=Stage.S5_SUPPORT, to=Stage.S6_VOLUME_PRICE,
                    reason=f"量价齐升：量达前5日均量 {b.v / av5:.1f} 倍，"
                           f"涨 {pct:.1f}%{tv_txt}",
                    price=b.c, turnover=b.turnover))
                stage = Stage.S6_VOLUME_PRICE
            continue

        # ---------------- S6 -> S7：确认进场 ------------------
        if stage is Stage.S6_VOLUME_PRICE:
            if s6_i is None:
                reset(i, "S6 基准丢失")
                continue
            base_tv = bars[s6_i].turnover
            keep = True
            if base_tv and b.turnover is not None:
                keep = b.turnover >= base_tv * p.s7_turnover_keep
            rising = b.c > prev.c and pct > 0
            if keep and rising:
                res.confirm_date = b.d
                # 入场区间：以确认日收盘与当日振幅为参考
                res.entry_low = min(b.c, b.o)
                res.entry_high = max(b.c, b.h)
                res.transitions.append(Transition(
                    d=b.d, frm=Stage.S6_VOLUME_PRICE, to=Stage.S7_CONFIRMED,
                    reason=f"确认进场：换手率维持高位"
                           f"（{b.turnover:.2f}% vs S6 日 "
                           f"{base_tv:.2f}%），股价继续走高 {pct:.1f}%"
                           if (b.turnover is not None and base_tv)
                           else "确认进场：换手率维持且股价走高",
                    price=b.c, turnover=b.turnover))
                stage = Stage.S7_CONFIRMED
            elif not rising:
                reset(i, "S6 次日股价未走高，未确认")
            continue

        # S7 之后不再推进（由用户决策），保持确认状态
    res.stage = stage
    res.tag = STAGE_TAG.get(stage, "")
    if res.support is not None:
        res.notes.append(f"护盘底线 {res.support:.2f}；跌破 "
                         f"{res.stop:.2f} 视为形态破坏")
    if stage is Stage.S3_SHRINK_WASH:
        res.notes.append("缩量洗盘中：主力控盘吸筹，暂不宜追")
    elif stage is Stage.S7_CONFIRMED:
        res.notes.append("已确认进场：注意次日是否延续，跌破护盘底线止损")
    elif stage is Stage.IDLE:
        res.notes.append("当前无吸筹形态")
    return res


# ================================================================== 批量
def scan(rows_by_code: dict[str, list[dict]], as_of: datetime | str,
         params: AccParams | None = None) -> dict[str, AccResult]:
    """对多只股票跑状态机。用于自选股列表打标签 / 邮件持仓策略。"""
    return {code: evaluate(code, rows, as_of, params)
            for code, rows in rows_by_code.items()}


def summary_table(results: dict[str, AccResult]) -> list[dict[str, Any]]:
    """给 UI / 邮件用的紧凑表格：只保留在形态中的。"""
    out = []
    for code, r in results.items():
        if r.stage is Stage.IDLE:
            continue
        out.append({
            "code": code,
            "stage": r.stage.value,
            "stage_cn": STAGE_CN[r.stage],
            "tag": r.tag,
            "is_buy": r.is_buy,
            "support": r.support,
            "entry": (None if r.entry_low is None
                      else [r.entry_low, r.entry_high]),
        })
    # 越靠后的阶段越值得关注
    order = list(Stage)
    out.sort(key=lambda x: order.index(Stage(x["stage"])), reverse=True)
    return out


if __name__ == "__main__":                       # pragma: no cover
    import json

    def _mk(rows_in: list[tuple]) -> list[dict]:
        return [
            {"trade_date": f"D{i + 1:03d}", "open": o, "high": h, "low": low,
             "close": c, "volume": v, "turnover": tv}
            for i, (o, h, low, c, v, tv) in enumerate(rows_in)
        ]

    demo = _mk([
        # 20 日平静期（构成前5日均量的基数）
        *[(10, 10.1, 9.9, 10, 1000, 1.0) for _ in range(20)],
        (10, 11.2, 10.0, 11.0, 3000, 4.0),    # S1 放量爆拉 +10%
        (11, 11.3, 10.8, 11.0, 1800, 3.0),    # S2 上影线堆叠
        (11, 11.05, 10.9, 10.95, 1200, 2.0),  # S3 缩量
        (10.9, 11.0, 10.85, 10.9, 1100, 1.9),  # S3 连续缩量
        (10.9, 10.98, 10.8, 10.85, 1000, 1.85),  # S3 连续缩量
        (10.85, 10.9, 10.75, 10.78, 950, 1.8),  # S4 换手趋稳 + 阴跌
        (10.78, 11.1, 10.3, 10.9, 1300, 2.0),  # S5 长下影护盘，底线 10.30
        (10.9, 11.4, 10.9, 11.3, 2200, 4.2),   # S6 量价齐升
        (11.3, 11.8, 11.2, 11.7, 2400, 4.5),   # S7 确认进场
    ])
    r = evaluate("DEMO", demo, datetime(2026, 10, 9, 15, 0))
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
