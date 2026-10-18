# -*- coding: utf-8 -*-
"""★ 语料**可用性体检**（M10 第① 步的闸门）。

================================ 为什么要这个 ================================
规格4.9.3 要求「规则必须逐条可回溯到真实采集的语料」。
但「采集到了 N 条」**不等于**「N 条都能支撑规则」——
抖音采集器落库的 `content` 是**作品的标题/文案**，
而**方法论在视频音频里**，不在标题里。

⇒ 若不先体检就接LLM 抽取，模型面对
  「10月9号复盘#金融#财经#股市」这种标题，
  唯一能产出「复盘日看涨跌幅」这类东西的方式就是**编造**。
  而 `admit()` 只检查「evidence 字段齐不齐」，
  **查不出 quote 本身有没有信息量**——
  一条指向空标题的 evidence 照样能通过准入，
  最后得到「有出处但毫无内容」的规则库。这是比无规则更糟的假阳性。

⇒ 本模块**只做度量，不做抽取**。它的结论决定「值不值得接 LLM」。

四项体检指标：
  1. `dup_ratio`   标题/描述重复率（同一段文字被存了两遍）
  2. `noise_ratio`  非财经内容占比（穿搭/段子/生活日常）
  3. `rule_bearing` 含**可判定条件**的语料占比
     —— 判据：必须出现「阈值 + 方向 + 动作」三要素之一
  4. `coverage`     每个候选源的体检结果

=========================== ★ 最重要的一条=============================
**`rule_bearing` 才是真正的闸门。**
只有标题类语料时它会接近 0，此时正确的结论是
「语料不足以支撑规则，**不要接抽取器**」，
而不是降低门槛让流程「跑通」。

运行：
    python -m backend.domain.corpus_audit
    python -m backend.domain.corpus_audit --dump rule_bearing
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST_TZ = "+08:00"

# ── 非财经噪声：这些内容里没有可蒸馏的交易规则 ──────────────────
# ★ 判据用**词**而非整句，避免「#股票」这种标签把生活vlog 放进来。
NOISE_PATTERNS = [
    r"#穿搭", r"#裙子", r"#今天穿什么", r"#ootd",
    r"#舞蹈", r"#唱歌", r"#日常分享", r"#生活碎片",
    r"#抽象", r"#搞笑", r"#段子", r"#萌", r"#宠物",
    r"#美食", r"#旅游", r"#游戏", r"#川渝暴龙",
    r"#remotion", r"deepseek", r"codex", r"#vibe",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.I)

# ── 财经内容的最弱信号：至少得在讲行情/交易 ────────────────────
FIN_RE = re.compile(
    r"#(金融|财经|股市|股票|股民|A股|投资|理财|交易|复盘|实盘|ETF|港股|美股)"
    r"|涨停|跌停|板块|仓位|止盈|止损|选股|大盘|行情|个股|半导体|创新药"
    r"|固态电池|风电|CPO|PCB|文化传媒|存储芯片|商业航天|无人驾驶|锂电"
    # ★ 缠论是本项目的核心方法论，必须算财经语义。
    #   漏了它会让「出现底背驰就低吸」这种**真规则**被判 actionable=False
    #   —— 闸门过严与闸门失效一样有害，都会把真规则挡在门外。
    r"|缠论|背驰|中枢|分型|笔|线段|一买|二买|三买|一卖|二卖|三卖",
    re.I,
)

# ── ★ 可判定条件的要素：阈值 / 方向 / 动作────────────────────
#    三者齐备才可能编译成 DSL 的 `if -> then`。
#    只有「方向+动作」而无阈值的是**观点**，不是规则。
NUMERIC_RE = re.compile(r"\d+(\.\d+)?\s*(%|个点|倍|万|亿|天|日|成仓)")
THRESHOLD_HINT_RE = re.compile(
    r"(超过|大于|高于|低于|小于|跌破|站上|突破|回撤|放大|缩小|连续)\s*"
    r"\d|\d+\s*(以上|以下|以内|之上|之下)"
    r"|\d+\s*%|MA\d|macd|MACD|均线|量比|换手|布林|金叉|死叉|背驰|中枢",
)
ACTION_RE = re.compile(
    r"(买入|卖出|加仓|减仓|清仓|建仓|持有|空仓|观望|止盈|止损|"
    r"低吸|追高|打板|抄底|逃顶|上车|离场|进场)"
)

HASHTAG_RE = re.compile(r"#\S+")
# 平台自动贴的免责声明，不是博主的方法论
DISCLAIMER_RE = re.compile(
    r"(不构成(任何)?投资建议|股市有风险|入市需谨慎|仅个人记录|非投资建议)")


@dataclass
class ItemAudit:
    transcript_ref: str
    pub_time: str
    text: str
    dup: bool            # 标题与描述重复
    noise: bool          # 非财经
    fin: bool            # 财经相关
    has_threshold: bool  # 有阈值/指标
    has_action: bool     # 有动作
    actionable: bool     # 三要素齐备 => 可能是规则
    tag_only: bool       # 去掉 hashtag 后正文为空

    @property
    def reason(self) -> str:
        if self.noise:
            return "非财经内容"
        if self.tag_only:
            return "仅 hashtag 标签，无正文"
        if not self.fin:
            return "无财经语义"
        if self.dup:
            return "标题=描述（同一段文字存两遍）"
        if not self.has_threshold and not self.has_action:
            return "既无阈值也无动作（纯观点/无信息量）"
        if self.actionable:
            return "★ 可支撑规则"
        return "缺要素（仅部分）"


def audit_text(content: str, pub_time: str = "",
               ref: str = "") -> ItemAudit:
    raw = content or ""
    # ★ 判定「重复」要先剥掉免责声明，否则免责声明行会让两半都非空而误判。
    core = DISCLAIMER_RE.sub(" ", raw)
    half = len(core) // 2
    dup = bool(core.strip()) and half > 10 and core[:half].strip() == core[half:].strip()

    body = HASHTAG_RE.sub(" ", raw)
    body = DISCLAIMER_RE.sub(" ", body).strip()
    tag_only = len(HASHTAG_RE.findall(raw)) > 0 and len(body) == 0

    has_thr = bool(NUMERIC_RE.search(body) or THRESHOLD_HINT_RE.search(body))
    has_act = bool(ACTION_RE.search(body))
    return ItemAudit(
        transcript_ref=ref, pub_time=pub_time, text=raw.strip(),
        dup=dup,
        noise=bool(NOISE_RE.search(raw)) and not bool(FIN_RE.search(raw)),
        fin=bool(FIN_RE.search(raw)),
        has_threshold=has_thr,
        has_action=has_act,
        # ★ 三要素：阈值 + 动作 + 财经语义
        actionable=has_thr and has_act and bool(FIN_RE.search(raw)),
        tag_only=tag_only,
    )


def audit_corpus(rows: list[dict]) -> dict:
    """→ 体检汇总。`rows` 来自 `distill.load_corpus()`。"""
    items = [audit_text(r.get("content", ""), r.get("pub_time", ""),
                        r.get("transcript_ref", "")) for r in rows]
    n = len(items) or 1

    def ratio(pred) -> float:
        return round(100.0 * sum(1 for i in items if pred(i)) / n, 1)

    by_source: dict[str, dict] = {}
    for r, i in zip(rows, items):
        s = r.get("strategy_id") or "(未标注)"
        d = by_source.setdefault(s, {"n": 0, "actionable": 0})
        d["n"] += 1
        d["actionable"] += 1 if i.actionable else 0

    return {
        "total": len(items),
        "dup_ratio": ratio(lambda i: i.dup),
        "tag_only_ratio": ratio(lambda i: i.tag_only),
        "noise_ratio": ratio(lambda i: i.noise),
        "fin_ratio": ratio(lambda i: i.fin),
        "has_threshold_ratio": ratio(lambda i: i.has_threshold),
        "has_action_ratio": ratio(lambda i: i.has_action),
        # ★ 真正的闸门
        "rule_bearing": ratio(lambda i: i.actionable),
        "by_source": by_source,
        "items": items,
    }


def verdict(report: dict, threshold: int = 5) -> tuple[bool, str]:
    """语料够不够支撑规则蒸馏？

    ★ 阈值 5%（即 100 条里至少 5 条能编译成规则）刻意定得很低：
      「只有 5% 可用」在实盘上仍然不足以做统计，
      但足以说明**抽取器有没有东西可抽**——
      若连5% 都不到，接 LLM 就是让它对着标题编规则。
    """
    rb = report["rule_bearing"]
    n = report["total"]
    if n == 0:
        return False, "语料为空，先跑采集。"
    if rb < threshold:
        return False, (
            f"★ 语料不足以支撑规则蒸馏：仅 {rb}%（{n} 条中"
            f" {round(n*rb/100)} 条）含「阈值+动作+财经语义」三要素，"
            f"低于 {threshold}% 门槛。**不要接抽取器**——"
            f"对着标题抽取唯一的结果是编造。"
        )
    return True, f"可支撑蒸馏：{rb}% 的语料含可判定条件。"


if __name__ == "__main__":                      # pragma: no cover
    from backend.domain.distill import load_corpus

    rows = load_corpus(limit=1000)
    rep = audit_corpus(rows)
    ok, msg = verdict(rep)

    print("=" * 76)
    print("M10 语料可用性体检")
    print("=" * 76)
    print(f"  语料总数{rep['total']}")
    print("-" * 76)
    for k, label in (
        ("dup_ratio", "标题/描述重复"),
        ("tag_only_ratio", "仅 hashtag 无正文"),
        ("noise_ratio", "非财经噪声"),
        ("fin_ratio", "财经相关"),
        ("has_threshold_ratio", "含阈值/指标"),
        ("has_action_ratio", "含动作词"),
        ("rule_bearing", "★ 可支撑规则（三要素齐）"),
    ):
        print(f"  {label:26s} {rep[k]:>6.1f}%")
    print("-" * 76)
    print("  分源明细:")
    for s, d in rep["by_source"].items():
        p = round(100.0 * d["actionable"] / max(d["n"], 1), 1)
        print(f"    {s:20s} {d['n']:>4d} 条  可支撑 {p:>5.1f}%")
    print("=" * 76)
    print(("✔ " if ok else "✘ ") + msg)

    if "--dump" in sys.argv:
        for i in rep["items"]:
            if i.actionable:
                print(f"\n★ [{i.pub_time[:10]}] {i.transcript_ref}\n{i.text[:300]}")
