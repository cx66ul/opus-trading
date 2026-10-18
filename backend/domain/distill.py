# -*- coding: utf-8 -*-
"""★ 策略蒸馏管道（M10 · 规格 4.9.2的四步工作流）。

================================ 规格红线 ==================================
> 🔴 **绝对禁止**：不得凭模型记忆或推测**编造**博主的方法论、口头禅、
> 指标或战绩。规则必须**逐条可回溯到真实采集的语料**（`evidence` 字段）；
> 语料不足的规则标注 `status: pending`，**宁可空缺也绝不虚构**。

⇒ 本模块的硬约束（`tests/test_distill.py` 守护）：
  1. **没有 evidence 的规则一律不许进 `rule_library`** ——
     `evidence` 为空时直接判定 `REJECTED`，不是 `PENDING`。
     「待定」的含义是「有出处但样本不足」，**不是「没出处」**。
  2. 规则的 `if` 必须是**可编译的 DSL**（`backend/domain/dsl.py` 的 22 谓词），
     不可编译的规则不能标 `active`。
  3. 同一批语料重复蒸馏必须**幂等**（按 rule_id upsert）。

=============================== 四步工作流 ==================================
  ① 语料采集   → `scripts/scrape_douyin.py`（已实现，需登录态）
  ② 结构化抽取 → `extract_rules()`：语料 → 「条件 → 动作」规则草案
  ③ 编译回测   → `compile_rule()`：DSL 判定 + 回测（走 `backtest`）
  ④ 准入上线   → `admit()`：仅通过样本外验证的规则进 `active`

★★ 为什么本模块**不自己调 LLM**
-------------------------------
LLM 抽取的结果必须可复核，而「模型从语料里读出了什么」无法审计。
本模块只负责**结构化与准入判定**，抽取由外部注入（`extractor` 参数）：
  · 接LLM：把 `extractor` 接到本地 7B / 云端，产出仍走同一套校验
  · 不接：语料不足以支撑规则时，正确做法是**不产出规则**

运行：
    python -m backend.domain.distill --status       # 查看各步骤存量
    python -m backend.domain.distill --selfcheck    # 管道自检（不需语料）
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST = timezone(timedelta(hours=8))

# 规则状态
STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"      # 有出处但样本不足
STATUS_REJECTED = "rejected"    # 无出处 / 不可编译 / 回测不过


@dataclass
class Rule:
    """蒸馏出的一条规则（对应规格 4.9.2 的 schema）。"""
    rule_id: str
    strategy_id: str
    name: str
    if_json: dict                # 条件（DSL schema）
    then_json: dict              # 动作
    timeframe: str = "1d"
    evidence: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    backtest: dict | None = None
    status: str = STATUS_PENDING
    version: str = "1"

    # ---- 准入判定用的派生信息 ----
    reject_reason: str = ""

    def to_row(self) -> tuple:
        """→ `rule_library` 表的一行。"""
        return (self.rule_id, self.strategy_id, self.name,
                json.dumps(self.if_json, ensure_ascii=False),
                json.dumps(self.then_json, ensure_ascii=False),
                self.timeframe,
                json.dumps(self.evidence, ensure_ascii=False),
                self.confidence, json.dumps(self.backtest, ensure_ascii=False)
                if self.backtest else None,
                self.status, self.version)


# ==================================================================② 抽取
Extractor = Callable[[list[dict]], list[Rule]]


def extract_rules(corpus_rows: list[dict],
                  extractor: Extractor | None = None) -> list[Rule]:
    """② 结构化抽取：语料 → 规则草案。

    ★ `extractor` 为 None 时返回空列表 —— **这是正确的**：
      没有抽取器就没有规则，凭空生成就是「编造」。
    """
    if extractor is None:
        return []
    return extractor(corpus_rows)


# ================================================================== ③ 编译
def compile_rule(rule: Rule) -> tuple[bool, str]:
    """规则的条件部分能否编译为可执行 DSL 判定。

    返回 `(ok, reason)`。不可编译的规则**不能**标 `active`。

    为什么必须真编译：`if_json` 是嵌套结构，
    谓词名写错（如 `vol_ratio_5` 而非 `vol_ratio`）在纯数据层面看不出来，
    只在回测时静默判 False ⇒ 规则永不触发却「看起来已上线」。

    ★ `dsl.validate_rule(rule) -> list[str]` 返回**错误列表**
      （空列表 = 通过），不是 `(ok, err)` 二元组。

    ★★ 它校验的是**完整规则** `{rule_id, if: {all_of: [...]}}`，
      不是裸条件。直接传 `rule.if_json` 会恒报
      「缺少 rule_id / 未知顶层字段 'all_of'」——
      看起来像「规则不合法」，实际是**schema 传错了**。
    """
    try:
        from backend.domain import dsl
        payload = {"rule_id": rule.rule_id, "if": rule.if_json}
        errs = list(dsl.validate_rule(payload) or [])
        return (not errs), ("; ".join(map(str, errs))[:300])
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ================================================================== ④ 准入
def admit(rule: Rule, min_samples: int = 30) -> Rule:
    """按证据链与可编译性定状态。这是**唯一的准入入口**。

    判定顺序（顺序有意义）：
      1. **无证据 → REJECTED**（红线，不是 PENDING）
      2. 不可编译 → REJECTED
      3. 回测样本不足 → PENDING（有出处，只是还不能上线）
      4. 回测通过 → ACTIVE
    """
    # ★ 第1 条：红线。没有 evidence 一律 REJECTED。
    #   曾想用 PENDING（"等语料"），但那会让「凭空写的规则」
    #   混进待定池，将来可能被人当成「已蒸馏但待验证」——
    #   而它的真实状态是「没有出处」。两者必须区分。
    if not rule.evidence:
        rule.status = STATUS_REJECTED
        rule.reject_reason = "无 evidence（规格 4.9.3 红线：不得凭记忆编造）"
        return rule

    for ev in rule.evidence:
        if not ev.get("quote_ref") or not ev.get("source"):
            rule.status = STATUS_REJECTED
            rule.reject_reason = (
                f"evidence 缺 source/quote_ref，无法回溯：{ev}")
            return rule

    # ★★ 第 1.5 道闸门：**明确标注未听校的证据不得上线**（2026-10-10 补）。
    #   为什么必须有：`admit()` 原本只看「evidence 非空 + 能编译 + 样本够」，
    #   而 `speech_verified=False`（ASR/OCR 双源都没听校）意味着
    #   **引文本身可能识别错**。实测踩到：R_MA10_STOP 有 69 万样本，
    #   直接被判 ACTIVE —— 等于把「未经人耳确认的数字」推上线。
    #
    #   ★ 判据用 `is False` 而非 `is not True`：
    #     「显式标了False」＝明确知道没听校 ⇒ 必须拦；
    #     「没这个字段」＝旧数据/其他采集器不知道听校状态
    #     ⇒ 拦下来会让所有历史规则一夜全废，且与本条红线的原意
    #     （针对**编造**）不符。
    unverified = [ev for ev in rule.evidence
                  if ev.get("speech_verified") is False]
    if unverified:
        rule.status = STATUS_REJECTED
        rule.reject_reason = (
            f"证据未听校（speech_verified=False，{len(unverified)}/"
            f"{len(rule.evidence)} 条）——引文可能识别错误，"
            f"不得上线（规格 4.9.3）")
        return rule

    ok, why = compile_rule(rule)
    if not ok:
        rule.status = STATUS_REJECTED
        rule.reject_reason = f"条件不可编译：{why}"
        return rule

    bt = rule.backtest or {}
    n = int(bt.get("samples") or 0)
    if n < min_samples:
        rule.status = STATUS_PENDING
        rule.reject_reason = f"回测样本 {n} < {min_samples}，暂不上线"
        return rule

    rule.status = STATUS_ACTIVE
    rule.reject_reason = ""
    return rule


# ================================================================== 落库
def upsert_rules(rules: list[Rule]) -> int:
    """幂等写 `rule_library`（主键 `rule_id`）。"""
    if not rules:
        return 0
    from backend.data.sqlite import get_conn

    con = get_conn()
    con.executemany(
        """
        INSERT INTO rule_library
          (rule_id, strategy_id, name, if_json, then_json, timeframe,
           evidence, confidence, backtest, status, version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (rule_id) DO UPDATE SET
          name=EXCLUDED.name, if_json=EXCLUDED.if_json,
          then_json=EXCLUDED.then_json, evidence=EXCLUDED.evidence,
          confidence=EXCLUDED.confidence, backtest=EXCLUDED.backtest,
          status=EXCLUDED.status, version=EXCLUDED.version
        """,
        [r.to_row() for r in rules],
    )
    con.commit()
    return len(rules)


def load_corpus(strategy_id: str | None = None,
                limit: int = 500) -> list[dict]:
    """读 `corpus`（②的输入）。"""
    from backend.data.sqlite import get_conn

    con = get_conn()
    if strategy_id:
        rows = con.execute(
            "SELECT strategy_id, source, pub_time, content, transcript_ref"
            " FROM corpus WHERE strategy_id = ?"
            " ORDER BY pub_time DESC LIMIT ?", (strategy_id, limit)).fetchall()
    else:
        rows = con.execute(
            "SELECT strategy_id, source, pub_time, content, transcript_ref"
            " FROM corpus ORDER BY pub_time DESC LIMIT ?", (limit,)).fetchall()
    return [{"strategy_id": r[0], "source": r[1], "pub_time": r[2],
             "content": r[3] or "", "transcript_ref": r[4] or ""} for r in rows]


# ================================================================== 自检
if __name__ == "__main__":                       # pragma: no cover
    print("=" * 76)
    print("M10 蒸馏管道 · 状态")
    print("=" * 76)
    from backend.data.sqlite import get_conn

    con = get_conn()
    for t, label in (("corpus", "① 语料"), ("rule_library", "④ 规则库")):
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except Exception as exc:
            n = f"ERR {exc}"
        print(f"  {label:10s} {t:14s} {n}")

    try:
        import json as _j
        rs = con.execute(
            "SELECT rule_id, status, count(*) FROM rule_library"
            " GROUP BY status").fetchall()
        if rs:
            print("\n  规则状态分布:")
            for rid, st, n in rs:
                print(f"    {st:10s} {n:>4d}")
    except Exception:
        pass

    print()
    print("★ 红线：没有 evidence 的规则一律 REJECTED，"
          "**不得凭记忆补写语料或规则**。")
    print("★ 采集需先登录：python scripts/scrape_douyin.py yeren --login")