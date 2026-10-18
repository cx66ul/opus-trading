# -*- coding: utf-8 -*-
"""
Hawk · 每日推荐引擎（附录 4.4 · M6）

对应提示词 4.4 的完整实现：
    · 硬性过滤（任一命中直接剔除，且**必须记录原因**）
    · 五因子 Score = 0.25×板块强度 + 0.25×缠论结构 + 0.20×个股RPS
                 + 0.15×消息面 + 0.15×情绪周期适配
    · A/B/C 三档（多因子共振 / 满足2维/ 单一维度）
    · ★ 反向审计：每只推荐必须输出 risk_note（「如果它错了，最可能错在哪」）

★★铁律二：纯函数 f(输入, as_of, params)，不取系统时间。

★ 数据可用性（实测 2026-10-08）：
    ✓ 缠论结构（M4 已完成）
    ✓ 个股 RPS（本地 5571 只日线可算）
    ✓ 消息面（M2 已完成，raw_news/news 表）
    ✓ 情绪周期适配（M5 六态状态机）
    ⚠️ 板块强度 —— 成分股不可得（M5 结论），故降级为「板块分用情绪周期近似」
       并在每只推荐的 risk_note 中显式标注该降级
    ✗ 上市日期 —— D11 已用长历史反推下界，可用于「上市<60日」判定
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
# 硬性过滤阈值（附录 4.4 原表）
FILTER_ST = True                     # ST / *ST / 退市整理
FILTER_MIN_LIST_DAYS = 60            # 上市 < 60 日
FILTER_SUSPEND = True                # 当日停牌
FILTER_FLOAT_MV_MIN = 2_000_000_000# 20 亿
FILTER_FLOAT_MV_MAX = 200_000_000_000       # 2000 亿
FILTER_MAX_5DAY_GAIN = 35.0          # 近 5 日累计涨幅 > 35%（追高）
FILTER_ONE_WORD_LIMIT = True         # 当日一字板涨停（买不进）

# 降权项（附录 4.4：命中则降权而非剔除）
DEMOTE_BLOCK_DISCOUNT = 0.08         # 大宗折价 > 8%
DEMOTE_REDUCTION = True              # 股东减持公告未消化

# 五因子权重（附录 4.4）
SCORE_WEIGHTS = {
    "board": 0.25,
    "chanlun": 0.25,
    "rps": 0.20,
    "news": 0.15,
    "emotion": 0.15,
}

TIER_A_MAX = 3# A 档上限
TIER_B_MAX = 8               # B 档上限
TIER_A_THRESHOLD = 70.0      # A 档分数线
TIER_B_THRESHOLD = 55.0


@dataclass
class StockRow:
    """推荐计算的输入行（由 data_provider 组装）。"""

    code: str
    name: str
    is_st: bool = False
    is_suspend: bool = False
    float_mv: float = 0.0
    list_date_lb: str | None = None     # 上市日期下界（D11）
    gain_5d: float = 0.0                # 近 5 日累计涨幅 %
    is_one_word_limit: bool = False     # 当日一字板
    pct_today: float = 0.0

    # 因子得分（0~100）
    s_board: float = 50.0
    s_chanlun: float = 0.0
    s_rps: float = 50.0
    s_news: float = 50.0
    s_emotion: float = 50.0
    image_strategy: dict = field(default_factory=dict)

    # 依据（可解释性，附录十二章第 4 条）
    chan_reasons: list[str] = field(default_factory=list)
    news_reasons: list[str] = field(default_factory=list)
    board_note: str = ""
    # ★ 用显式布尔标记而非字符串匹配（早期按 board_note 内容判断，
    #   因「板块强度降级」与「板块强度因子降级」差两个字而完全漏判）
    board_degraded: bool = False
    demos: list[str] = field(default_factory=list)     # 降权项
    news_direction: str = "neutral"                   # bull/bear/neutral
    news_strength: int = 0


@dataclass
class Rejection:
    """被剔除的记录 + 原因（必须可追溯）。"""

    code: str
    name: str
    reasons: list[str]


@dataclass
class Recommendation:
    """一条推荐。"""

    code: str
    name: str
    tier: str                # A | B | C
    score: float
    factors: dict[str, float]
    risk_note: str                       # ★ 反向审计（必填）
    reasons: list[str]                   # 为什么推荐
    position_hint: float = 0.0           # 建议仓位上限（结合市场状态）
    as_of: str = ""
    image_strategy: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "tier": self.tier,
            "score": round(self.score, 2),
            "factors": {k: round(v, 1) for k, v in self.factors.items()},
            "risk_note": self.risk_note,
            "reasons": self.reasons,
            "position_hint": self.position_hint,
            "as_of": self.as_of,
            "image_strategy": self.image_strategy,
        }


# ================================================================== 硬性过滤
def hard_filter(rows: list[StockRow], as_of: date,
                params: dict | None = None) -> tuple[list[StockRow], list[Rejection]]:
    """硬性过滤。任一命中直接剔除。

    返回 (保留, 剔除)。剔除原因逐条记录 —— 静默剔除会让人以为「系统没选中」，
    而非「被规则挡住」，这在实盘排查时是灾难。
    """
    p = params or {}
    min_days = p.get("min_list_days", FILTER_MIN_LIST_DAYS)
    mv_min = p.get("float_mv_min", FILTER_FLOAT_MV_MIN)
    mv_max = p.get("float_mv_max", FILTER_FLOAT_MV_MAX)
    max_gain = p.get("max_5day_gain", FILTER_MAX_5DAY_GAIN)

    keep: list[StockRow] = []
    dropped: list[Rejection] = []

    for r in rows:
        bad: list[str] = []
        if r.is_st:
            bad.append("ST/退市风险")
        if r.is_suspend:
            bad.append("当日停牌")
        # 上市天数：只用「下界」判定，无法判定时保守（附录 D11）
        if r.list_date_lb:
            try:
                days = (as_of - date.fromisoformat(r.list_date_lb[:10])).days
                if days < min_days:
                    bad.append(f"上市仅 {days} 天（<{min_days}）")
            except ValueError:
                bad.append("上市日期格式非法（按次新股处理）")
        else:
            bad.append("无上市日期数据（按次新股保守剔除）")
        # ★ 边界：附录 4.4 原文是「流通市值 < 20 亿 或 > 2000 亿」，
        #   即恰好 20 亿与恰好 2000 亿都**应当保留**。
        #   早期写成 mv_max 用了 <，导致 2000 亿整被剔除（实测发现）。
        if mv_min <= r.float_mv <= mv_max:
            pass
        else:
            bad.append(f"流通市值 {r.float_mv / 1e8:.1f} 亿超出 "
                       f"[{mv_min / 1e8:.0f}, {mv_max / 1e8:.0f}] 亿")
        if r.gain_5d > max_gain:
            bad.append(f"近 5 日涨 {r.gain_5d:.1f}%（>{max_gain}%，追高）")
        if r.is_one_word_limit:
            bad.append("当日一字板涨停（买不进）")

        if bad:
            dropped.append(Rejection(r.code, r.name, bad))
        else:
            keep.append(r)
    return keep, dropped


# ================================================================== 打分
def _image_enabled(row: StockRow, params: dict | None = None) -> bool:
    return bool(row.image_strategy.get("enabled") and
                (params or {}).get("image_strategy_enabled", True))


def score_weights(row: StockRow, params: dict | None = None) -> dict:
    base = (params or {}).get("weights") or SCORE_WEIGHTS
    total = sum(base.values())
    normalized = {k: v / total for k, v in base.items()} if total else dict(SCORE_WEIGHTS)
    if _image_enabled(row, params):
        w = row.image_strategy["weight"]
        return {**{k: v * (1 - w) for k, v in normalized.items()}, "image_strategy": w}
    return normalized


def calc_score(row: StockRow, params: dict | None = None) -> float:
    """五因子加权 Score（0~100）。"""
    weights = score_weights(row, params)
    total_w = sum(weights.values())
    s = (weights["board"] * row.s_board
         + weights["chanlun"] * row.s_chanlun
         + weights["rps"] * row.s_rps
         + weights["news"] * row.s_news
         + weights["emotion"] * row.s_emotion)
    if "image_strategy" in weights:
        s += weights["image_strategy"] * row.image_strategy["score"]
    score = s / total_w if total_w else 0.0
    # 降权项：命中则打 85 折
    if row.demos:
        score *= 0.85
    return round(max(0.0, min(100.0, score)), 2)


# ================================================================== 分档
def assign_tier(score: float, hit_dims: int, params: dict | None = None) -> str:
    """A/B/C 分档。

    附录 4.4：
      A 档（重点）= 多因子共振（板块前10% + 缠论买点 + 消息正向 + 情绪适配）
      B 档（关注）= 满足 2 个维度
      C 档（观察）= 单一维度
    实现上用「分数线 + 命中维度数」双条件，避免单纯靠分数把
    「单因子极高分」的股票误判为A 档。
    """
    p = params or {}
    a_th = p.get("tier_a_threshold", TIER_A_THRESHOLD)
    b_th = p.get("tier_b_threshold", TIER_B_THRESHOLD)
    if score >= a_th and hit_dims >= 3:
        return "A"
    if score >= b_th and hit_dims >= 2:
        return "B"
    return "C"


# ================================================================== 反向审计
def build_risk_note(row: StockRow, tier: str, params: dict | None = None) -> str:
    """★ 反向审计（附录 4.4 强制要求）。

    「如果它错了，最可能错在哪」—— 每只推荐都必须有。
    **禁止输出无风险提示的推荐。**
    """
    risks: list[str] = []

    # ① 消息面是主要不确定性来源
    if row.news_direction == "bear":
        risks.append(f"消息面偏空（利空强度 {row.news_strength}），若消息继续发酵则逻辑反转")
    elif row.news_strength >= 4:
        risks.append("消息强度已高，存在「利好出尽」风险")
    elif row.news_direction == "neutral" and row.news_strength == 0:
        risks.append("无消息面驱动，缺催化时容易横盘")

    # ② 缠论结构风险
    if row.s_chanlun < 60:
        risks.append("缠论结构分偏低，买点级别弱，可能不触发预期")
    strong_signals = [r for r in row.chan_reasons if "三买" in r or "1buy" in r]
    if not strong_signals:
        risks.append("无强级别买点（缺三买/一买），可能只是反弹")

    # ③ 板块降级风险（M5 成分股不可得）
    # ★ 改为标记驱动而非字符串匹配：
    #   早期写成 `if "板块强度降级" in row.board_note`，
    #   而实际文本是「板块强度因子降级」—— 差两个字就完全漏判，
    #   导致降级信息没进risk_note，用户以为板块因子正常。
    if row.board_degraded or not row.board_note:
        risks.append("板块强度因子降级（成分股不可得），板块共振判断可能失准")

    # ④ 市场环境风险
    if row.s_emotion < 45:
        risks.append(f"市场环境分偏低（{row.s_emotion:.0f}），系统性回撤风险高")

    # ⑤ RPS 过高风险
    if row.s_rps >= 90:
        risks.append(f"RPS 达{row.s_rps:.0f}，短期涨幅已大，追高风险")

    # ⑥ 降权项
    for d in row.demos:
        risks.append(f"降权项：{d}")

    # ⑦ 停牌/流动性
    if row.is_suspend:
        risks.append("当前停牌，无法买入")

    if _image_enabled(row, params):
        audit = row.image_strategy
        risks.extend(x["name"] for x in audit.get("matched", []) if x["points"] < 0)
        risks.extend(audit.get("warnings", []))
        risks.append("图片策略为用户优先规则，收益尚未验证；跌破支撑需重新评估，业绩题材待财报核验")

    if not risks:
        # 附录明确禁止无风险提示的推荐 —— 必须兜底写一条真实的
        risks.append("未识别到显著风险，但本次数据源缺少资金流与板块成分，"
                     "建议小仓位试仓并设紧止损")
    return "；".join(risks)


# ================================================================== 顶层
def recommend(rows: list[StockRow], as_of: datetime | date,
              market_state: str = "repair",
              position_cap: float = 0.40,
              params: dict | None = None) -> dict[str, Any]:
    """每日推荐主入口。

    as_of 由调用方注入（铁律二）。
    """
    p = params or {}
    adate = as_of.date() if isinstance(as_of, datetime) else as_of
    astr = (as_of.isoformat(timespec="seconds") if isinstance(as_of, datetime)
            else str(as_of))

    keep, dropped = hard_filter(rows, adate, p)

    active = [r for r in rows if _image_enabled(r, p)]
    image_policy = None
    if active:
        audit = active[0].image_strategy
        image_policy = {k: audit[k] for k in ("revision", "policy_hash", "weight", "available_from",
                                             "validation_status", "evaluation_mode")}
        image_policy["enabled"] = True
        image_policy["weights"] = score_weights(active[0], p)
        image_policy["eligible_rows"] = sum(bool(r.image_strategy.get("eligible")) for r in active)
        image_policy["note"] = "图片蒸馏策略优先；评分表示形态匹配，不是上涨概率。历史行情套用当前规则不构成样本外验证。"
        limits = [r.image_strategy.get("market_position_cap") for r in active]
        limits = [v for v in limits if v is not None]
        if limits:
            position_cap = min(position_cap, min(limits))

    scored: list[tuple[StockRow, float, int, str]] = []
    for r in keep:
        sc = calc_score(r, p)
        # 命中维度数：分数显著高于中性 50 的因子数
        dims = sum(1 for k in ("s_board", "s_chanlun", "s_rps", "s_news", "s_emotion")
                   if r.__dict__[k] >= 60)
        if _image_enabled(r, p) and r.image_strategy.get("eligible") and r.image_strategy["score"] >= 60:
            dims += 1  # Correlated screenshot rules count as ONE strategy dimension.
        tier = assign_tier(sc, dims, p)
        if _image_enabled(r, p) and not r.image_strategy.get("eligible"):
            tier = "C"  # Missing dominant factor cannot qualify as a high-conviction candidate.
        scored.append((r, sc, dims, tier))

    # 分档排序
    order = {"A": 0, "B": 1, "C": 2}
    scored.sort(key=lambda x: (order[x[3]], -x[1]))

    recs: list[Recommendation] = []
    cnt = {"A": 0, "B": 0, "C": 0}
    caps = {"A": TIER_A_MAX, "B": TIER_B_MAX, "C": 15}
    for r, sc, dims, tier in scored:
        if cnt[tier] >= caps[tier]:
            continue
        risk = build_risk_note(r, tier, p)
        reasons = list(r.chan_reasons) + list(r.news_reasons)
        audit = r.image_strategy if _image_enabled(r, p) else {}
        if audit:
            reasons = [f"图片策略 {audit['score']:.0f}分（权重{audit['weight']:.0%}）"] + [
                f"{x['rule_id']} {x['name']}" for x in audit.get("matched", []) if x["points"] > 0
            ] + reasons
        if r.board_note:
            reasons.append(r.board_note)
        if not reasons:
            reasons.append(f"综合得分 {sc:.1f}，由五因子加权得出")
        recs.append(Recommendation(
            code=r.code, name=r.name, tier=tier, score=sc,
            factors={"board": r.s_board, "chanlun": r.s_chanlun,
                     "rps": r.s_rps, "news": r.s_news,
                     "emotion": r.s_emotion,
                     **({"image_strategy": audit["score"]} if audit else {})},
            risk_note=risk, reasons=reasons,
            position_hint=round(position_cap / max(1, cnt[tier] + 1), 3),
            as_of=astr,
            image_strategy={**audit, "weighted_contribution": round(audit["score"] * audit["weight"], 2),
                            "demotion_multiplier": .85 if r.demos else 1} if audit else {},
        ))
        cnt[tier] += 1

    # ★ 强制校验：每条推荐都必须有 risk_note
    for r in recs:
        assert r.risk_note and len(r.risk_note) >= 10, \
            f"{r.code} 缺少风险提示 —— 附录 4.4 禁止无风险提示的推荐"

    return {
        "as_of": astr,
        "market_state": market_state,
        "position_cap": position_cap,
        "total_input": len(rows),
        "after_filter": len(keep),
        "rejected": len(dropped),
        "recommendations": [r.to_dict() for r in recs],
        "rejections": [{"code": d.code, "name": d.name, "reasons": d.reasons}
                       for d in dropped[:50]],
        "stats": {"A": cnt["A"], "B": cnt["B"], "C": cnt["C"]},
        "image_policy": image_policy,
    }


# ================================================================== 持仓健康度
HEALTH_WEIGHTS = {
    "structure": 0.30,
    "board": 0.25,
    "news": 0.20,
    "volume": 0.15,
    "market": 0.10,
}


@dataclass
class HealthRow:
    """持仓健康度输入。"""

    code: str
    name: str
    s_structure: float = 50.0      # 缠论结构完好度
    s_board: float = 50.0
    s_news: float = 50.0
    s_volume: float = 50.0        # 量价配合度
    notes: list[str] = field(default_factory=list)


def calc_health(row: HealthRow, market_score: float,
                params: dict | None = None) -> dict[str, Any]:
    """持仓健康度（附录 4.3）。

    Health = 0.30×结构 + 0.25×板块 + 0.20×消息 + 0.15×量能 + 0.10×大盘

    ★ 缺失板块分时（M5 成分股不可得）应给中性 50 而非 0，
      否则所有持仓的健康度都会被系统性压低 12.5 分。
    """
    w = HEALTH_WEIGHTS
    health = (w["structure"] * row.s_structure
              + w["board"] * row.s_board
              + w["news"] * row.s_news
              + w["volume"] * row.s_volume
              + w["market"] * market_score)
    health = round(max(0.0, min(100.0, health)), 1)

    action = "持有"
    if health < 40:
        action = "减仓/离场"
    elif health < 60:
        action = "谨慎持有"
    elif health >= 80:
        action = "可加仓"

    return {
        "code": row.code, "name": row.name, "health": health,
        "level": ("red" if health < 40 else "yellow" if health < 60 else "green"),
        "action": action,
        "factors": {"structure": row.s_structure, "board": row.s_board,
                    "news": row.s_news, "volume": row.s_volume,
                    "market": round(market_score, 1)},
        "notes": row.notes,
    }


if __name__ == "__main__":
    from datetime import date as _d

    print("=" * 74)
    print("=== M6 推荐引擎 · 构造数据验证 ===")
    print("=" * 74)

    today = _d(2026, 10, 8)

    def mk(code, name, **kw) -> StockRow:
        base = dict(s_chanlun=80.0, s_rps=70.0, s_news=75.0,
                    s_board=70.0, s_emotion=65.0)
        base.update(kw)
        return StockRow(code=code, name=name, **base)

    rows = [
        # 各项达标 + 多因子共振 -> A 档
        mk("600519", "贵州茅台", float_mv=1.57e12, list_date_lb="2001-08-27",
           gain_5d=3.2, chan_reasons=["三买成立", "回抽未破中枢上沿 1258"],
           news_reasons=["消息面：获批重组（利好 strength 4）"],
           news_direction="bull", news_strength=4),
        # 达标但因子弱 -> C 档
        mk("000001", "平安银行", float_mv=2.29e11, list_date_lb="1991-04-03",
           gain_5d=1.1, s_chanlun=45.0, s_rps=40.0,
           chan_reasons=["无明确买点"], news_reasons=["无消息驱动"]),
        # ST -> 剔除
        mk("000016", "*ST康佳A", float_mv=3.9e9, list_date_lb="1980-01-01",
           is_st=True, gain_5d=5.0),
        # 上市不足 -> 剔除
        mk("301999", "新股测试", float_mv=5e9, list_date_lb="2026-09-20", gain_5d=10.0),
        # 市值过小 -> 剔除
        mk("300001", "小市值", float_mv=8e8, list_date_lb="2000-01-01", gain_5d=5.0),
        # 市值过大 -> 剔除
        mk("601398", "超大市值", float_mv=3.5e12, list_date_lb="2006-01-01", gain_5d=2.0),
        # 追高 -> 剔除
        mk("300750", "宁德时代", float_mv=8e11, list_date_lb="2018-06-11", gain_5d=42.0),
        # 一字板 -> 剔除
        mk("002594", "比亚迪", float_mv=2e11, list_date_lb="2011-06-30",
           is_one_word_limit=True, gain_5d=8.0),
        # 降权项
        mk("600050", "中国联通", float_mv=1e11, list_date_lb="2002-10-09",
           gain_5d=4.0, demos=["股东减持公告未消化"]),
    ]

    res = recommend(rows, today, market_state="repair", position_cap=0.40)
    print(f"\n【过滤统计】输入 {res['total_input']} → 通过 {res['after_filter']}"
          f" → 剔除 {res['rejected']}")
    print(f"【分档】{res['stats']}")

    print(f"\n【剔除明细（必须可追溯）】")
    for r in res["rejections"]:
        print(f"  {r['code']} {r['name']:10s} -> {'；'.join(r['reasons'])}")

    print(f"\n【推荐清单】")
    for rec in res["recommendations"]:
        print(f"\n  [{rec['tier']}档] {rec['code']} {rec['name']} 得分 {rec['score']}")
        print(f"     因子: " + " ".join(f"{k}={v:.0f}" for k, v in rec["factors"].items()))
        print(f"     依据: {'；'.join(rec['reasons'][:2])}")
        print(f"     ★风险: {rec['risk_note'][:100]}")

    print(f"\n【持仓健康度】市场分 47.5（修复期）")
    for hr in [
        HealthRow("600519", "贵州茅台", 85, 70, 80, 75),
        HealthRow("000001", "平安银行", 35, 50, 40, 45),
    ]:
        r = calc_health(hr, 47.5)
        print(f"  {r['code']} {r['name']:8s} Health={r['health']:>5} "
              f"{r['level']:6s} -> {r['action']}")

    print("\n" + "=" * 74)
    print("每条推荐都带 risk_note（已断言校验），符合附录 4.4 强制要求")
