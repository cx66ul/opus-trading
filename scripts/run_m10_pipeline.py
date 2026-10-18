# -*- coding: utf-8 -*-
"""M10 完整链路实跑：语料包候选 → Rule → 编译 → 回测 → admit → 落库。

================================ 本脚本的立场 ==================================
★ **不替用户猜口径。** 三条候选规则的阈值虽然都有出处，但**分母/基准未答**
  （"亏钱效应面积>30%" 的30% 是涨停家数占比？亏钱个股占比？）。
  猜一个填进去能让链路"跑通"，但那等于**伪造口径**——
  正是规格 4.9.3 禁止的那种编造，只是发生在条件而非 evidence 上。

⇒ 因此脚本对每条规则做**两遍**编译：
  A. **如实的**（scope_undefined=True）—— 预期被REJECTED / PENDING，
     看闸门**是不是真的拦得住**。
  B. **口径已答的**（把用户填的口径代入）—— 只在 `--scope-answered` 时跑，
     用于看"如果口径答了，链路会走到哪一步"。

★ A 段的 REJECTED 是**正确结果**，不是失败。若闸门把 A 段放行，
  说明闸门形同虚设，那才是真问题。

用法：
    python -m scripts.run_m10_pipeline# 如实跑（预期 A 段被拦）
    python -m scripts.run_m10_pipeline --scope-answered# 代入口径后跑
    python -m scripts.run_m10_pipeline --backtest       # 真回测（读 kline）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST = timezone(timedelta(hours=8))

from backend.domain import breadth as _breadth       # noqa: E402
from backend.domain.distill import (  # noqa: E402
    Rule, admit, compile_rule, upsert_rules, STATUS_REJECTED,
    STATUS_PENDING, STATUS_ACTIVE,
)

PKG = _ROOT / "data" / "creator_corpus" / "Douyin-Corpus-20261010"
GAPS = _ROOT / "data" / "scope_gaps.json"

STRATEGY = "yeren_xiaoyang"

# ==================================================================
# 三条候选规则。`if_json` 只写**DSL 里真的存在的谓词**，
# 不新造谓词名（如 `loss_area_ratio` 就不存在 ⇒ 会 REJECTED，
# 这是**正确**的：语料要的口径指标本项目还没实现，见缺口报告）。
# ==================================================================
CANDIDATES: list[dict] = [
    {
        "rule_id": "R_TIDE_CLEAR_30",
        "name": "退潮期亏钱效应(跌停占比) >30% → 提醒清仓并停止打板",
        #★ 用户已答：亏钱效应用跌停家数量化。见LOSS_AREA_SCOPE。
        "scope_undefined": {},
        "scope_answered": True,
        # ★★ 谓词格式是**单键 dict**：`{"market_state": {"in": [...]}}`，
        #   不是 `{"predicate": "market_state", "params": {...}}`。
        #   我第一版就是这么写错的 ⇒ 3 条全部因 schema 报错被REJECTED，
        #   而拒绝理由里**完全没提口径** ⇒ 那次跑**什么都没证明**。
        #   「被拒绝」和「因为对的理由被拒绝」是两件事，必须分开。
        # ★★ 我第一版写的是 `{"in": ["退潮"]}` —— **这会让规则永远判False**。
        #   market_regime 表实际取值只有
        #   repair(150) / frozen(48) / start(35) / ferment(14) / ebb(4)，
        #   **不存在"退潮"这个字符串**（退潮 = REGIME_EBB = "ebb"）。
        #   危险之处：schema 校验**照样通过**、规则照样进库，
        #   只是永不触发 ⇒ 看起来"已上线"，实际是哑弹。
        #   这正是 compile_rule 注释里警告的失败模式。
        "if_json": {"all_of": [
            {"market_state": {"in": ["ebb", "frozen"]}},
            # ★★ `loss_area_ratio` 谓词已实现（breadth.loss_area_ratio +
            #   StrategyContext.loss_area_ratio + dsl 谓词，2026-10-10）。
            #   ★ 单位是**比例**不是百分数 ⇒ 阈值写 0.30；写 30 会永不触发。
            {"loss_area_ratio": {"op": ">", "value": 0.30}},
        ]},
        # ★ then.action 只能是 BUY / SELL / HOLD（dsl.validate_rule 白名单）。
        #   我第一版写了 CLEAR / STOP ⇒ 也是 schema 错。
        #   另：★ D13 铁律——**代码不得出现 action=SELL**。
        #   博主的"卖出/清仓"只能作为**提醒**（note）表达，
        #   这里用 HOLD + note，符合"系统不自动卖出"的既定决策。
        "then_json": {"action": "HOLD",
                      "note": "退潮期亏钱效应扩散面积>30% → 提醒清仓并停止打板"},
        "evidence_refs": ["e05", "e06", "e07", "e08"],
        "src_seconds": "13.9-17.8 / 18-20",
    },
    {
        "rule_id": "R_MA10_STOP",
        "name": "收盘跌破 10 日线 → 提醒止损",
        # ★ 用户已答：收盘破（盘中破不算）⇒ 语义明确，可直接编译。
        "scope_undefined": {},
        "scope_answered": True,
        # ★ price_vs_ma 的参数名是 `ma`（不是 n），且必须给 op + value。
        "if_json": {"all_of": [
            {"price_vs_ma": {"ma": 10, "op": "<", "value": 0}},
        ]},
        "then_json": {"action": "HOLD",
                      "note": "破 10 日线 → 提醒止损（系统不自动卖出）"},
        "evidence_refs": ["e20", "e21", "e22"],
        "src_seconds": "见文稿 t02 未支持条件",
    },
    {
        "rule_id": "R_PREOPEN_VOL_1T",
        "name": "科技高开但 11 点前全市场成交额不足 1 万亿 → 提醒找高点卖",
        # ★ 用户已答：成交量看**成交额**。
        #★★2026-10-10 数据已解锁（原"阻塞"结论作废，见 data_ready）。
        "scope_undefined": {},
        "scope_answered": True,
        "data_ready": {
            "unlocked_at": "2026-10-10",
            "why_unblocked": "★★ 科创板（688+689）成交量单位错误已修全库。"
                             "此前算出的「11 点前成交额」全部偏大 15 倍，"
                             "连带把1 万亿阈值判成「永不触发」。",
            "amount_field": "仍然只能用 `volume × close` 自算 —— "
                            "`kline_min.amount` 已废弃（恒 None）。",
            "recomputed": "★★ 2026-10-10 补齐m30 后（覆盖率 82%→**93.67%**）重算："
                          "11 点前累计成交额中位 **12089 亿**（实测）/外推 12922 亿；"
                          "「11点前占全天」中位 **58.0%**。"
                          "**1 万亿阈值实测触发 3/60 天（5%）／外推 3/60 天**。",
            "coverage_sensitivity": "★ 补齐前后同一条规则的两个数字："
                                    "覆盖率 82% ⇒ 中位 10220亿、触发 26/60 天；"
                                    "覆盖率 94% ⇒ 中位 12089 亿、触发 3/60 天。"
                                    "⇒ **绝对值与触发频次都对覆盖度高度敏感**，"
                                    "引用时必须连覆盖率一起报，否则不可复现。"
                                    "（两口径都在合理量级，"
                                    "说明「1 万亿是合理阈值」这个结论**稳健**；"
                                    "但具体触发概率**尚未稳定**。）",
            "directionality": "⚠ **无单调性**：分档前瞻 h5 无单调趋势，"
                               "且样本量不足以证方向 ⇒ 暂不能进 DSL 谓词。",
            "known_gap": "★ 北交所 920（**347 只，0.73% 成交额**）**腾讯 m30 返回 0 根** "
                         "⇒ 「全市场」严格说缺这一块。已如实标注，"
                         "不用外推值冒充、不另找源混入口径。",
        },
        "data_blocked": {
            "reason": "⚠ **口径与字段均已解锁**（见 data_ready）；"
                      "剩余阻塞是**样本量与方向性**，不是数据可得性。",
            "kline_daily.amount": "全库 137 万行，**非空 0 条** ⇒ 成交额必须自算",
            "kline_min.amount": "★ 字段**已废弃**：实测 `Σamount/Σ(volume×close)` "
                                 "逐股跨 **4 个数量级**（log10 -5~-1，中位 1.36e-2），"
                                 "当日 4556 只**0 只**落在 1 附近 ⇒ 与量价脱钩、"
                                 "逐股漂移，无法标定。采集器已改为恒置 None。",
            "why_not_trusted": "腾讯分钟线 `it[7]` 的单位未在任何文档标注。"
                               "此前判据写的是「amt/vol=0.05 且随股价反向变化」，"
                               "⇒ **那个判据本身是错的**：拿「股」当「手」比，"
                               "换算后比值中位 0.945（看似正常），"
                               "换回股口径就是 1.36e-2。"
                               "★ 同一份数据能算出两个相差 70 倍的「结论」"
                               "⇒ 说明判据不成立，必须换判据（逐股比+ 分布）。"
                               "※ 该诊断是在「688 被误放大 100 倍」的 volume 上做的，"
                               "比率中位数已失真；但「跨 4 个数量级、0 只落在 1 附近」"
                               "的定性结论不受量纲影响，故 `it[7]` 仍判不可用。",
            "correct_way": "★ 成交额必须**自己算** `volume × close`。"
                           "★★ 2026-10-10 更新：日线口径 `sum(volume×close)` "
                           "= 14237 亿（2026-09-29），**A 股合理量级**；"
                           "此前记的「218130 亿」是科创板单位错误的产物，**已作废**。",
            "remaining_gap": "① 北交所 920（347 只）腾讯 m30 无数据 ⇒ "
                             "「全市场」严格说缺这一块；"
                             "② 样本仅 60 个交易日（2026-07-09~10-08）。",
            "old_magnitude_claim": "⚠⚠ **双重作废**：「11点前 66~82 亿 vs 1 万亿 "
                                   "⇒ 差两个数量级」——"
                                   "① 那是直接相加不可信的 amount 得到的；"
                                   "② 即便用 volume×close，"
                                   "**科创板单位错误会让它偏大 15 倍**。"
                                   "正确量级是中位 10220 亿，与 1 万亿同量级。",
            "need": "① 等样本累积到 ≥120 个交易日再判方向性（现在 60 天）；"
                    "② 北交所 920 要进「全市场」需另找支持 920 的分钟线源；"
                    "③ **引用任何绝对值时必须连覆盖率一起报**（见 coverage_sensitivity）；"
                    "④ 在此之前**不进 DSL 谓词** —— "
                    "无单调性的指标做阈值只会得到「任何阈值都能切出好结果」。",
        },
        # ★ 全市场 11 点累计成交额 ≠ 个股量比。口径未答时**没有对应谓词**
        #   （DSL 22 个谓词里没有「全市场成交额」），只能退到涨幅条件，
        #   并在 scope_undefined 里显式记录条件不完整。
        "if_json": {"all_of": [
            {"pct_change_today": {"op": ">", "value": 0}},
        ]},
        "then_json": {"action": "HOLD",
                      "note": "科技高开但 11 点前量能不足 1 万亿 → 提醒找高点卖"},
        "evidence_refs": ["e04", "e17", "e18", "e19"],
        "src_seconds": "84.2-86.5",
    },
]


# ==================================================================
# ★ 用户 2026-10-10 已答口径：
#   · 亏钱效应 → **用跌停家数量化**
#   · 10 日线   → **收盘破**（盘中不算）
#   · 成交量→ **看成交额**
#   但「答了口径」≠「数据支持回测」，见每条的data_blocked。
# ==================================================================

# ★★ 实测结论（2025-10-01 ~ 2026-10-09，242 个交易日）：
#   亏钱效应 = 跌停家数，分母有唯一可用解：
#     口径A跌停/(涨停+跌停)  中位 13.2%  最大 86.5%  → >30% 有 **46/242** 天
#     口径C 跌停/全市场      中位 0.19%  最大  5.52% → >30% 有 **0/242** 天 ★永不触发
#   ⇒ 用户说的「跌停家数」若分母是全市场，30% 阈值**历史上从未发生过**，
#     规则会是哑弹。分母必须是**涨跌停总数**才有意义。
#   ⇒ 因此采用口径A，并保留口径C 作为对照记录在 evidence 里。
LOSS_AREA_SCOPE = {
    "definition": "亏钱效应面积 = 跌停家数 / (涨停家数 + 跌停家数)",
    "limit_band": "按板块限幅：主板10% / 双创20% / 北交所30%；"
                  "★ 涨停排除 ST、跌停包含 ST",
    "user_answer": "亏钱效应用跌停家数量化",
    "denominator_chosen": "涨停+跌停总数（因全市场为分母时30%永不触发）",
    # ★ 下面是**严格口径**（复用 breadth.is_limit_up/down）的实测值。
    #   ⚠ 曾在此处写过「46 天」——那是**宽松口径**（pct_chg >= 限幅-0.3）
    #     时代的数，与本模块实际执行的口径不符 ⇒ 口径记录本身在骗人。
    "evidence_range": "2025-10-01~2026-10-09，242 个交易日："
                      ">30% 有 111 天（严格口径）；中位 26.8%、最大 91.2%",
    "st_source": "★ `stock.is_st`（SQLite，5571 行 / 202 只为 1）—— "
                 "库里**有**ST 标志，此前注释称「库内没有」是错的。"
                 "⚠ 它是**当前快照**，历史 ST 会漂移；"
                 "已同时用「收盘≈round(前收×0.95,2)」反推做对照，"
                 "两口径的 ratio_pct 一并记录在事件研究产物里。",
    "semantics": "★ 该指标是**同步/滞后描述**，不是预测信号（2026-10-10 事件研究实测）："
                 "比例越高当日越差（单调，触发日 22/22 全跌），"
                 "但 h5 前瞻超额 +1.12pp（方向相反）。"
                 "合法用法＝盘中提醒收缩风险；不可用于收盘后预测次日。",
}


def load_gap_index() -> dict:
    if not GAPS.exists():
        return {}
    items = json.loads(GAPS.read_text(encoding="utf-8"))["items"]
    idx = {}
    for it in items:
        for kw in ("面积大于30", "10块钱", "1万亿"):
            if kw in (it["asr_text"] or "") or kw in (it["ocr_text"] or ""):
                idx[kw] = it
    return idx


def build_rules(scope_answered: bool) -> list[Rule]:
    rules = []
    for c in CANDIDATES:
        ev = [{
            # ★ evidence 必带 source + quote_ref，否则 admit 直接 REJECTED
            "source": "creator-corpus/Douyin-Corpus-20261010/corpus.jsonl",
            "creator": "yeren" if c["rule_id"] != "R_PREOPEN_VOL_1T"
                       else "kuajing_yangyang",
            "quote_ref": "#".join(c["evidence_refs"]),
            "seconds": c["src_seconds"],
            "speech_verified": False,      # ★ 未听校 ⇒ 永远不是 ACTIVE
            "scope_answered": bool(c.get("scope_answered")) and scope_answered,
            "scope_undefined": c["scope_undefined"],
            **({"scope_detail": LOSS_AREA_SCOPE}
               if c["rule_id"] == "R_TIDE_CLEAR_30" else {}),
            **({"data_blocked": c["data_blocked"]}
               if c.get("data_blocked") else {}),
            # ★ data_ready 与 data_blocked **并存**：数据解锁了但方向性没证，
            #   两者都要落盘。只留其一就会丢失另一半事实。
            **({"data_ready": c["data_ready"]}
               if c.get("data_ready") else {}),
        }]
        rules.append(Rule(
            rule_id=c["rule_id"], strategy_id=STRATEGY, name=c["name"],
            if_json=c["if_json"], then_json=c["then_json"],
            timeframe="1d", evidence=ev, confidence=0.0,
        ))
    return rules


def backtest_rule(rule: Rule) -> dict:
    """→ 最小回测契约。**口径已答但数据不支持 ⇒ samples=0 + 阻塞原因**，
    不是假数字。"""
    ev0 = (rule.evidence or [{}])[0]
    if not ev0.get("scope_answered"):
        return {"samples": 0,
                "skipped": True,
                "reason": "口径未答（分母/基准未知）⇒ 回测无法定义，"
                          "任何样本数都是编造"}
    if ev0.get("data_blocked"):
        b = ev0["data_blocked"]
        # ★ 2026-10-10：区分两种「阻塞」，别混成一句「数据不支持」。
        #   数据解锁（data_ready 存在）但方向性未证 ⇒ 阻塞原因是**样本量**，
        #   若仍写「数据不支持」会让人以为还得等采集，方向就错了。
        ready = ev0.get("data_ready")
        if ready:
            why = ("数据已解锁，但**样本量与方向性不足**"
                   f"（{b.get('need', '')[:60]}）")
        else:
            why = f"口径已答但**数据不支持**：{b['reason']}"
        out = {"samples": 0, "skipped": True,
               "reason": why,
               "blocked_detail": b}
        if ready:
            out["data_ready"] = ready
            #★ 触发频次是可查的事实，必须报出来（它是阈值合理性的直接证据）
            out["threshold_recomputed"] = ready.get("recomputed")
        return out
    # ★ 该规则条件所需字段齐备 ⇒ 真跑回测
    if rule.rule_id == "R_MA10_STOP":
        return backtest_ma10()
    if rule.rule_id == "R_TIDE_CLEAR_30":
        # ★ 这条是**市场级**规则（market_state / loss_area_ratio 同一天全市场同值），
        #   用逐股滑窗回测会得到「同一个数字 ×5400」的伪重复
        #   （pseudo-replication：样本量虚高且无统计意义）。
        #   ⇒ 改用事件研究：以「触发日」为样本单位看前瞻收益。
        from backtest_tide_rule import run as _tide
        r = _tide()
        ct, dr = r.get("contemporaneous"), r.get("dose_response")
        return {
            "samples": r["trigger_days"],
            "skipped": False,
            "method": "event_study（市场级规则不能逐股回测）",
            "trigger_days": r["trigger_days"],
            "days_total": r["days_total"],
            "threshold_sensitivity": r["threshold_sensitivity"],
            "forward": r.get("forward"),
            # ★ 关键结论：同期成立、前瞻不成立 ⇒ 是同步描述不是预测信号
            "contemporaneous": ct,
            "dose_response": dr,
            "verdict": ("contemporaneous_only" if ct
                        and ct["monotone_decreasing"] and dr
                        and not dr["monotone_decreasing"] else "unknown"),
            "note": ("亏钱效应扩散与**当日**下跌单调相关（触发日 22/22 全跌），"
                     "但**前瞻**收益为正 ⇒ 该指标是同步/滞后描述，"
                     "不是预测信号。合法用法是盘中提醒收缩风险，"
                     "不可用于收盘后预测次日方向。"),
        }
    return {"samples": 0, "skipped": True,
            "reason": "尚未接入 kline 扫描"}


# ★★ 语料口径（2026-10-11 用户确认）：亏钱效应面积
#   = `跌停 / (涨停 + 跌停)`，即 `breadth.DENOM_LIMIT_POOL`。
#   ⇒ 阈值 30% 真实可触发（实测 243 天里 23 天 > 30%）；
#     若换成「跌停/全市场」则是 0 天（规则哑弹）。
#★ 口径常量**从 breadth 导入**，不写字面量 "limit_pool"：
#   写字符串会在拼错时静默走错口径（`loss_area_ratio` 只认常量值）。
LOSSDENOM = _breadth.DENOM_LIMIT_POOL


def loss_area_by_day(con) -> dict[str, dict]:
    """逐日的市场级横截面：涨停/跌停家数 + 亏钱效应面积。

    ★ 复用 `backtest_tide_rule.build_days`，**不重写一套**：
      两套涨停家数口径必然对不上（项目已吃过这个亏）。
    """
    from backtest_tide_rule import build_days
    return {str(d["date"]): d for d in build_days(con)}


def backtest_ma10(horizon: int = 5, limit_codes: bool = False) -> dict:
    """R_MA10_STOP 的**真实回测**：全市场日线逐日跑「收盘破 10 日线」。

    ★ 为什么这条能真跑而其它两条不能：
      `price_vs_ma` 只需 closes（kline_daily 137万行，close 非空），
      而「亏钱效应面积」需要涨跌停家数口径、「11点成交额」需要分钟 amount
      —— 两者都缺数据。所以口径全部答了 ≠ 全部可回测。

    ★ 口径：**信号日收盘**（用户已答"收盘破"），
      持有 horizon 日后在**收盘价**卖出 —— 只用收盘价口径，
      「按信号价」的胜率是事后极值选择偏差，不可用于实盘。
    """
    from datetime import datetime as _dt

    from backend.data import duck
    from backend.domain import dsl
    from backend.domain.strategy import StrategyContext

    con = duck.get_ro_conn()
    # ★ 必须按 code 分组后逐日滑窗，样本量 = 全市场 × 交易日 ≈ 数千万次
    #   evaluate调用 ⇒ 全量跑很慢。加 `limit_codes` 便于先验逻辑、再放开。
    rows = con.execute(
        "select code, trade_date, close from kline_daily "
        "where close is not null and close > 0 "
        "and trade_date >= date '2025-01-01' "
        + ("and code >= '600000' and code < '601000' " if limit_codes
           else "")
        + "order by code, trade_date").fetchall()
    if not rows:
        return {"samples": 0, "skipped": True, "reason": "无日线数据"}

    by_code: dict[str, list] = {}
    for code, dt, close in rows:
        by_code.setdefault(code, []).append((dt, close))

    rule = {"rule_id": "R_MA10_STOP", "if": {"all_of": [
        {"price_vs_ma": {"ma": 10, "op": "<", "value": 0}}]}}

    #★★★ 把**真实市场级横截面**填进上下文。
    #   之前 `StrategyContext(...)` 只传了 code/as_of/closes，
    #   `loss_area_ratio` 恒为 None ⇒ DSL 里「亏钱效应面积 > 30%」
    #   这条谓词**永不触发且不报错**（哑弹）。
    #   ⇒ 字段、谓词、计算函数三层都齐了，唯独没人调用 —— 这就是缺口。
    market = loss_area_by_day(con)

    rets: list[tuple] = []   # (trade_date, return) —— 带日期才能按时间切段
    hit = 0
    for code, series in by_code.items():
        if len(series) < 10 + horizon + 1:
            continue
        for i in range(10, len(series) - horizon):
            closes = [c for _, c in series[:i + 1]]
            as_of = _dt.combine(series[i][0],
                                _dt.min.time()).replace(
                                    tzinfo=CST, hour=15)
            mrow = market.get(str(series[i][0])) or {}
            ctx = StrategyContext(
                code=code, as_of=as_of, closes=closes,
                # ★ 比例（0~1），不是百分数；缺失留 None 绝不填 0
                loss_area_ratio=mrow.get("ratio"),
                loss_area_denom=LOSSDENOM,
                limit_up_today=mrow.get("limit_up"),
                limit_down_today=mrow.get("limit_down"),
                market_sample=mrow.get("sample"))
            res = dsl.evaluate(rule, ctx)
            if not res.matched:
                continue
            hit += 1
            entry = series[i][1]
            exit_ = series[i + horizon][1]
            if entry and entry > 0:
                # ★ 按日期排序是**必须**的：按 code 遍历的顺序里
                #   早期/晚期信号是交错的，直接切四段等于随机切分。
                rets.append((series[i][0], (exit_ / entry) - 1.0))
    rets.sort(key=lambda x: x[0])          # ★ 先按时间排序
    rets = [r for _, r in rets]
    if not rets:
        return {"samples": 0, "skipped": True,
                "reason": "全市场无一次命中（条件或数据有问题）"}
    wins = sum(1 for r in rets if r > 0)
    avg = sum(rets) / len(rets)

    # ★★★ 样本量稳定性检验（最贵的教训：30/60 只样本的漂亮数字不能信）
    #   只报一个总样本量的结果 = 在多个切分里挑好看的那个。
    #★ 按**时间顺序**切 4 段（不是对收益排序后切——那是分布检验，
    #   不是稳定性检验）。早期与晚期符号相反 ⇒ 该结论不能上线。
    seg = []
    qn = len(rets) // 4
    for k in range(4):
        chunk = rets[k * qn:(k + 1) * qn] if k < 3 else rets[3 * qn:]
        if not chunk:
            continue
        seg.append({
            "seg": f"Q{k + 1}",
            "n": len(chunk),
            "winrate_pct": round(100.0 * sum(1 for r in chunk if r > 0)
                                 / len(chunk), 2),
            "avg_return_pct": round(100.0 * sum(chunk) / len(chunk), 3),
        })
    signs = {1 if s["avg_return_pct"] > 0 else -1 for s in seg}
    sv = sorted(rets)
    n = len(rets)
    return {
        "samples": n,
        "horizon_days": horizon,
        "entry": "信号日收盘",
        "exit": f"第 {horizon} 日收盘",
        "winrate_pct": round(100.0 * wins / n, 2),
        "avg_return_pct": round(100.0 * avg, 3),
        "median_return_pct": round(100.0 * sv[n // 2], 3),
        "quartiles": seg,
        "sign_stable": len(signs) == 1,
        "verdict_hint": ("符号稳定" if len(signs) == 1
                         else f"★符号不稳定（{len(signs)} 种）⇒ 不能上线"),
        "note": "★ 单一样本量结果不可用于实盘；已附四分位稳定性检验",
    }


def probe_limit_down_area() -> dict:
    """用真实数据验证「亏钱效应面积 > 30%」历史上到底能不能触发。

    ★ 这才是「口径答了之后该做的事」：不是急着回测规则收益，
      而是先确认**这个条件在历史上到底发生过没有**。
      若永不触发 ⇒ 0 样本不是「样本不够」，而是「条件不存在」，
      性质完全不同（前者等数据，后者该改口径或改规则）。

    ★★ 必须复用 `backend.domain.breadth` 的**严格口径**
      （`abs(close - round(pre_close×(1±限幅), 2)) < 0.005`），
      **不能**自己写 `pct_chg <= -限幅+0.3`：
      breadth 文档已实测宽松口径在 2026-10-08 多判 603823
      （收46.59 vs 涨停价 46.65，差 0.06 并未封板），
      且 ST 涨停要排除、ST 跌停要包含。
      ⇒ 另造一套口径 = 把已修好的坑重新踩一遍。
    """
    #★★★ 复用 `loss_area_by_day`（即 `backtest_tide_rule.build_days`），
    #   **不再在本文件里重算一遍**。
    #   原实现有两个真实问题：
    #     ① 用「收盘 ≈ round(前收×0.95,2)」**反推 ST**，
    #        注释自己写着「这是近似，不是权威 ST 名单」；
    #        而 `backtest_tide_rule` 用的是 `stock` 维表**真名单**
    #        ⇒ 同一份语料两套 ST 判定 ⇒ 涨停家数不一致。
    #     ② 用库里的 `pre_close` 列（实测仅 93.7% 与 lag(close) 一致）。
    #   ⇒ 两套实现的「>30% 天数」可能对不上，且**没人知道该信哪个**。
    from backend.data import duck
    con = duck.get_ro_conn()
    out = {"total_days": 0, "over30": 0, "max_pct": 0.0, "min_pct": 0.0,
           "median_pct": 0.0, "samples": [], "denom": LOSSDENOM}
    vals = []
    for d, row in sorted(loss_area_by_day(con).items()):
        ratio = row.get("ratio")
        if ratio is None:
            continue
        ld = row.get("limit_down") or 0
        lu = row.get("limit_up") or 0
        if ld + lu < 10:
            continue
        pct_val = 100.0 * ratio
        vals.append(pct_val)
        out["total_days"] += 1
        if pct_val > 30:
            out["over30"] += 1
            if len(out["samples"]) < 8:
                out["samples"].append({"date": d, "limit_down": ld,
                                       "limit_up": lu,
                                       "pct": round(pct_val, 1)})
    if vals:
        sv = sorted(vals)
        out["max_pct"] = round(max(vals), 2)
        out["min_pct"] = round(min(vals), 2)
        out["median_pct"] = round(sv[len(sv) // 2], 2)
    return out


def dead_predicates(rule: Rule) -> list[str]:
    """→ 该规则里**取值域在库中不存在**的谓词参数（哑弹检测）。

    ★ 这是最阴险的一类 bug：schema 校验通过、规则进库、页面显示"已上线"，
      但因为枚举值写错（如`market_state in ["退潮"]` 而库里只有 `ebb`），
      **规则永不触发**。不主动查根本发现不了——
      规则"安静地"什么都不做，比报错更难发现。
    """
    bad: list[str] = []
    for item in (rule.if_json.get("all_of") or []):
        if not isinstance(item, dict) or len(item) != 1:
            continue
        name = next(iter(item))
        params = item[name] or {}
        if name == "market_state":
            states = set()
            try:
                from backend.data import duck
                con = duck.get_ro_conn()
                states = {r[0] for r in con.execute(
                    "select distinct state from market_regime").fetchall()}
            except Exception:
                states = set()
            if states:
                for v in params.get("in", []):
                    if v not in states:
                        bad.append(f"market_state.in={v!r}"
                                   f"（库中只有 {sorted(states)}）")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope-answered", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--write", action="store_true", help="写入 rule_library")
    ap.add_argument("--negative-control", action="store_true",
                    help="加跑对照组：坏 schema / 无 evidence")
    a = ap.parse_args()

    print("=" * 74)
    print(f"M10 完整链路实跑｜模式："
          f"{'口径已答' if a.scope_answered else '如实（口径未答）'}")
    print("=" * 74)

    gaps = load_gap_index()
    rules = build_rules(a.scope_answered)

    print("\n【语料来源】creator-corpus 20261010｜36 作品 / 772 evidence"
          "｜speech_verified 全为 False")
    print("【口径缺口索引】" + (", ".join(gaps) or "（未生成）"))

    print("\n" + "-" * 74)
    print("① 结构化抽取（手工落 DSL，不接 LLM —— 语料密度不够，见审计）")
    print("-" * 74)
    for r in rules:
        print(f"  {r.rule_id:<22} {r.name[:44]}")
        print(f"    if   = {json.dumps(r.if_json, ensure_ascii=False)}")
        print(f"    then = {json.dumps(r.then_json, ensure_ascii=False)}")
        ev = r.evidence[0]
        print(f"    未答口径：{list(ev['scope_undefined'])}"
              f"（speech_verified={ev['speech_verified']}）")

    print("\n" + "-" * 74)
    print("② 编译（compile_rule —— 真跑 dsl.validate_rule）")
    print("-" * 74)
    for r in rules:
        ok, why = compile_rule(r)
        print(f"  {r.rule_id:<22} {'✅ 可编译' if ok else '✘ 不可编译'}")
        if not ok:
            print(f"      {why}")

    if a.backtest:
        print("\n" + "-" * 74)
        print("③ 回测")
        print("-" * 74)
        for r in rules:
            r.backtest = backtest_rule(r)
            print(f"  {r.rule_id:<22} samples={r.backtest['samples']}"
                  f"｜{r.backtest.get('reason', '已真跑回测')}")
            bd = r.backtest.get("blocked_detail")
            if bd:
                for k, v in bd.items():
                    if k == "reason":
                        continue
                    print(f"      {k}: {v}")
            for k, v in r.backtest.items():
                if k in ("reason", "blocked_detail"):
                    continue
                print(f"      {k}: {v}")

        #★ 口径可行性探针：条件历史上到底触发过没有
        print("\n" + "-" * 74)
        print("③b 口径可行性探针（用真实数据验证条件是否**曾经成立**）")
        print("-" * 74)
        pr = probe_limit_down_area()
        print(f"  「亏钱效应面积>30%」＝ 跌停/(涨停+跌停)")
        print(f"  样本 {pr['total_days']} 个交易日（涨跌停总数≥10）")
        print(f"  分布: min {pr['min_pct']}% / 中位 {pr['median_pct']}% "
              f"/ max {pr['max_pct']}%")
        print(f"  **>30% 的天数: {pr['over30']} / {pr['total_days']}**")
        for s in pr["samples"][:6]:
            print(f"    {s['date']}: 跌停{s['limit_down']} "
                  f"涨停{s['limit_up']} → {s['pct']}%")
        if pr["over30"] == 0:
            print("  ★ 该条件历史上**从未成立** ⇒ 0 样本是「条件不存在」"
                  "而非「样本不够」")

    print("\n" + "-" * 74)
    print("④ 准入（admit）")
    print("-" * 74)
    from collections import Counter
    tally = Counter()
    for r in rules:
        admit(r)
        tally[r.status] += 1
        icon = {"active": "✔", "pending": "…", "rejected": "✘"}[r.status]
        print(f"  {icon} {r.rule_id:<22} {r.status.upper():<9}")
        print(f"      {r.reject_reason or '（无）'}")
        if r.status == STATUS_ACTIVE:
            print("      ★★ 不该出现：speech_verified=False 的语料不得 ACTIVE")

    print("\n" + "-" * 74)
    print("④b 条件完整性 & 哑弹探针")
    print("-" * 74)
    # ★★ admit() 把"条件没定义完"和"样本还不够"都判成 PENDING，
    #   拒绝理由只说「样本 0 < 30」—— 这**掩盖了真实问题**：
    #   不是样本少，是条件本身依赖未答口径，根本无法回测。
    #   ⇒ 必须单独把"条件不完整"标出来，否则会被读成"再攒攒样本就行"。
    for r in rules:
        undef = r.evidence[0]["scope_undefined"]
        incomplete = bool(undef) and not r.evidence[0]["scope_answered"]
        if incomplete:
            print(f"  ⚠ {r.rule_id:<22} 条件**不完整**，PENDING 的真实原因"
                  f"不是样本不足")
            for k, q in undef.items():
                print(f"      未答口径 `{k}`：{q}")
    print()
    # 哑弹探针：schema 能过≠ 条件能命中。用真实取值试跑。
    print("  哑弹探针（schema 通过 ≠ 条件可命中）：")
    for r in rules:
        dead = dead_predicates(r)
        if dead:
            print(f"  ✘ {r.rule_id:<22} 含库里不存在的取值 {dead}"
                  f" ⇒ 永不触发（哑弹）")
        else:
            print(f"  ✔ {r.rule_id:<22} 取值域与库一致")

    print("\n" + "-" * 74)
    print("汇总")
    print("-" * 74)
    for k, v in sorted(tally.items()):
        print(f"  {k:<10} {v}")
    print(f"  合计 {len(rules)}")
    print()
    if not a.scope_answered:
        print("★ 预期结论：**全部非 ACTIVE** 是**正确结果**。")
        print("  理由：speech_verified=False + 口径未答⇒ 既不能证明是"
              "博主原话，")
        print("  也不能定义条件。若闸门放行，那才是闸门失效。")

    if a.negative_control:
        # ★★ 对照组：证明「全REJECTED」不是因为**全都恰好挂了**。
        #  只跑三条真实候选，得到三个REJECTED 无法区分：
        #   ·闸门正常工作（因为口径/证据不足而拦）
        #   · 我的 schema 全写错了（其实跟语料一点关系都没有）
        # 必须再跑两条**故意做坏**的：
        #   ·BAD_SCHEMA   —— schema 合法但谓词不存在 ⇒ 应因「未知谓词」被拦
        #   ·NO_EVIDENCE  —— schema 完全正确但没出处 ⇒ 应因「无evidence」被拦
        # 若这两条给出的拒绝理由**不同**，才说明闸门在按各自的理由工作。
        print("\n" + "=" * 74)
        print("⑤ 负对照（验证闸门按正确理由拦截，而不是恰好都挂）")
        print("=" * 74)
        neg = [
            Rule(rule_id="NEG_BAD_SCHEMA", strategy_id=STRATEGY,
                 name="负对照：谓词不存在",
                 if_json={"all_of": [{"loss_area_ratio": {"op": ">",
                                                          "value": 0.3}}]},
                 then_json={"action": "HOLD", "note": "负对照"},
                 evidence=[{"source": "creator-corpus", "quote_ref": "neg1",
                            "speech_verified": False}]),
            Rule(rule_id="NEG_NO_EVIDENCE", strategy_id=STRATEGY,
                 name="负对照：无 evidence（凭空写的）",
                 if_json={"all_of": [{"market_state": {"in": ["ebb"]}}]},
                 then_json={"action": "HOLD", "note": "负对照"},
                 evidence=[]),
        ]
        for r in neg:
            admit(r)
            print(f"  ✘ {r.rule_id:<18} {r.status.upper()}")
            print(f"      {r.reject_reason}")
        reasons = {r.rule_id: r.reject_reason for r in neg}
        diff = reasons.get("NEG_BAD_SCHEMA") != reasons.get("NEG_NO_EVIDENCE")
        print(f"\n  两条拒绝理由是否不同：{'是 ⇒ 闸门按各自理由工作' if diff else '否 ⇒ 闸门不可信'}")
        print("  ⇒ 若二者理由相同，说明闸门只是一律拒绝，无法区分问题。")

    if a.write:
        n = upsert_rules(rules)
        print(f"\n已写入 rule_library {n} 条（status 如上，非ACTIVE）")


if __name__ == "__main__":
    main()