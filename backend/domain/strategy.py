# -*- coding: utf-8 -*-
"""
Hawk · 策略接口 + 注册表（M9 · 提示词 12.1「接口 + 注册表」铁律）

★★ 验收标准（M9 原文）：
   「**不改核心代码**即可接入新策略并跑出对比报告」

   所以这个模块必须做到：新增策略 = 写一个类 + 一行注册，
   不碰任何既有文件。为此：
   · 策略只依赖**数据快照**（StrategyContext），不直接查数据库
   · 所有阈值走params 字典，不硬编码
   · 注册表是`dict[str, type]`，注册后自动出现在对比/蒸馏/回测里

★为什么用「上下文快照」而不是让策略自己去取数：
   ① 回测时必须能注入 as_of 截断的历史数据；
   ② 若策略自己去查库，就没法保证所有因子取自同一时点（铁律二）；
   ③ 测试可以在没有数据库的情况下构造 Context 跑单测。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))


# ================================================================== 上下文
@dataclass
class StrategyContext:
    """策略求值所需的**全部**输入快照。

    ★ 一次性备齐所有因子，而不是让策略自己去查 ——
      这是「所有因子取自同一 as_of」的唯一可靠做法（附录 D.3 第4 条）。
    """
    code: str
    as_of: datetime

    # ---- 价格与均线
    closes: list[float] = field(default_factory=list)      # 升序
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)

    # ---- 属性（缺失必须是 None，不能是 0 —— 缺失与 0 语义完全不同）
    is_st: bool | None = None
    is_suspend: bool | None = None
    is_new: bool | None = None# 上市不足 N 日
    float_mv: float | None = None
    total_mv: float | None = None
    turnover: float | None = None
    pct_chg_today: float | None = None
    rps: float | None = None

    # ---- 大盘/板块
    market_state: str = ""
    market_score: float | None = None
    board_rank: int | None = None
    board_strength: float | None = None
    board_rank_change: int | None = None

    # ---- 缠论（由 chan.analyze 的结果抽取，调用方负责填）
    chan_signals: list[dict] = field(default_factory=list)
    chan_zhongshu_pos: str = ""# above | inside | below | ""
    beichi_types: list[str] = field(default_factory=list)
    chan_stage: str = ""# bi | segment | zhongshu | ""

    # ---- 消息
    news_direction: str = ""             # bull | bear | neutral
    news_strength: float | None = None
    news_within_days: int | None = None
    news_recent_direction: str = ""

    # ---- 涨跌停
    limit_up_days: int | None = None     # 近 N 日内涨停次数；None=未知
    # ★★ 当日**市场级**涨跌停家数。
    #   为什么必须显式传进来、而不是在 `loss_area_ratio` 里现算：
    #   那需要全市场横截面，而 `StrategyContext` 只持有**单只股票**的序列。
    #   ⇒ 不显式传 ⇒ `loss_area_ratio` 永远是 None ⇒ DSL 里
    #     「亏钱效应面积 > 30%」这条谓词**永不触发**且不报错（哑弹）。
    #   实测2026-10-11：字段/谓词/计算函数三层都齐，
    #   但生产链路无一处赋值 —— 「定义了 ≠ 接上了」。
    limit_up_today: int | None = None    # 当日涨停家数（排除 ST）
    limit_down_today: int | None = None  # 当日跌停家数（包含 ST）
    market_sample: int | None = None# 当日全市场有效样本数

    # ---- 亏钱效应（市场级，非个股）
    # ★ 由 `breadth.loss_area_ratio()` 算好后填入，比例（0~1，非百分数）。
    #   None = 当日无涨跌停/算不出⇒ 缺失，绝不当 0。
    loss_area_ratio: float | None = None
    loss_area_denom: str = ""            # 记下用的哪个分母，供UI 如实展示

    # ---- 涨跌幅序列（派生，便于 pct_change_n / 穿越判定）
    pct_changes: list[float] = field(default_factory=list)

    def last_close(self) -> float | None:
        return self.closes[-1] if self.closes else None

    def ma(self, n: int) -> float | None:
        """N 日均线。数据不足返回 None（★ 不是 0）。"""
        if n <= 0 or len(self.closes) < n:
            return None
        return sum(self.closes[-n:]) / n

    def price_vs_ma(self, n: int) -> float | None:
        """价格相对 N 日均线的偏离，小数（-0.01 = -1%）。"""
        m = self.ma(n)
        if m is None or m <= 0:
            return None
        return (self.closes[-1] / m) - 1.0

    def vol_ratio(self, n: int, exclude_current: bool = True) -> float | None:
        """当前量 / N 日均量。均量为 0 时返回 None（分母趋零会产生无意义巨值）。

        ★★ exclude_current 默认 True —— 分母**不含**当前这根。
          含进去的话，单根暴量会被自己的分母摊平：
            N=5，当日量是平常 50 倍时，比值也只有
            50/ (4 + 50) * 5 ≈ 4.6，永远达不到「> 5」的暴量阈值
            ⇒ 巨量出货检测**永远不会触发**（实测发现）。
          附：若不排除当前，最大比值恒为 N/(N-1)（趋近 1.25 for N=5），
          阈值设成 1.25 以上就等于永远不触发。
        """
        if n <= 0 or len(self.volumes) < n:
            return None
        if exclude_current:
            # ★ 必须用 [-(n+1):-1] 去掉当前这根。
            #   写成 v[-n-1:] 会多取一根、把当前量也放进分母，
            #   摊薄效果又回来了（实测：21 根时取到 21 根，均值被抬高）。
            if len(self.volumes) < n + 1:
                return None
            base = self.volumes[-(n + 1):-1]
        else:
            if len(self.volumes) < n:
                return None
            base = self.volumes[-n:]
        avg = sum(base) / len(base)
        if avg <= 0:
            return None
        return self.volumes[-1] / avg

    def pct_change_n(self, n: int) -> float | None:
        """近 N 日累计涨跌幅（小数）。"""
        if n <= 0 or len(self.closes) < n + 1:
            return None
        base = self.closes[-(n + 1)]
        if base <= 0:
            return None
        return (self.closes[-1] / base) - 1.0

    def crossed_above(self, n: int, idx: int = -1) -> bool | None:
        """价格上穿 N 日均线：上一根未穿、当前根已穿。

        返回 None 表示数据不足（调用方须判 False并记 warning）。
        """
        if n <= 0 or len(self.closes) < n + abs(idx) + 1:
            return None
        def above(i: int) -> bool | None:
            if i - n < 0:
                return None
            ma = sum(self.closes[i - n + 1: i + 1]) / n
            return self.closes[i] > ma
        cur, prev = above(idx), above(idx - 1)
        if cur is None or prev is None:
            return None
        return bool(cur and not prev)


# ================================================================== 决策
@dataclass
class StrategyDecision:
    """策略输出。**必须能解释**（第十二章第 4 条）。"""
    strategy_id: str
    code: str
    as_of: str
    action: str                  # BUY | SELL | HOLD
    score: float = 0.0
    position: float = 0.0        # 建议仓位 0~1
    reasons: list[str] = field(default_factory=list)   # 人类可读的触发链
    ref_rules: list[str] = field(default_factory=list)  # 命中的规则 id
    risk_note: str = ""
    stop_loss: str = ""          # 语义名而非价格：prev_bi_start / atr_2 ...
    take_profit: list[float] = field(default_factory=list)
    max_hold_days: int | None = None

    def to_json(self) -> dict:
        return {
            "strategy_id": self.strategy_id, "code": self.code,
            "as_of": self.as_of, "action": self.action,
            "score": round(self.score, 4), "position": round(self.position, 4),
            "reasons": self.reasons, "ref_rules": self.ref_rules,
            "risk_note": self.risk_note, "stop_loss": self.stop_loss,
            "take_profit": self.take_profit, "max_hold_days": self.max_hold_days,
        }


# ================================================================== 接口
class TraderStrategy(ABC):
    """策略基类（M9）。

    子类只需实现 `decide`，可选覆盖 `params` / `validate`。
    注册：`` @register class MyStrat(TraderStrategy): ... ``
    """

    strategy_id: str = ""
    name: str = ""
    version: str = "1"
    timeframe: str = "1d"

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = {**self.default_params(), **params}

    def default_params(self) -> dict[str, Any]:
        """默认阈值。子类覆盖它来暴露可调参数。"""
        return {}

    # ---- 子类必须实现
    @abstractmethod
    def decide(self, ctx: StrategyContext) -> StrategyDecision:
        """纯函数：给定快照输出决策。禁止取系统时间/查库。"""

    # ---- 供 UI/回测消费
    def describe(self) -> dict:
        return {
            "strategy_id": self.strategy_id, "name": self.name,
            "version": self.version, "timeframe": self.timeframe,
            "params": self.params,
            "param_hash": self.param_hash(),
        }

    def param_hash(self) -> str:
        """参数指纹（附录 7.2 参数版本冻结）。"""
        import hashlib
        import json
        blob = json.dumps(self.params, sort_keys=True, ensure_ascii=False,
                          default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ================================================================== 注册表
_REGISTRY: dict[str, type[TraderStrategy]] = {}


def register(cls: type[TraderStrategy]) -> type[TraderStrategy]:
    """类装饰器：把策略登记到注册表。"""
    if not getattr(cls, "strategy_id", ""):
        raise ValueError(f"{cls.__name__} 缺少 strategy_id")
    if cls.strategy_id in _REGISTRY and _REGISTRY[cls.strategy_id] is not cls:
        raise ValueError(f"strategy_id '{cls.strategy_id}' 已被占用"
                         f"（{_REGISTRY[cls.strategy_id].__name__}）")
    _REGISTRY[cls.strategy_id] = cls
    return cls


def get_strategy(strategy_id: str) -> TraderStrategy:
    """按 id 取策略实例。"""
    if strategy_id not in _REGISTRY:
        raise KeyError(f"未注册的策略: {strategy_id}；已注册: "
                       f"{sorted(_REGISTRY)}")
    return _REGISTRY[strategy_id]()


def all_strategies() -> dict[str, type[TraderStrategy]]:
    """全部已注册策略（dict 是副本，改它不影响注册表）。"""
    return dict(_REGISTRY)


def unregister(strategy_id: str) -> None:
    """注销（测试用）。"""
    _REGISTRY.pop(strategy_id, None)


# ================================================================== 基线策略
@register
class ChanlunBase(TraderStrategy):
    """`chanlun_base` 基线策略（M9 验收要求）。

    规则：缠论结构健康 + 大盘不拖累 → BUY
      1. 有 strong 级别买点（1buy/2buy/3buy）
      2. 价格在中枢上方（结构不弱）
      3. 市场状态不是「退潮/冰点」
      4. 量比不极端（缩量涨停、暴量出货都排除）

    ★ 这是**基线**，不是最优解 —— M10 的蒸馏策略要与它对比，
      胜率低于基线的规则一律不上线。
    """

    strategy_id = "chanlun_base"
    name = "缠论基线"
    version = "1"
    timeframe = "1d"

    def default_params(self) -> dict[str, Any]:
        return {
            "buy_levels": ["strong"],
            # ★ 卖点级别必须与买点分开配置。
            #   原实现用 buy_levels 去筛卖点（见 decide 中的修复说明），
            #   改买点级别会静默改变卖点行为。默认与买点同档，保持既有语义。
            "sell_levels": ["strong"],
            "buy_kinds": ["1buy", "2buy", "3buy"],
            "sell_kinds": ["1sell", "2sell", "3sell"],
            "require_above_zhongshu": True,
            "block_states": ["退潮", "冰点"],
            "vol_ratio_max": 5.0,
            "vol_ratio_min": 0.2,
            "base_position": 0.3,
            "score_buy_hit": 40.0,
            "score_state_penalty": 20.0,
            "score_vol_penalty": 15.0,
        }

    def decide(self, ctx: StrategyContext) -> StrategyDecision:
        p = self.params
        d = StrategyDecision(
            strategy_id=self.strategy_id, code=ctx.code,
            as_of=ctx.as_of.isoformat(timespec="seconds"), action="HOLD",
            position=0.0)

        # ---- 硬性前置：停牌 / ST 直接不参与
        if ctx.is_suspend:
            d.action, d.risk_note = "HOLD", "停牌，不可交易"
            d.reasons.append("停牌")
            return d
        if ctx.is_st:
            d.action, d.risk_note = "HOLD", "ST 股，风险过高"
            d.reasons.append("ST 股")
            return d

        # ---- 卖点优先（先判断离场，避免买入信号掩盖风险）
        # ★★ 修复：卖点级别原来错误地复用了 p["buy_levels"]。
        #   后果是「调高买点门槛」会连着把卖点门槛一起调高，
        #   卖点静默变少 —— 卖点变少意味着风险信号被吞掉，
        #   方向恰好是危险的那一侧。现改用独立的 sell_levels。
        sells = [s for s in ctx.chan_signals
                 if s.get("kind") in p["sell_kinds"]
                 and s.get("level") in p.get("sell_levels", p["buy_levels"])]
        if sells:
            d.action = "SELL"
            d.reasons.append(
                f"存在卖点信号 {sells[0].get('kind')}（{sells[0].get('date')}）")
            d.risk_note = "卖点信号触发，优先离场"
            d.score = -60.0
            return d

        # ---- 买入条件
        buys = [s for s in ctx.chan_signals
                if s.get("kind") in p["buy_kinds"]
                and s.get("level") in p["buy_levels"]]
        if not buys:
            #★ risk_note 必须始终有值：即使「不推荐」也要说清为什么不推荐，
            #   否则 UI 上是一句空的推荐位，用户无法判断该不该看。
            weak = [s for s in ctx.chan_signals
                    if s.get("kind") in p["buy_kinds"]
                    and s.get("level") not in p["buy_levels"]]
            if weak:
                d.risk_note = (
                    f"仅有弱级别买点（{weak[0].get('kind')}，未确认），"
                    f"按口径不算买入信号；需等回抽确认后的 strong 三买")
                d.reasons.append(f"弱级别买点 {weak[0].get('kind')} 不计入触发")
            else:
                d.risk_note = "当前无缠论买点信号，不构成买入理由"
            d.reasons.append("无 strong 级别买点")
            return d

        #★ 必须按「确认过的三买」口径：weak 是观察态，不算买点。
        b = buys[0]
        d.reasons.append(f"买点 {b.get('kind')}（{b.get('date')}）：{b.get('reason')}")
        d.score += p["score_buy_hit"]

        if p["require_above_zhongshu"] and ctx.chan_zhongshu_pos != "above":
            # 不直接否决，但要扣分并写进 risk_note（结构弱不等于不能买）
            d.score -= 25.0
            d.risk_note = (d.risk_note + " " if d.risk_note else "") + \
                f"价格在最近中枢{ctx.chan_zhongshu_pos or '未知'}，结构偏弱"
            d.reasons.append(f"中枢位置={ctx.chan_zhongshu_pos or '未知'}")

        if ctx.market_state in p["block_states"]:
            d.score -= p["score_state_penalty"]
            d.risk_note = (d.risk_note + " " if d.risk_note else "") + \
                f"市场处于{ctx.market_state}，建议降低仓位"
            d.reasons.append(f"市场状态={ctx.market_state}（扣分）")

        vr = ctx.vol_ratio(5)
        if vr is not None:
            if vr > p["vol_ratio_max"] or vr < p["vol_ratio_min"]:
                d.score -= p["score_vol_penalty"]
                d.risk_note = (d.risk_note + " " if d.risk_note else "") + \
                    f"量比 {vr:.2f} 异常（缩量滞涨或暴量出货）"
                d.reasons.append(f"量比={vr:.2f}（异常，扣分）")

        # ---- 仓位：由分数映射，且受市场状态上限约束
        pos = p["base_position"]
        if d.score >= 50:
            pos = min(0.5, pos * 1.5)
        elif d.score < 20:
            pos = pos * 0.5
        if ctx.market_state in ("高潮", "退潮", "冰点"):
            pos = min(pos, 0.2)
        d.position = round(pos, 4)

        d.action = "BUY" if d.score >= 20 else "HOLD"
        if d.action == "BUY":
            d.ref_rules.append(self.strategy_id)
        if not d.risk_note:
            d.risk_note = "结构与量能无明显异常"
        d.stop_loss = "prev_bi_start"
        d.take_profit = [0.08, 0.15]
        d.max_hold_days = 10
        return d


if __name__ == "__main__":                       # pragma: no cover
    print("=" * 70)
    print("M9 策略注册表")
    print("=" * 70)
    for sid, cls in sorted(all_strategies().items()):
        inst = cls()
        print(f"\n【{sid}】{cls.name} v{inst.version} @ {inst.timeframe}")
        print(f"  参数: {inst.describe()['params']}")
        print(f"  参数指纹: {inst.param_hash()}")

    # 冒烟：构造一个上下文跑一遍
    ctx = StrategyContext(
        code="600519",
        as_of=datetime(2026, 10, 8, 15, 0, tzinfo=CST),
        closes=[1400 + (i % 7) * 10 for i in range(120)],
        highs=[1410 + (i % 7) * 10 for i in range(120)],
        lows=[1390 + (i % 7) * 10 for i in range(120)],
        volumes=[1e6 * (1 + i % 3) for i in range(120)],
        dates=[f"2026-{1 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(120)],
        chan_signals=[{"kind": "3buy", "level": "strong", "date": "2026-10-08",
                       "reason": "回抽不破中枢上沿"}],
        chan_zhongshu_pos="above", market_state="修复",
        is_st=False, is_suspend=False)
    print("\n" + "=" * 70)
    print("冒烟测试：ChanlunBase")
    print("=" * 70)
    for d in get_strategy("chanlun_base").decide(ctx).reasons:
        print(f"  · {d}")
    dec = get_strategy("chanlun_base").decide(ctx)
    print(f"  → {dec.action} score={dec.score} position={dec.position}")
    print(f"  风险: {dec.risk_note}")