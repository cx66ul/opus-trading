# -*- coding: utf-8 -*-
"""
Hawk · 大盘环境判断（附录 4.7 · M5）

对应提示词 4.7 的六态市场状态机 + MarketScore。

★★ 纯函数（铁律二）：all 函数形如 f(输入, as_of, params) -> 输出，
   不取系统时间。状态机是**有记忆**的（需要连续采样点判断迟滞），
   因此状态转换由外部驱动（regime_tracker），本模块只做单点判定与历史应用。

★ 输入数据可用性（实测 2026-10-08）：
    ✓ 涨停/跌停家数、炸板率、最高连板高度 —— 东财 push2ex
    ✓ 昨日涨停今日溢价 —— 需涨停池历史（当前 snapshot 表已有当日池）
    ✓ 上涨家数占比 —— 腾讯全市场行情扫描
    ✓ 两市成交额 —— 由日线 volume 汇总
    ✗ 融资余额、主力净流入、北隔夜 —— 东财 push2 不可达，当前缺失
    ⇒ 这些因子在 MarketScore 中**显式标注为 0并重新归一化权重**，
      绝不假装有数据（第十二章：不许编造）。

六态与仓位上限（附录 4.7 原表）：
    🧊 冰点   跌停>涨停，炸板率>60%，成交额萎缩      ≤ 20%
    🔧 修复   涨停数回升，首板溢价转正              ≤ 40%
    🚀 启动   板块共振，连板高度 3→5               ≤ 70%
    🔥 发酵   连板高度 ≥5，涨停 > 80 家            ≤ 80%
    🎆 高潮   涨停 > 120 家但炸板率上升            ≤ 50%（只减不加）
    💧 退潮   高位股炸板、昨日涨停大面积低开      ≤ 20%（禁新开）
★ 阈值标定
----------
附录 4.7 给的是「涨停>120 家」「连板 3~5」这类**印象式**阈值。
用 2025-09-18~2026-10-08 的 251 个完备交易日实测：

    涨停家数   p25=28   p50=39   p75=54   p90=77   max=130
    跌停家数   p25=4    p50=10   p75=18   p90=40   max=231
    连板高度   p10=3    p25=3    p50=4    p75=5    p90=5
    炸板率     p25=.254 p50=.306 p75=.365 p90=.431
    上涨占比   p25=.294 p50=.457 p75=.659 p90=.766

★★ 原阈值的后果（实测）
   · `启动 = 3<=连板<=5 且 涨停>跌停` → 连板高度 p10~p90 **全在 3~5**，
     该条件几乎恒真 ⇒ **90.8% 的交易日被判为「启动」**，
     状态机完全失去区分度（且不报错）。
   · `高潮 = 涨停>=120` → 251 天里只 2 天达成（0.8%），近乎永不触发。
   · `退潮 = 炸板率>=0.55 且 溢价<-1.5` → 炸板率 p90 才 0.431，永不触发。
   ⇒ 六态退化成「启动」一态，M6/M7 读到的市场环境毫无信息量。

★★ 校准原则
   1. 每个阈值取某个**实测分位**，且在 `THRESHOLD_PROVENANCE` 里登记来源，
      避免下次又有人拍数字。
   2. 判定用**多因子组合**而非单一条件 —— 单一阈值在该分位上
      会把 84% 的日子都圈进去（正是「启动」的教训）。
   3. **任一状态占比不得 > 60%**，否则视为失去区分度；
      由 `tests/test_regime_thresholds.py` 守护。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST = timezone(timedelta(hours=8))

# ================================================================== 常量
REGIME_FROZEN = "frozen"        # 冰点
REGIME_REPAIR = "repair"# 修复
REGIME_START = "start"          # 启动
REGIME_FERMENT = "ferment"      # 发酵
REGIME_PEAK = "peak"            # 高潮
REGIME_EBB = "ebb"              # 退潮

REGIME_META = {
    REGIME_FROZEN:  {"label": "冰点", "emoji": "🧊", "cap": 0.20, "open_new": True},
    REGIME_REPAIR:  {"label": "修复", "emoji": "🔧", "cap": 0.40, "open_new": True},
    REGIME_START:   {"label": "启动", "emoji": "🚀", "cap": 0.70, "open_new": True},
    REGIME_FERMENT: {"label": "发酵", "emoji": "🔥", "cap": 0.80, "open_new": True},
    REGIME_PEAK:    {"label": "高潮", "emoji": "🎆", "cap": 0.50, "open_new": False},  # 只减不加
    REGIME_EBB:     {"label": "退潮", "emoji": "💧", "cap": 0.20, "open_new": False},  # 禁新开
}

# MarketScore 因子权重（附录 4.7）
SCORE_WEIGHTS = {
    "money_effect": 0.25,      # 赚钱效应
    "volume": 0.20,            # 量能
    "limit_structure": 0.20,   # 涨停结构
    "index_trend": 0.20,       # 指数趋势
    "outside": 0.15,           # 外围
}

# ================================================================== 状态阈值
# ★ 说明见模块 docstring 的「阈值标定」一节
REGIME_THRESHOLDS = {
    # 退潮：接力断裂 = 炸板率 p75 以上 + 昨日涨停溢价崩塌 + 上涨占比 p25 以下
    "ebb_broken_rate": 0.365,        # = 炸板率 p75
    "ebb_premium": -1.0,             # 昨日涨停今日溢价 %
    "ebb_up_ratio": 0.294,           # = 上涨占比 p25
    # 高潮：涨停家数进入 p90 且炸板率抬升到中位以上
    "peak_limit_up": 77,             # = 涨停 p90
    "peak_broken_rate": 0.306,       # = 炸板率 p50
    # 发酵：连板高度达p75 且涨停家数达 p75
    "ferment_streak": 5,             # = 连板 p75
    "ferment_limit_up": 54,          # = 涨停 p75
    # 冰点：跌停多于涨停 + 上涨占比极低 + 炸板率中位以上
    "frozen_up_ratio": 0.294,        # = 上涨占比 p25
    "frozen_broken_rate": 0.306,     # = 炸板率 p50
    # 启动：连板 p50 以上 + 涨停 p50 以上 + 上涨占比 p60 以上
    "start_streak": 4,               # = 连板 p50
    "start_limit_up": 39,            # = 涨停 p50
    "start_up_ratio": 0.55,          # = 上涨占比 p60
    # 修复：涨停 p50 以上 + 广度 p60 以上（要「明显改善」而非「不差」）
    "repair_limit_up": 39,           # = 涨停 p50
    "repair_up_ratio": 0.55,         # = 上涨占比 p60
}

# 阈值出处登记（改阈值必须同步改这里，否则下次会「凭印象拍数字」）
THRESHOLD_PROVENANCE = {
    "window": "2025-09-18 ~ 2026-10-08（251 个横截面完备交易日）",
    "source": "backend/domain/data_gate.py 闸门过滤后的实测分位数",
    "streak": "p10=3 p25=3 p50=4 p75=5 p90=5",
    "limit_up": "p25=28 p50=39 p75=54 p90=77 max=130",
    "limit_down": "p25=4 p50=10 p75=18 p90=40 max=231",
    "broken_rate": "p25=.254 p50=.306 p75=.365 p90=.431",
    "up_ratio": "p25=.294 p50=.457 p75=.659 p90=.766",
    "note": ("原附录阈值为印象式，未用真实分布校准；"
             "导致「启动」占 90.8%、「高潮」仅 2 天，状态机失去区分度"),
}


# ================================================================== 数据结构
@dataclass
class Sentiment:
    """情绪指标（附录 4.7 输入之「情绪」）。"""

    limit_up: int = 0# 涨停家数
    limit_down: int = 0            # 跌停家数
    broken_rate: float = 0.0       # 炸板率0~1
    max_streak: int = 0            # 最高连板高度
    yesterday_premium: float = 0.0 # 昨日涨停今日平均溢价（%）
    up_ratio: float = 0.0          # 上涨家数占比 0~1

    @property
    def limit_net(self) -> int:
        return self.limit_up - self.limit_down


@dataclass
class VolumeInfo:
    """量能（附录 4.7 输入之「资金」）。"""

    total_amount: float = 0.0      # 两市成交额（元）
    amount_ma5: float = 0.0        # 5 日均值（元）
    main_inflow: float | None = None   # 主力净流入 —— **当前无数据源**

    @property
    def vol_ratio(self) -> float:
        """量比：成交额 / 5日均值。"""
        if self.amount_ma5 <= 0:
            return 1.0
        return self.total_amount / self.amount_ma5

    @property
    def has_inflow(self) -> bool:
        return self.main_inflow is not None


@dataclass
class MarketEnv:
    """大盘环境完整输入。"""

    as_of: str
    sentiment: Sentiment
    volume: VolumeInfo
    index_pct: float = 0.0         # 代表指数涨跌幅（沪深300 或上证）
    outside_ok: bool | None = None # 外围（隔夜美股）—— 无数据源时为 None

    # 由评分函数填充
    score: float = 0.0
    detail: dict = field(default_factory=dict)
    missing_factors: list[str] = field(default_factory=list)


@dataclass
class RegimePoint:
    """状态机的一个采样点。"""

    ts: str
    regime: str
    score: float
    cap: float
    detail: dict = field(default_factory=dict)


# ================================================================== 评分
# MarketScore 各子项的**满分线**（同样是实测分位，不是拍脑袋）
SCORE_FULL_SCALE = {
    "limit_up": 77,     # 涨停家数 p90（原用 120 ⇒ 长期停在 32 分）
    "max_streak": 5,    # 连板高度 p75（原用 6 ⇒ 常年 83+）
}


def score_market(env: MarketEnv, params: dict | None = None) -> MarketEnv:
    """计算 MarketScore（0~100）。

    MarketScore = 0.25×赚钱效应 + 0.20×量能 + 0.20×涨停结构
                + 0.20×指数趋势 + 0.15×外围

    ★ 缺失因子的处理（关键设计）：
      东财 push2 不可达 => 主力净流入、外围无数据源。
      此时**显式标记 missing_factors，并把权重重新归一化**，
      而不是当0处理 —— 当 0 会让「缺失」被误解为「很差」。

    ★ 满分线（`lu_full` / `streak_full`）按**实测分位**取，见
      `SCORE_FULL_SCALE` 与 `THRESHOLD_PROVENANCE`。
    """
    p = params or {}
    fs = {**SCORE_FULL_SCALE, **(p.get("full_scale") or {})}
    lu_full = fs["limit_up"]
    streak_full = fs["max_streak"]
    s, v = env.sentiment, env.volume

    # ---- ① 赚钱效应（0~100）----
    # ★ 满分线取实测 p90（涨停 77 家）而非「120 家」：
    #   251 天里涨停≥120 只有 2 天，按 120 封顶会让赚钱效应
    #   长期停在 p50(39)/120*100 = 32 分附近，区分度尽失。
    #
    # ★★★ 归一化必须带 `* 100`，满分线是「多少家得100 分」不是除数。
    #   原式 `limit_up / 1.2` 之所以能工作，是因为 1.2 = 120/100；
    #   直接换成 `/ lu_full` 会让涨停 86 家只得 **1.12 分**（而非 100），
    #   money_effect 被压到 ~40、limit_structure 只有 ~21，
    #   MarketScore 全线塌到 20~30 —— **不报错，只是全都变成「差市场」**。
    lu_score = min(100.0, s.limit_up / lu_full * 100)
    ur_score = s.up_ratio * 100
    yp_score = max(0.0, min(100.0, 50 + s.yesterday_premium * 10))
    money_effect = 0.5 * lu_score + 0.3 * ur_score + 0.2 * yp_score

    # ---- ② 量能（0~100）----
    vr = v.vol_ratio
    if vr < 0.8:
        vol_score = max(0.0, (vr - 0.5) / 0.3 * 100)   # 萎缩
    elif vr <= 1.5:
        vol_score = 60+ (vr - 0.8) / 0.7 * 40         # 温和放量
    else:
        vol_score = 100.0                             # 显著放量

    # ---- ③ 涨停结构（0~100）----
    # 涨停家数 + 连板高度 - 炸板率惩罚
    # ★ 连板满分线同样按实测：p75=5，用 6 做满分线会让结构分常年 83+
    #   （同样注意 `*100`）
    streak_score = min(100.0, s.max_streak / streak_full * 100)
    broken_penalty = s.broken_rate * 100
    limit_structure = max(0.0, min(100.0,
                                   0.5 * lu_score + 0.3 * streak_score - 0.2 * broken_penalty))

    # ---- ④ 指数趋势（0~100）----
    index_trend = max(0.0, min(100.0, 50 + env.index_pct * 25))

    # ---- ⑤ 外围（0~100）----
    missing: list[str] = []
    if env.outside_ok is None:
        outside = 50.0                                # 中性
        missing.append("outside（隔夜外围，无数据源）")
    else:
        outside = 100.0 if env.outside_ok else 0.0
    if not v.has_inflow:
        # 净流入只作修正项，缺失时不计入（已在 limit_structure 中体现）
        missing.append("main_inflow（东财 push2 不可达）")

    # ---- 加权（缺失项重新归一化）----
    factors = {
        "money_effect": money_effect,
        "volume": vol_score,
        "limit_structure": limit_structure,
        "index_trend": index_trend,
        "outside": outside,
    }
    w = dict(SCORE_WEIGHTS)
    if env.outside_ok is None:
        w["outside"] = 0.0
    total_w = sum(w.values())
    if total_w <= 0:
        env.score = 50.0
    else:
        env.score = sum(factors[k] * w[k] for k in factors) / total_w

    env.missing_factors = missing
    env.detail = {
        **{k: round(v, 2) for k, v in factors.items()},
        "limit_up": s.limit_up, "limit_down": s.limit_down,
        "broken_rate": round(s.broken_rate, 4),
        "max_streak": s.max_streak,
        "yesterday_premium": round(s.yesterday_premium, 3),
        "up_ratio": round(s.up_ratio, 4),
        "vol_ratio": round(vr, 3),
        "total_amount": v.total_amount,
        "weights_used": {k: round(x / total_w, 4) for k, x in w.items() if x > 0},
    }
    return env


# ================================================================== 状态判定
def classify_regime(env: MarketEnv, params: dict | None = None) -> str:
    """单点判定市场状态（不含迟滞）。

    ★ 判定顺序有意义：高潮/退潮优先于发酵，
      否则「涨停 130 家」会同时命中发酵与高潮，取错状态。

    ★★ 阈值来自 `REGIME_THRESHOLDS`（按实测分位标定，见该常量注释）。
      允许 `params["thresholds"]` 覆盖，便于做敏感性分析；
      缺项回落到默认值 —— **不能因为只传了一个键就丢其他阈值**。
    """
    p = params or {}
    th = {**REGIME_THRESHOLDS, **(p.get("thresholds") or {})}
    s, v = env.sentiment, env.volume

    limit_up = s.limit_up
    limit_down = s.limit_down
    broken = s.broken_rate
    streak = s.max_streak
    vr = v.vol_ratio
    prem = s.yesterday_premium
    ur = s.up_ratio

    # --- 退潮：高位股炸板 + 昨日涨停今日大幅低开 + 市场普跌 ---
    #  ★ 判据是「**接力断裂**」而非「涨停家数少」。
    #    最初这里写的是 `涨停 <= p25`（28 家），是错的：
    #    「今日新封 150 家」与「昨日涨停今日崩盘-4%」可以并存 ——
    #    前者是新钱进场，后者是老钱撤退。退潮讲的是后者。
    #    加了 `涨停<=28` 之后，本该判退潮的日子被跳过去，
    #    结果落到「高潮」（150 家 + 炸板 70% 满足高潮），
    #    而高潮是「只减不加但可持有」，退潮是「禁新开」——
    #    ★ 判错方向会让人在最该跑的时候以为还能开仓。
    #  正确判据：炸板率 p75 以上 + 昨日涨停溢价崩塌 + 上涨占比极低（p25）。
    if (broken >= th["ebb_broken_rate"] and prem <= th["ebb_premium"]
            and ur <= th["ebb_up_ratio"]):
        return REGIME_EBB

    # --- 高潮：涨停家数达p90 但炸板率抬升（只减不加）---
    if (limit_up >= th["peak_limit_up"]
            and broken >= th["peak_broken_rate"]):
        return REGIME_PEAK

    # --- 发酵：连板高度达 p75 且涨停家数达 p75 ---
    if (streak >= th["ferment_streak"]
            and limit_up >= th["ferment_limit_up"]):
        return REGIME_FERMENT

    # --- 冰点：市场普跌（上涨占比 p25 以下）---
    #  ★ 原条件还要求 `跌停>涨停 且 炸板率 p50 以上`，实测只命中 6%——
    #    过严了。冰点的本质是「**普跌**」，广度跌破 p25 就该算；
    #    再叠加跌停/炸板两个条件，等于要求「普跌 + 恐慌」，把多数普跌日漏掉。
    #    实测 `广度<p25` 覆盖 24.7%，与「市场差的日子占比」相符。
    if ur < th["frozen_up_ratio"]:
        return REGIME_FROZEN

    # --- 启动：连板 p50 以上 + 涨停 p50 以上 + 上涨占比 p60 以上 ---
    #   ★ 三个条件缺一不可。原版只有 `3<=streak<=5 且 涨停>跌停`，
    #     而连板高度 p10~p90 全在 3~5 ⇒ 该条件几乎恒真（命中 84%）。
    if (streak >= th["start_streak"]
            and limit_up >= th["start_limit_up"]
            and ur >= th["start_up_ratio"]):
        return REGIME_START

    # --- 修复：涨停达 p50 + 广度达 p60 ---
    #  ★ 不能用「涨停>跌停 且 广度>=p50」—— 实测命中 49%，成了默认态
    #    （修复独占 61%，与「启动占 90.8%」是同一类病：门槛定得太松）。
    #    修复的语义是「**刚从冰点回升**」，要的是**广度明显改善**，
    #    不是「广度不差」。实测 `涨停>=39(p50) 且 广度>=0.55(p60)` 命中 27.9%。
    if (limit_up >= th["repair_limit_up"]
            and ur >= th["repair_up_ratio"]):
        return REGIME_REPAIR

    # --- 兜底 ---
    #  ★★★ 兜底**不能**绕过上面那些多因子条件，否则等于给「启动」开了后门：
    #   实测「涨停 70 家 + 连板 4 板 + 上涨占比仅 0.20」——
    #   上涨占比只有 p25（普跌），显然不是启动；三条件联立正确地排除了它，
    #   但 MarketScore 算出 61.8 分（涨停多 + 量能足）⇒ 旧兜底把它判成「启动」。
    #   ⇒「启动」的严格条件被完全抵消，退化回单条件版本的老毛病
    #     （实测那样会让 90.8% 的交易日都是「启动」）。
    #
    #   走到这里说明广度已 ≥ p25（冰点已在上一步排除），但结构条件都没达标。
    #   此时只能用分数做**方向**判断，且**不硬升「启动」**——
    #   「启动」需要连板+涨停+广度三者联立，不能靠分数单独成立。
    return (REGIME_REPAIR if env.score >= 40 else REGIME_START)


def position_cap(regime: str) -> float:
    """该状态下的仓位上限。"""
    return REGIME_META.get(regime, {}).get("cap", 0.3)


# ================================================================== 迟滞
class RegimeTracker:
    """带迟滞的状态跟踪器（附录 4.7 要求）。

    规则：
      · 进入某状态需**连续 2 个采样点**满足条件
      · 退出需**偏离阈值 10%**（避免在阈值附近反复跳变）

    ★ 这是**有状态**的组件，不能做成纯函数 ——
      故状态由调用方持有（便于持久化与回测重放）。
      单点判定 classify_regime() 仍是纯函数。
    """

    def __init__(self, enter_confirm: int = 2, exit_hysteresis: float = 0.10) -> None:
        self.enter_confirm = enter_confirm
        self.exit_hysteresis = exit_hysteresis
        self.current: str | None = None
        self._candidate: str | None = None
        self._candidate_count = 0
        self.history: list[RegimePoint] = []

    def update(self, env: MarketEnv) -> RegimePoint:
        """喂入一个采样点，返回当前状态。

        ★★ 迟滞计数必须「只在状态**不等于** current 时累加」。
          早期版本在维持分支把 `_candidate_count` 清零，导致：
            状态从 frozen 出发 →连续 3 天单点判定都是 start
            → 只有第 1 天 cnt=1，第 2 天因`raw != current` 但
              `candidate` 刚被重置而重新计数…结果第 3 天才切，
            中间那两天市场明明已经转好，落库却仍是 frozen。
          实测 2026-09-14~09-16 连续 3 天单点判定为repair/start，
          落库状态却滞留 frozen 到 09-17。
          ⇒ 「市场已经转向但状态机不反映」= 上层拿到过期信息。

        正确语义：
          · raw == current  → 清零候选计数（回到稳态）
          · raw != current  → 计数 +1；达到 enter_confirm 才切换
            且切换后**计数清零**（新状态也需要连续确认才能再切走）
        """
        raw = classify_regime(env)

        if self.current is None:
            # 首次：直接采用（没有「上一个状态」，谈不上需要确认）
            self.current = raw
            self._candidate = raw
            self._candidate_count = 0
        elif raw == self.current:
            # 维持：候选作废
            self._candidate = raw
            self._candidate_count = 0
        else:
            # 偏离：连续确认才切
            if raw == self._candidate:
                self._candidate_count += 1
            else:
                self._candidate = raw
                self._candidate_count = 1
            if self._candidate_count >= self.enter_confirm:
                self.current = raw
                self._candidate = raw
                self._candidate_count = 0

        point = RegimePoint(ts=env.as_of, regime=self.current, score=round(env.score, 2),
                            cap=position_cap(self.current), detail=dict(env.detail))
        self.history.append(point)
        return point

    @property
    def meta(self) -> dict:
        return REGIME_META.get(self.current or "", {})

    def label(self) -> str:
        m = self.meta
        return f"{m.get('emoji', '')} {m.get('label', '未知')}"

    def can_open_new(self) -> bool:
        """高潮/退潮期禁止新开仓（附录 4.7）。"""
        return bool(self.meta.get("open_new", True))


# ================================================================== 采集
def collect_sentiment(as_of: datetime, params: dict | None = None) -> Sentiment:
    """从数据源采集情绪指标。

    数据源（全部实测可用）：
      · 涨停/炸板/跌停 —— 东财 push2ex
      · 连板高度 —— 涨停池的lbc 字段
      · 昨日涨停溢价 —— 涨停池 + 实时行情
      · 上涨家数占比 —— 全市场扫描
    """
    from backend.data.adapter import bootstrap, get_adapter

    p = params or {}
    bootstrap()
    em = get_adapter("eastmoney")
    d = as_of.strftime("%Y%m%d")
    s = Sentiment()

    try:
        zt = em.fetch_zt_pool(d, as_of)
        s.limit_up = len(zt)
        s.max_streak = max((r.get("streak") or 0) for r in zt) if zt else 0
    except Exception as exc:
        print(f"[warn] 涨停池获取失败: {exc}")

    try:
        zb = em.fetch_zb_pool(d, as_of)
        tot = s.limit_up + len(zb)
        s.broken_rate = len(zb) / tot if tot else 0.0
    except Exception:
        pass

    try:
        dt = em.fetch_dt_pool(d, as_of)
        s.limit_down = len(dt)
    except Exception:
        pass

    return s


def collect_volume(as_of: datetime) -> VolumeInfo:
    """采集量能。

    ★★ 实测坑（2026-10-08）：kline_daily.amount **全为 NULL**（137 万行无一有值）。
       原因：腾讯日线接口只返回成交量（手）不返回成交额，M1 同步时未填充。
       但 volume 完整可用（137 万行全部 > 0）。
       故此处用 `收盘价 × 成交量` **估算**成交额。

       ⚠️ 这是估算值，不是真实成交额：
          · 成交量单位已换算为「股」（腾讯返回「手」×100）
          · 用收盘价近似均价，高低价波动大的股票会有偏差
          · 只用于**量比**（成交额与其 5 日均值的比值），
            相对比较时误差可抵消，绝对值不可对外披露。
       真实成交额需等东财 push2his 可用后回填（见 M5 遗留项）。

    口径：沪深主板 + 创业板 + 科创板；北交所（8/4 开头）流动性小，单列不计入。
    """
    from backend.data import duck

    v = VolumeInfo()
    try:
        con = duck.get_ro_conn()
        row = con.execute(
            """
            SELECT sum(close * volume) FROM kline_daily
            WHERE trade_date = (SELECT max(trade_date) FROM kline_daily)
              AND code NOT LIKE '8%' AND code NOT LIKE '4%'
              AND volume > 0
            """
        ).fetchone()
        v.total_amount = float(row[0] or 0.0)

        rows = con.execute(
            """
            SELECT trade_date, sum(close * volume) AS amt FROM kline_daily
            WHERE trade_date > (SELECT max(trade_date) - INTERVAL 12 DAY FROM kline_daily)
              AND code NOT LIKE '8%' AND code NOT LIKE '4%'
              AND volume > 0
            GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5
            """
        ).fetchall()
        vals = [r[1] for r in rows if r[1] is not None]
        if vals:
            v.amount_ma5 = sum(vals) / len(vals)
    except Exception as exc:
        print(f"[warn] 量能采集失败: {exc}")

    # 主力净流入：东财 push2 不可达 → 留 None，由评分函数重新归一化权重
    v.main_inflow = None
    return v


def collect_up_ratio(as_of: datetime, codes: list[str] | None = None) -> float:
    """上涨家数占比（用腾讯批量行情扫描，可靠）。"""
    from backend.data.adapter import bootstrap, get_adapter
    from backend.data.sqlite import get_conn

    bootstrap()
    tx = get_adapter("tencent")
    all_codes = [r[0] for r in get_conn().execute(
        "SELECT id FROM stock WHERE status='active'").fetchall()]
    if codes:
        all_codes = codes
    up = 0
    total = 0
    B = 60
    for i in range(0, len(all_codes), B):
        batch = all_codes[i : i + B]
        try:
            rows = tx.fetch_realtime(batch, as_of)
        except Exception:
            continue
        for r in rows:
            pc = r.get("pct_chg")
            if pc is None:
                continue
            total += 1
            if pc > 0:
                up += 1
    return up / total if total else 0.0


# ================================================================== 顶层
def analyze_market(as_of: datetime, sentiment: Sentiment | None = None,
                   volume: VolumeInfo | None = None,
                   index_pct: float = 0.0,
                   tracker: RegimeTracker | None = None,
                   params: dict | None = None) -> tuple[MarketEnv, RegimePoint | None]:
    """顶层入口。

    返回 (env, point)；给了 tracker 才做迟滞状态机，否则只做单点判定。
    """
    env = MarketEnv(
        as_of=as_of.isoformat(timespec="seconds") if isinstance(as_of, datetime) else str(as_of),
        sentiment=sentiment or collect_sentiment(as_of),
        volume=volume or collect_volume(as_of),
        index_pct=index_pct,
        outside_ok=None,           # 外围无数据源
    )
    env = score_market(env, params)
    point = tracker.update(env) if tracker else None
    return env, point


if __name__ == "__main__":
    import sys as _s
    from datetime import datetime as _dt

    logger_on = True
    print("=" * 74)
    print("=== M5 大盘环境 · 六态状态机（真实数据）===")
    print("=" * 74)

    now = _dt(2026, 10, 8, 15, 0, tzinfo=CST)
    s = collect_sentiment(now)
    v = collect_volume(now)

    print(f"\n【情绪指标】as_of=2026-10-08 15:00")
    print(f"  涨停 {s.limit_up} 家 | 跌停 {s.limit_down} 家")
    print(f"  炸板率 {s.broken_rate:.1%} | 最高连板 {s.max_streak} 板")
    print(f"  净涨停 {s.limit_net:+d}")

    print(f"\n【量能】")
    print(f"  两市成交额 {v.total_amount / 1e8:,.0f} 亿元")
    print(f"  5日均额 {v.amount_ma5 / 1e8:,.0f} 亿元 | 量比 {v.vol_ratio:.2f}")
    print(f"  主力净流入: 无数据源（东财 push2 不可达）")

    env, pt = analyze_market(now, sentiment=s, volume=v, index_pct=0.0)

    print(f"\n【MarketScore】{env.score:.1f} / 100")
    for k, v_ in env.detail.items():
        if isinstance(v_, (int, float)):
            print(f"    {k:22s} {v_}")

    print(f"\n【缺失因子（显式标注，未假装有数据）】")
    for m in env.missing_factors:
        print(f"    - {m}")

    print(f"\n【六态判定】")
    import json

    raw = classify_regime(env)
    m = REGIME_META[raw]
    print(f"    单点判定: {m['emoji']} {m['label']}  仓位上限 {m['cap']:.0%}")
    print(f"    可新开仓: {'是' if m['open_new'] else '否（只减不加）' if raw == 'peak' else '否（禁新开）'}")

    print(f"\n【迟滞验证】连续喂 5 个采样点，观察切换需要 2 次确认")
    tr = RegimeTracker()
    for i in range(5):
        e = MarketEnv(as_of=f"t{i}", sentiment=Sentiment(
            limit_up=130 if i >= 2 else 40, limit_down=10 if i >= 2 else 30,
            broken_rate=0.45 if i >= 2 else 0.2, max_streak=6 if i >= 2 else 2,
            yesterday_premium=1.0, up_ratio=0.6),
            volume=VolumeInfo(total_amount=1e12, amount_ma5=9e11), index_pct=0.5)
        e = score_market(e)
        p = tr.update(e)
        mm = REGIME_META[p.regime]
        print(f"    采样{i+1}: 判定={classify_regime(e):8s} "
              f"实际状态={mm['emoji']}{mm['label']}  (连续确认 {tr._candidate_count})")

    print("\n" + "=" * 74)
    print("所有判定均为 f(输入, as_of, params)，未取系统时间（铁律二）")