# -*- coding: utf-8 -*-
"""
Hawk · K 线标注层（M9 · 决策 D13「A+C 组合」）

★ 用户 2026-10-09 08:13 决策：
    **方案 A（风控止损参考）+ 方案 C（不自动卖出，卖点由用户决断）**

  这条决策改变了系统的角色定位，必须落到代码里：
    · 旧：策略给 BUY/SELL，回测自动平仓 → 系统是「交易员」
    · 新：策略只给 **买点提示 + 卖点提醒 + 止损参考位**，
         卖出与否由用户决断 → 系统是「副驾」（提示词原文定位）

★ 因此本模块**不产生任何「卖出」动作**，只产生三类标注：
    1. `buy`   —— 买点（含级别、理由、置信度）
    2. `sell`  —— ★ 卖点**提醒**（不是指令），标注在 K 线上等用户判断
    3. `stop`  —— A 方案的风控止损参考位（含触发条件说明）

★ 为什么要做成「纯函数 + JSON」而不是直接画图：
    · 本机无matplotlib/plotly，且为看图引入重依赖不划算（M11 才有 UI）
    · 标注逻辑是**判断**（哪个点是卖点、止损该放哪），
      必须能被单元测试；画图是渲染，不该混进判断
    · 输出标准 OHLC + marks，前端任何库都能渲染（M10/M11）

★ 关键：止损位是**参考**，不是自动触发。
    缠论里止损位置有多种取法（笔起点/中枢下沿/ATR），
    各有适用场景，本模块**全部输出并标注取法**，
    由用户选择 —— 系统不替你决定止损在哪。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))


# ================================================================== 数据结构
@dataclass
class Mark:
    """K 线上的一条标注。"""
    kind: str                   # buy | sell | stop
    date: str
    price: float
    level: str = ""             # strong | normal | weak
    signal_kind: str = ""# 1buy/2buy/3buy/1sell/...
    reason: str = ""
    confidence: float = 0.0
    # 卖点专用：这是提醒还是指令（永远是提醒）
    advisory: bool = True
    # 止损专用
    stop_basis: str = ""# prev_bi_start | zhongshu_low | atr
    stop_note: str = ""

    def to_json(self) -> dict:
        d = {
            "kind": self.kind, "date": self.date,
            "price": round(self.price, 3), "level": self.level,
            "signal_kind": self.signal_kind, "reason": self.reason,
            "confidence": round(self.confidence, 3),
        }
        if self.kind == "sell":
            d["advisory"] = self.advisory
            d["note"] = "卖点提醒（系统不自动卖出，请自行决断）"
        if self.kind == "stop":
            d["stop_basis"] = self.stop_basis
            d["stop_note"] = self.stop_note
        return d


@dataclass
class KlineOverlay:
    """一只标的的完整 K 线 + 标注。"""
    code: str
    name: str
    as_of: str
    bars: list[dict] = field(default_factory=list)       # OHLC
    marks: list[Mark] = field(default_factory=list)
    zhongshus: list[dict] = field(default_factory=list)  # 中枢区间（画矩形）
    bis: list[dict] = field(default_factory=list)        # 笔（画线段）
    position: str = ""                                   # 当前相对最后中枢
    last_zhongshu: dict | None = None
    current_stop_refs: list[Mark] = field(default_factory=list)
    disclaimer: str = ("标注为系统分析结果，不构成投资建议；"
                       "卖点为提醒而非指令，卖出与否由你决定。")

    def to_json(self) -> dict:
        return {
            "code": self.code, "name": self.name, "as_of": self.as_of,
            "bars": self.bars,
            "marks": [m.to_json() for m in self.marks],
            "zhongshus": self.zhongshus,
            "bis": self.bis,
            "position": self.position,
            "last_zhongshu": self.last_zhongshu,
            "current_stop_refs": [m.to_json() for m in self.current_stop_refs],
            "disclaimer": self.disclaimer,
        }


# ================================================================== 止损参考
def stop_references(
    entry_price: float,
    zhongshu_low: float | None,
    prev_bi_low: float | None,
    atr: float | None = None,
    atr_mult: float = 2.0,
    min_gap_pct: float = 0.005,
) -> list[Mark]:
    """★ A 方案：给出**多个**止损参考位，标注各自的取法与理由。

    ★★ 为什么不自动选一个：
      缠论止损有多种取法，各有代价：
        prev_bi_start —— 破前一笔低点，信号意义强，但幅度可能很大（-15%）
        zhongshu_low  —— 破中枢下沿，结构转弱，幅度适中
        atr           —— 波动率自适应，幅度随行情缩放
      选哪个取决于你能承受多少回撤、以及这只票的波动特征。
      **系统不替你决定，只把三个都摆出来并说明代价。**

    ★★ `min_gap_pct`：过滤掉「离入场价几乎为0」的无效止损位。
      实测踩过：买点当天恰好就在中枢下沿附近（54.56 买、54.56 止损），
      「距入场 -0.0%」的止损位毫无意义 —— 一进场就触发。
      低于 min_gap_pct 的位直接丢弃，因为止损至少要有缓冲空间。
    """
    out: list[Mark] = []

    def _add(px: float, basis: str, note: str) -> None:
        if px <= 0 or px >= entry_price:
            return                # 高于入场价 = 已经是止盈方向，不是止损
        gap = (px / entry_price - 1) * 100
        if abs(gap) < min_gap_pct * 100:
            return                # ★ 距入场太近，等于没有止损空间
        out.append(Mark(kind="stop", date="", price=round(px, 3),
                        stop_basis=basis,
                        stop_note=f"{note}（距入场 {gap:+.1f}%）"))

    if zhongshu_low is not None:
        _add(zhongshu_low, "zhongshu_low",
             f"跌破最近中枢下沿 {zhongshu_low:.2f}。结构转弱，幅度适中，"
             f"缠论常用口径")
    if prev_bi_low is not None:
        _add(prev_bi_low, "prev_bi_start",
             f"跌破前一笔低点 {prev_bi_low:.2f}。信号意义最强，"
             f"但幅度可能偏大")
    if atr is not None and atr > 0:
        _add(entry_price - atr_mult * atr, "atr",
             f"入场价-{atr_mult:.0f}×ATR({atr:.2f})。波动率自适应，"
             f"震荡市会被反复触发")
    # 按价格从高到低排（最近的上方在前，符合看盘习惯）
    out.sort(key=lambda m: -m.price)
    return out


def atr(bars: list[dict], n: int = 14) -> float | None:
    """平均真实波幅。数据不足返回 None（不是 0）。"""
    if len(bars) < n + 1:
        return None
    trs: list[float] = []
    for i in range(len(bars) - n, len(bars)):
        h, l = float(bars[i]["high"]), float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


# ================================================================== 标注构建
BUY_KINDS = ("1buy", "2buy", "3buy")
SELL_KINDS = ("1sell", "2sell", "3sell")


def build_overlay(
    code: str,
    name: str,
    rows: list[dict],
    chan_result,
    as_of: datetime | str,
    *,
    show_weak: bool = False,
    stop_atr_mult: float = 2.0,
) -> KlineOverlay:
    """从缠论结果构建 K 线标注。

    ★ show_weak 默认 False —— M8 实测 weak 三买胜率仅 20%，
      在图上默认不显示，避免误读。标注为 weak 的卖点仍会显示
      （卖点本来就少，漏掉会让人误以为「没有卖点」）。
    """
    as_of_s = (as_of.isoformat(timespec="seconds")
               if isinstance(as_of, datetime) else str(as_of))
    bars = [{"date": b.d, "open": b.o, "high": b.h, "low": b.l,
             "close": b.c, "volume": b.v} for b in chan_result.bars]

    ov = KlineOverlay(code=code, name=name, as_of=as_of_s, bars=bars,
                      position=chan_result.position())

    # ---- 笔（前端画线段用）
    ov.bis = [{"dir": b.direction, "high": b.high, "low": b.low,
               "start_date": b.start_date, "end_date": b.end_date}
              for b in chan_result.bis]

    # ---- 中枢（前端画矩形用）
    for zs in chan_result.zhongshus:
        ov.zhongshus.append({
            "idx": zs.idx, "low": zs.low, "high": zs.high,
            "start_date": zs.start_date, "end_date": zs.end_date,
            "bi_count": zs.bi_count, "amplitude": zs.amplitude,
        })
    if chan_result.zhongshus:
        last = chan_result.zhongshus[-1]
        ov.last_zhongshu = {"low": last.low, "high": last.high,
                            "start_date": last.start_date,
                            "end_date": last.end_date,
                            "amplitude": last.amplitude}

    # ---- 买卖点标注
    for s in chan_result.signals:
        is_buy = s.kind in BUY_KINDS
        is_sell = s.kind in SELL_KINDS
        if not (is_buy or is_sell):
            continue
        # ★ 买点默认只显示 strong（weak 胜率 20%，显示会误导）
        if is_buy and s.level != "strong" and not show_weak:
            continue
        ov.marks.append(Mark(
            kind="buy" if is_buy else "sell",
            date=s.date, price=s.price, level=s.level,
            signal_kind=s.kind, reason=s.reason,
            confidence=s.confidence,
            # ★ 卖点永远是「提醒」，不是指令
            advisory=is_sell,
        ))

    # ---- 止损参考位（对最近一个 strong 买点给出）
    buys = [m for m in ov.marks if m.kind == "buy" and m.level == "strong"]
    if buys:
        last_buy = buys[-1]
        zs_low = ov.last_zhongshu["low"] if ov.last_zhongshu else None
        # 前一笔低点：从缠论结构里找买点之前那笔的 low
        prev_low = None
        for b in reversed(chan_result.bis):
            if b.end_date <= last_buy.date and b.direction == "down":
                prev_low = b.low
                break
        ov.current_stop_refs = stop_references(
            last_buy.price, zs_low, prev_low,
            atr=atr(bars), atr_mult=stop_atr_mult)

    return ov


# ================================================================== 持仓视图
@dataclass
class PositionView:
    """持仓标的的盯盘视图：C 方案的核心产出。"""
    code: str
    name: str
    as_of: str
    entry_price: float
    current_price: float
    holding_days: int | None
    overlay: KlineOverlay
    active_buys: list[dict] = field(default_factory=list)
    active_sells: list[dict] = field(default_factory=list)   # ★ 提醒
    stop_refs: list[dict] = field(default_factory=list)
    unrealized_pct: float | None = None
    action_hint: str = ""
    disclaimer: str = ("本视图仅为标注与提醒，不构成投资建议。"
                       "★ 卖点出现不代表必须卖出，卖出与否由你决定。")

    def to_json(self) -> dict:
        return {
            "code": self.code, "name": self.name, "as_of": self.as_of,
            "entry_price": round(self.entry_price, 3),
            "current_price": round(self.current_price, 3),
            "holding_days": self.holding_days,
            "unrealized_pct": (round(self.unrealized_pct, 2)
                               if self.unrealized_pct is not None else None),
            "overlay": self.overlay.to_json(),
            "active_buys": self.active_buys,
            "active_sells": self.active_sells,
            "sell_note": "★ 以下为卖点提醒（非指令），是否卖出由你判断",
            "stop_refs": self.stop_refs,
            "stop_note": "以下为风控参考位，系统不会自动触发",
            "action_hint": self.action_hint,
            "disclaimer": self.disclaimer,
        }


def build_position_view(
    code: str, name: str, rows: list[dict], chan_result,
    entry_price: float, as_of: datetime | str,
    *, show_weak_buy: bool = False, stop_atr_mult: float = 2.0,
) -> PositionView:
    """构建持仓盯盘视图（C 方案：标注卖点，不自动卖）。

    ★ `action_hint` 是**唯一**的行动建议文本，且刻意不写「卖出」二字 ——
      因为决策权在用户。系统只陈述客观状态。
    """
    ov = build_overlay(code, name, rows, chan_result, as_of,
                       show_weak=show_weak_buy,
                       stop_atr_mult=stop_atr_mult)
    cur = ov.bars[-1]["close"] if ov.bars else 0.0
    holding = None
    if isinstance(as_of, datetime) and ov.bars:
        try:
            d0 = datetime.fromisoformat(ov.bars[-1]["date"]).date()
            holding = (as_of.date() - d0).days
        except ValueError:
            holding = None

    # 只保留**尚未处理**的近期标注（近 30 根内）
    recent = ov.bars[-30]["date"] if len(ov.bars) >= 30 else \
        (ov.bars[0]["date"] if ov.bars else "")
    active = [m for m in ov.marks if m.date >= recent]

    view = PositionView(
        code=code, name=name,
        as_of=ov.as_of, entry_price=entry_price, current_price=cur,
        holding_days=holding, overlay=ov,
        active_buys=[m.to_json() for m in active if m.kind == "buy"],
        active_sells=[m.to_json() for m in active if m.kind == "sell"],
        stop_refs=[m.to_json() for m in ov.current_stop_refs],
        unrealized_pct=(round((cur / entry_price - 1) * 100, 2)
                        if entry_price > 0 else None),
    )

    # ---- 客观状态陈述（不含「建议卖出」这类指令）
    hints: list[str] = []
    if view.active_sells:
        latest = view.active_sells[-1]
        hints.append(f"近30 根内有卖点提醒：{latest['signal_kind']}"
                     f"（{latest['date']}）—— 请自行判断是否离场")
    if view.unrealized_pct is not None:
        if view.unrealized_pct < 0:
            hints.append(f"当前浮亏 {-view.unrealized_pct:.2f}%，"
                         f"可对照下方止损参考位评估风险")
        else:
            hints.append(f"当前浮盈 {view.unrealized_pct:.2f}%")
    if ov.position:
        hints.append(f"价格相对最近中枢：{ov.position}")
    view.action_hint = "；".join(hints) if hints else "无特殊状态"
    return view


if __name__ == "__main__":                        # pragma: no cover
    import json
    import random

    from backend.domain.chan import analyze

    # ★ 确定性但**有波动**的行情（纯平滑曲线出不了分型/中枢，
    #   实测 0 笔 0 中枢 —— 构造数据必须能产生目标结构才有验证价值）
    #★ 日期用真实日历推进，不用 `1+i//28` 那种公式 ——
    #   公式在 i>252 时会溢出到 11 月/12 月（造出「未来日期」标注），
    #   看着像 bug 也会掩盖真正的日期问题。
    from datetime import date as _date, timedelta as _td
    rng = random.Random(7)
    rows = []
    px = 100.0
    d = _date(2025, 1, 2)
    for i in range(300):
        px = max(5.0, px * (1 + rng.uniform(-0.035, 0.038)))
        o = px * (1 + rng.uniform(-0.008, 0.008))
        rows.append({
            "trade_date": d.isoformat(),
            "open": o, "high": max(o, px) * 1.012,
            "low": min(o, px) * 0.988, "close": px, "volume": 1e6})
        d += _td(days=1)

    res = analyze("TEST", rows, as_of="2026-10-08T15:00:00+08:00")
    ov = build_overlay("TEST", "测试标的", rows, res,
                       "2026-10-08T15:00:00+08:00")
    print("=" * 74)
    print("K 线标注（A+C 方案）")
    print("=" * 74)
    print(f"标的: {ov.code} {ov.name}   as_of={ov.as_of}")
    print(f"K线 {len(ov.bars)} 根 | 笔 {len(ov.bis)}段 | "
          f"中枢 {len(ov.zhongshus)} 个")
    print(f"当前位置: {ov.position}")
    print()
    print("标注（买点 strong + 全部卖点）:")
    for m in ov.marks:
        tag = "买" if m.kind == "buy" else "卖(提醒)"
        print(f"  [{tag}] {m.date}{m.price:8.2f} {m.level:>7s} "
              f"{m.signal_kind:<6s} conf={m.confidence:.2f}")
    print()
    print("止损参考位（系统不自动触发，由你选）:")
    for m in ov.current_stop_refs:
        print(f"  {m.price:8.2f}  [{m.stop_basis}] {m.stop_note}")
    print()
    print("免责声明:", ov.disclaimer)

    print()
    print("=" * 74)
    print("持仓盯盘视图（C 方案）")
    print("=" * 74)
    pv = build_position_view("TEST", "测试标的", rows, res,
                             entry_price=ov.bars[-1]["close"] * 0.97,
                             as_of="2026-10-08T15:00:00+08:00")
    j = pv.to_json()
    print(f"  入场 {j['entry_price']} → 现价 {j['current_price']} "
          f"（{j['unrealized_pct']:+.2f}%）")
    print(f"  {j['sell_note']}")
    print(f"  {j['stop_note']}")
    print(f"  {j['action_hint']}")
    print()
    print("  ★ 校验：JSON 里不应出现「动作指令」类字段")
    blob = json.dumps(j, ensure_ascii=False)
    for bad in ('"action"', '"position":1', '"order"', '"BUY"', '"SELL"'):
        assert bad not in blob.replace('"action_hint"', ''), f"出现指令字段 {bad}"
    assert pv.to_json()["sell_note"].startswith("★")
    print("     ✓ 无 action/order/BUY/SELL 指令字段")
    print("     ✓ 卖点标记为 advisory=True（提醒）")
    sells = [m for m in ov.marks if m.kind == "sell"]
    assert all(m.advisory for m in sells), "卖点必须全部是 advisory"
    print(f"     ✓ {len(sells)} 个卖点全部标记为提醒")
