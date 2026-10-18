# -*- coding: utf-8 -*-
"""`corpus_audit` 的守护测试（M10 第① 步闸门）。

★ 本测试的价值全在**「不能让它失效」**：
  体检若悄悄失灵，下游就会接抽取器对着标题编规则——
  这是比「没有规则」更糟的失败，因为它看起来是成功的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.domain.corpus_audit import (  # noqa: E402
    audit_corpus, audit_text, verdict,
)


# ─────────────────────────── 三要素判定（闸门的核心）
class TestActionableGate:
    def test_full_three_elements_is_actionable(self):
        a = audit_text("#半导体 站上20日线且量比大于2就加仓，跌破10日线减仓 #股票")
        assert a.fin and a.has_threshold and a.has_action
        assert a.actionable is True

    def test_direction_without_threshold_is_not_actionable(self):
        """只有动作词、没有阈值 ⇒ 是**观点**不是规则。

        这条最关键：模型最容易把「我觉得该跌了」包装成
        「跌幅超过 X 就买」，而那正是编造。
        """
        a = audit_text("#股票 明天感觉要跌，建议大家清仓离场")
        assert a.has_action is True
        assert a.has_threshold is False
        assert a.actionable is False

    def test_threshold_without_action_is_not_actionable(self):
        a = audit_text("#行情 涨停家数超过120家，连板高度到5板")
        assert a.has_threshold is True
        assert a.has_action is False
        assert a.actionable is False

    def test_indicator_name_counts_as_threshold(self):
        """指标名（金叉/背驰/均线）本身即阈值，无需数字。"""
        a = audit_text("#缠论 出现底背驰就低吸，出现顶背驰就减仓")
        assert a.has_threshold and a.has_action
        assert a.actionable is True

    def test_non_financial_never_actionable(self):
        a = audit_text("站上20日线就买，跌破就卖 #穿搭 #裙子")
        assert a.actionable is False


# ─────────────────────────── 噪声过滤
class TestNoiseFilter:
    def test_lifestyle_is_noise(self):
        a = audit_text("今天的穿搭 #穿搭 #裙子 #今天穿什么")
        assert a.noise is True

    def test_financial_tag_overrides_noise_pattern(self):
        """带 #股票 的内容即便含 '日常分享' 也不该被判成噪声。"""
        a = audit_text("复盘记录 #股票 #日常分享")
        assert a.noise is False
        assert a.fin is True

    def test_tech_unrelated_content_is_noise(self):
        a = audit_text("用 deepseek 和 codex 做 remotion 动画")
        assert a.noise is True


# ─────────────────────────── 重复检测
class TestDupDetection:
    def test_title_desc_duplication_detected(self):
        """抖音 desc 唯一、title 从它截断 ⇒ 落库时同一段写两遍。"""
        t = "10月9号复盘 #金融 #财经 #股市 #股票 #股民"
        assert audit_text(t + "\n" + t).dup is True

    def test_disclaimer_does_not_block_dup_detection(self):
        """★ 判定重复前必须先剥掉免责声明。

        否则「正文 + 免责声明」两半都非空且不相等 ⇒ 漏判。
        """
        body = "10月9号复盘 半导体板块日内震荡 #金融"
        t = body + "本视频仅个人记录，不构成投资建议。股市有风险，入市需谨慎"
        assert audit_text(t + "\n" + t).dup is True

    def test_genuinely_different_lines_not_dup(self):
        a = audit_text("第一行内容不同 #金融\n第二行也完全不同 #股市")
        assert a.dup is False

    def test_short_text_not_flagged_as_dup(self):
        """半段≤10 字符时不做判定 —— 否则短文本会被随机判成重复。"""
        assert audit_text("相同").dup is False


# ─────────────────────────── tag_only
class TestTagOnly:
    def test_hashtags_only_is_tag_only(self):
        assert audit_text("#金融 #财经 #股市").tag_only is True

    def test_hashtag_plus_words_is_not_tag_only(self):
        assert audit_text("复盘 #金融 #财经").tag_only is False


