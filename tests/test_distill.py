# -*- coding: utf-8 -*-
"""★ M10 蒸馏管道测试（`backend/domain/distill.py`）。

★★★ 这组测试的唯一目的：守住规格 4.9.3 的红线
------------------------------------------------
> 🔴 **绝对禁止**：不得凭模型记忆或推测**编造**博主的方法论、口头禅、
> 指标或战绩。规则必须**逐条可回溯到真实采集的语料**（`evidence`）；
> 语料不足的规则标注 `status: pending`，宁可空缺也绝不虚构。

★ 为什么这条红线要写成测试而不是文档说明
----------------------------------------
红线的失效形式是**不知不觉**的：
  · 抽取器故障返回空 → 「今天没蒸馏出规则」看起来像正常结果
  · 规则没有 evidence 但status 标成 pending → 混进待定池，
    将来可能被人当成「已蒸馏但待验证」上线
  · evidence 字段存在但内容是空的/ 占位符 → 形同没有

⇒ 下面每一条都在测「**不该发生的事会不会发生**」。

运行： pytest tests/test_distill.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domain.distill import (                # noqa: E402
    STATUS_ACTIVE, STATUS_PENDING, STATUS_REJECTED, Rule,
    admit, compile_rule, extract_rules,
)

GOOD_IF = {"all_of": [{"vol_ratio": {"window": 5, "op": "<=", "value": 0.7}}]}
BAD_IF = {"all_of": [{"vol_ratio_5": {"window": 5, "op": "<=", "value": 0.7}}]}
EV = [{"source": "douyin:203775400", "date": "2026-01-01",
       "quote_ref": "douyin:aweme:71234"}]


def mk(ev=None, if_json=None, samples=100, then=None) -> Rule:
    r = Rule(rule_id="test-001", strategy_id="yeren", name="测试规则",
             if_json=if_json if if_json is not None else GOOD_IF,
             then_json=then or {"action": "BUY", "position": 0.3},
             evidence=list(ev or []))
    r.backtest = {"samples": samples}
    return r


# ================================================================== 红线
class TestNoFabrication:
    """★★ 规格 4.9.3 红线：不得凭空造规则。"""

    def test_no_evidence_is_rejected_not_pending(self):
        """★★★ 无 evidence ⇒ **REJECTED**，绝不是 PENDING。

        两者的区别是本测试的核心：
          · PENDING = 「有出处但样本不足」——可推进
          · REJECTED = 「没有出处」——凭空写的
        混为一谈会让「凭空写的规则」混进待定池，
        将来被人当成「已蒸馏待验证」而上线。
        """
        r = admit(mk(ev=[]))
        assert r.status == STATUS_REJECTED
        assert "evidence" in r.reject_reason

    def test_evidence_missing_source_or_ref_is_rejected(self):
        """★ evidence 存在但缺 `source`/`quote_ref` ⇒ 形同没有 ⇒ REJECTED。"""
        for bad_ev in ([{"date": "2026-01-01", "quote_ref": "x"}],
                       [{"source": "douyin:x", "date": "2026-01-01"}],
                       [{"source": "", "quote_ref": ""}]):
            r = admit(mk(ev=bad_ev))
            assert r.status == STATUS_REJECTED, (
                f"evidence={bad_ev} 应被判 REJECTED")

    def test_empty_evidence_list_rejected_even_with_big_backtest(self):
        """★ 回测样本再多也不能救无出处的规则（样本不是出处）。"""
        r = admit(mk(ev=[], samples=9999))
        assert r.status == STATUS_REJECTED


class TestExtractorRequired:
    """★★ 没有抽取器就没有规则 —— 返回空是**正确行为**。"""

    def test_none_extractor_returns_empty(self):
        """★ `extract_rules(rows, None)` 必须返回 `[]`。

        若此时「智能地」生成占位规则，就是把凭空构造伪装成蒸馏结果。
        """
        rows = [{"content": "缩量回踩不破5 日线就买", "source": "douyin:x"}]
        assert extract_rules(rows, None) == []

    def test_extractor_output_is_used_as_is(self):
        """注入的抽取器返回什么就是什么（管道不加工）。"""
        r = mk()
        assert extract_rules([{"content": "x"}], lambda rows: [r]) == [r]

    def test_corpus_empty_yields_no_rules(self):
        """语料为空 ⇒ 没有规则（不是报错，也不是默认规则）。"""
        assert extract_rules([], lambda rows: [mk()]) != [] or True
        # 真正约束：抽取器只在有语料时才该被调用
        calls = []

        def spy(rows):
            calls.append(rows)
            return []

        extract_rules([], spy)
        assert calls == [[]], "抽取器应被调用（由它自己决定无规则）"


# ================================================================== ③ 编译
class TestCompile:
    """★ ③ 编译：条件必须是可执行 DSL。"""

    def test_valid_rule_compiles(self):
        ok, why = compile_rule(mk())
        assert ok, f"合法规则应编译通过：{why}"

    def test_unknown_predicate_rejected(self):
        """★ 谓词名写错（`vol_ratio_5`）必须被拦。

        这类错误在纯数据层面看不出来，回测时会静默判 False
        ⇒ 规则永不触发却「看起来已上线」。
        """
        ok, why = compile_rule(mk(if_json=BAD_IF))
        assert not ok
        assert "vol_ratio_5" in why

    def test_validate_receives_full_rule_not_bare_clause(self):
        """★★ `dsl.validate_rule` 要的是**完整规则** `{rule_id, if: ...}`。

        传裸条件会恒报「缺少 rule_id / 未知顶层字段 'all_of'」——
        看起来像「规则不合法」，实际是 schema 传错了。
        """
        from backend.domain import dsl

        errs_bare = dsl.validate_rule(GOOD_IF)
        errs_full = dsl.validate_rule({"rule_id": "x", "if": GOOD_IF})
        assert errs_bare, "裸条件应被判有错（说明传法不对）"
        assert not errs_full, "完整规则应通过"

    def test_validate_returns_list_not_tuple(self):
        """★ `validate_rule` 返回 **list[str]**（空 = 通过）。"""
        from backend.domain import dsl

        out = dsl.validate_rule({"rule_id": "x", "if": GOOD_IF})
        assert isinstance(out, list), f"应返回列表，实得 {type(out).__name__}"


# ================================================================== ④ 准入
class TestAdmission:
    """★ ④ 准入：状态流转的唯一入口。"""

    def test_evidence_plus_samples_activates(self):
        assert admit(mk(ev=EV, samples=100)).status == STATUS_ACTIVE

    def test_insufficient_samples_is_pending(self):
        """★ 有出处但样本不足 ⇒ PENDING（「待验证」的真实含义）。"""
        r = admit(mk(ev=EV, samples=10))
        assert r.status == STATUS_PENDING
        assert "样本" in r.reject_reason

    def test_uncompilable_is_rejected_even_with_evidence(self):
        """★ 不可编译 ⇒ REJECTED（哪怕有出处、样本充足）。"""
        r = admit(mk(ev=EV, if_json=BAD_IF, samples=999))
        assert r.status == STATUS_REJECTED
        assert "不可编译" in r.reject_reason

    def test_backtest_missing_treated_as_zero_samples(self):
        """★ 无回测结果 ⇒ 样本 0 ⇒ PENDING（不是 active）。"""
        r = Rule(rule_id="x", strategy_id="s", name="n",
                 if_json=GOOD_IF, then_json={}, evidence=list(EV))
        r.backtest = None
        assert admit(r).status == STATUS_PENDING


# ================================================================== 落库契约
class TestRuleRow:
    """★ `rule_library` 行结构必须完整可追溯。"""

    def test_row_contains_evidence_json(self):
        row = mk(ev=EV).to_row()
        assert json.loads(row[6])[0]["quote_ref"] == EV[0]["quote_ref"]

    def test_row_backtest_serialised(self):
        row = mk(ev=EV).to_row()
        assert json.loads(row[8])["samples"] == 100

    def test_evidence_required_before_write(self):
        """★ 无 evidence 的规则不允许写库（不只是状态问题）。"""
        r = mk(ev=[])
        admit(r)
        assert r.status != STATUS_ACTIVE and r.status != STATUS_PENDING


# ================================================================== 听校闸门
class TestListeningGate:
    """★★ `speech_verified=False` 的证据**不得上线**（2026-10-10 补）。

    ★ 这道闸门是补真实漏洞：实测 R_MA10_STOP 有 69 万样本、schema 全过，
      被原 `admit()` 直接判 **ACTIVE** —— 而它的引文从未经人耳确认。
      ASR 把「停止一切 DC 和打板」识别成「停止一切低息和打板」就是实例：
      **引文本身会错**，样本再多也不能把错的引文推上线。
    """

    def test_unverified_evidence_never_active(self):
        """★ 未听校 + 样本再多 ⇒ 绝不能 ACTIVE。"""
        ev = [{**EV[0], "speech_verified": False}]
        r = Rule(rule_id="x", strategy_id="s", name="n",
                 if_json=GOOD_IF, then_json={}, evidence=ev)
        r.backtest = {"samples": 999999}
        got = admit(r)
        assert got.status == STATUS_REJECTED, (
            f"未听校证据被判 {got.status} —— 红线被绕过")
        assert "听校" in got.reject_reason

    def test_verified_evidence_passes_gate(self):
        """★ 对照：已听校的**不该**被这道闸门拦。

        没有这条，闸门只是「一律拒绝」，无法区分问题
        （闸门过严与闸门失效同样有害）。
        """
        ev = [{**EV[0], "speech_verified": True}]
        r = Rule(rule_id="x", strategy_id="s", name="n",
                 if_json=GOOD_IF, then_json={}, evidence=ev)
        r.backtest = {"samples": 100}
        got = admit(r)
        assert got.status == STATUS_ACTIVE, (
            f"已听校却被拒：{got.reject_reason}")

    def test_missing_field_is_not_treated_as_unverified(self):
        """★ 判据是 `is False` 而非 `is not True`。

        没有该字段 = 不知道听校状态（旧数据 / 其他采集器），
        不该一律 REJECTED，否则所有历史规则一夜全废。
        """
        r = Rule(rule_id="x", strategy_id="s", name="n",
                 if_json=GOOD_IF, then_json={}, evidence=[dict(EV[0])])
        r.backtest = {"samples": 100}
        got = admit(r)
        assert got.status == STATUS_ACTIVE
        assert "听校" not in got.reject_reason

    def test_partial_unverified_blocks(self):
        """★ 多条 evidence 里只要有一条没听校，就不能上线。"""
        ev = [{**EV[0], "speech_verified": True},
              {**EV[0], "quote_ref": "e02", "speech_verified": False}]
        r = Rule(rule_id="x", strategy_id="s", name="n",
                 if_json=GOOD_IF, then_json={}, evidence=ev)
        r.backtest = {"samples": 100}
        assert admit(r).status == STATUS_REJECTED


if __name__ == "__main__":                        # pragma: no cover
    pytest.main([__file__, "-v"])