# ─────────────────────────── 汇总与裁决
class TestVerdict:
    def test_zero_actionable_blocks_distillation(self):
        """★ 本测试守护的红线：全是标题时必须**拒绝**接抽取器。"""
        rows = [{"content": f"10月9号复盘 #金融 #股市 #{i}",
                 "transcript_ref": f"r{i}", "strategy_id": "yeren"}
                for i in range(20)]
        rep = audit_corpus(rows)
        assert rep["rule_bearing"] == 0.0
        ok, msg = verdict(rep)
        assert ok is False
        assert "不要接抽取器" in msg

    def test_enough_actionable_allows(self):
        rows = [{"content": "站上20日线且量比大于2就加仓 #股票",
                 "transcript_ref": f"r{i}", "strategy_id": "s"} for i in range(20)]
        ok, _ = verdict(audit_corpus(rows))
        assert ok is True

    def test_empty_corpus_is_blocked(self):
        ok, msg = verdict(audit_corpus([]))
        assert ok is False
        assert "为空" in msg

    def test_threshold_is_deliberately_low(self):
        """门槛 5% 而非 50% —— 因为要判断的是「有没有东西可抽」，
        不是「够不够做统计」。这点必须显式锁住，防止有人后来「顺手提高」。"""
        rows = ([{"content": "占位 #股市", "transcript_ref": "p"}] * 19
                + [{"content": "站上20日线且量比大于2就加仓 #股票",
                    "transcript_ref": "x"}])
        rep = audit_corpus(rows)
        assert rep["rule_bearing"] == 5.0
        assert verdict(rep)[0] is True
        # 4% 则拒绝
        rows2 = ([{"content": "占位 #股市", "transcript_ref": "p"}] * 19
                 + [{"content": "占位 #股市", "transcript_ref": "y"}])
        assert verdict(audit_corpus(rows2))[0] is False

    def test_by_source_breakdown(self):
        rows = [{"content": "站上20日线且量比大于2就加仓 #股票",
                 "transcript_ref": "a", "strategy_id": "s1"},
                {"content": "今天天气不错 #穿搭", "transcript_ref": "b",
                 "strategy_id": "s2"}]
        rep = audit_corpus(rows)
        assert rep["by_source"]["s1"]["actionable"] == 1
        assert rep["by_source"]["s2"]["actionable"] == 0


# ─────────────────────────── 变异测试（证明测试真阳性）
class TestMutationResistance:
    def test_mutating_gate_would_be_caught(self):
        """★ 把 `and` 改成 `or`（门槛形同虚设）必须让判定翻转。

        这是「测试是否真阳性」的唯一可靠判据：
        变异后行为是否改变。

        ★ 语料必须选**只差一个要素**的：
          「站上20日线就加仓」阈值与动作都有，本来就 actionable，
          用它当样本无法区分 `and` 与 `or`（变异前后都是 True），
          这就是**第四类假阳性：样本在两种写法下行为等价**。
        """
        rows = [{"content": "涨停家数连续3天超过120家 #股票",
                 "transcript_ref": "a"} for _ in range(20)]
        rep = audit_corpus(rows)
        # 原判定：只有阈值无动作 ⇒ 全不可支撑规则
        assert rep["rule_bearing"] == 0.0
        # 模拟变异：and → or ⇒ 门槛失效，全部变成「可支撑」
        assert verdict(rep)[0] is False
        assert rep["has_threshold_ratio"] == 100.0   # 变异后 100% 通过

        # 模拟变异：门槛被改成 or
        item = audit_text(rows[0]["content"])
        assert (item.has_threshold or item.has_action) is True   # 变异后
        assert (item.has_threshold and item.has_action) is False  # 原判定

    def test_mutating_noise_filter_would_be_caught(self):
        a = audit_text("今天的穿搭 #穿搭 #裙子")
        assert a.noise is True
        # 模拟变异：噪声正则被清空 ⇒ 该条不再被判噪声
        assert bool(a.fin) is False   # 仍非财经 ⇒ fin/noise 双判据都在
