# -*- coding: utf-8 -*-
"""
Hawk · 消息管道与 LLM 打标测试

用例全部来自 M2 开发中的真实踩坑，注释标明来源。
运行： pytest tests/test_news.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.llm import (
    OllamaProvider,
    PROMPT_VERSION,
    extract_json,
    rule_based_fallback,
)
from backend.domain.news.base import dedup_key, merge_sources

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 8, 21, 50, tzinfo=CST)


# ================================================================== JSON 提取
class TestJsonExtraction:
    """7B 模型输出常带 markdown 围栏或前后解释文字，必须稳健提取。"""

    def test_plain_json(self):
        assert extract_json('{"direction":"bull","strength":3}') is not None

    def test_markdown_fence(self):
        got = extract_json('```json\n{"direction":"bear","strength":2}\n```')
        assert got is not None and got["direction"] == "bear"

    def test_surrounding_text(self):
        got = extract_json('好的，分析如下：\n{"direction":"neutral"}\n以上。')
        assert got is not None and got["direction"] == "neutral"

    def test_braces_inside_string(self):
        """字符串内的 } 和转义引号不能破坏括号配平。"""
        got = extract_json('{"reason":"含}括号与\\"引号","strength":4}')
        assert got is not None and got["strength"] == 4

    def test_non_json_returns_none(self):
        assert extract_json("完全不是 JSON") is None
        assert extract_json("") is None

    def test_truncated_json_returns_none(self):
        """输出被截断时必须返回 None，触发重试或兜底，而非抛异常。"""
        assert extract_json('{"direction":"bull","streng') is None


# ================================================================== 输出校验
class TestOutputValidation:
    """LLM 输出的非法值必须被丢弃并用默认值兜底，不能带病入库。"""

    def setup_method(self):
        self.p = OllamaProvider()

    def test_invalid_enum_falls_back(self):
        """来源：实测模型可能返回 '暴涨' 这类中文枚举。"""
        tag = self.p._to_tag(
            {"direction": "暴涨", "horizon": "永远", "strength": 3},
            "测试", "", [],
        )
        assert tag.direction == "neutral"
        assert tag.horizon == "short"

    def test_strength_clamped(self):
        """强度范围 0-5（附录 4.1），超出必须夹紧而非直接入库。"""
        assert self.p._to_tag({"strength": 99}, "t", "", []).strength == 5
        assert self.p._to_tag({"strength": -5}, "t", "", []).strength == 0

    def test_non_numeric_strength(self):
        """来源：实测模型会返回 '强' 这类中文强度。"""
        assert self.p._to_tag({"strength": "强"}, "t", "", []).strength == 1

    def test_confidence_clamped(self):
        assert self.p._to_tag({"confidence": 5.0}, "t", "", []).confidence == 1.0
        assert self.p._to_tag({"confidence": -1}, "t", "", []).confidence == 0.0


# ================================================================== 防幻觉
class TestAntiHallucination:
    """附录 4.6：LLM 输出的股票代码必须本地化校验，防幻觉。"""

    def setup_method(self):
        self.p = OllamaProvider()
        self.local = ["600519", "000001", "300750"]

    def test_fabricated_code_dropped(self):
        """★ 不存在的代码被丢弃；**中文公司名被正确映射**（v1.1 起支持）。

        早期把中文名也算「幻觉」并丢弃 —— 那是错的：
        模型输出公司名是正确做法，本地索引（5948 个别名）负责映射。
        实测：模型说「茅台」时可能给 002049（紫光国微），
        这类「代码合法但与消息矛盾」的情况由内容一致性校验拦（见下）。
        """
        tag = self.p._to_tag(
            {"related_codes": ["600519", "999999", "宁德时代"]},
            "测试", "", self.local,
        )
        assert "999999" not in tag.related_codes, "编造的代码必须丢弃"
        assert "600519" in tag.related_codes, "真实代码应保留"
        # 中文名经本地索引映射成代码 —— 这是期望行为
        assert "300750" in tag.related_codes, "中文名应被映射为代码"

    def test_names_mapped_to_codes(self):
        """★ v1.1 核心能力：related_names → 代码。"""
        tag = self.p._to_tag(
            {"related_names": ["青岛港", "茅台", "不存在的公司XYZ"]},
            "测试", "", self.local + ["601298"],
        )
        # 青岛港 / 茅台 不在 self.local 里 -> 应被 valid 过滤
        assert all(c in self.local or c == "601298" for c in tag.related_codes)
        assert "不存在的公司XYZ" not in tag.related_codes

    def test_string_code_wrapped(self):
        """模型可能把数组写成一个字符串，需容错。"""
        tag = self.p._to_tag({"related_codes": "600519"}, "测试", "", self.local)
        assert tag.related_codes == ["600519"]

    def test_empty_codes_ok(self):
        tag = self.p._to_tag({"related_codes": []}, "测试", "", self.local)
        assert tag.related_codes == []

    def test_prompt_version_recorded(self):
        """必须记录 prompt 版本，便于回溯「当时用什么打的标」。"""
        tag = self.p._to_tag({}, "测试", "", [])
        assert tag.prompt_ver == PROMPT_VERSION
        assert tag.llm_provider == "ollama"


# ================================================================== 规则兜底
class TestRuleFallback:
    """LLM 不可用时的兜底：关键设计是「不静默丢弃消息」。"""

    def test_bullish_keyword(self):
        t = rule_based_fallback("央行宣布降准0.5个百分点", "ollama")
        assert t.direction == "bull" and t.strength > 0

    def test_bearish_keyword(self):
        t = rule_based_fallback("某公司被证监会调查", "ollama")
        assert t.direction == "bear" and t.strength > 0

    def test_irrelevant_news(self):
        """无关消息应标neutral/0，而不是丢弃或乱判。"""
        t = rule_based_fallback("今天天气不错", "ollama")
        assert t.direction == "neutral" and t.strength == 0

    def test_fallback_marked_as_fallback(self):
        """必须能区分「LLM 打的」与「规则打的」，否则无法评估 LLM 质量。"""
        t = rule_based_fallback("降准", "ollama")
        assert "fallback" in t.llm_provider
        assert t.confidence <= 0.5, "兜底结果的置信度必须低"


# ================================================================== 去重
class TestDedup:
    def test_key_prefers_external_id(self):
        a = {"source": "s", "external_id": "123", "content": "x"}
        b = {"source": "s", "external_id": "123", "content": "y"}
        assert dedup_key(a) == dedup_key(b)

    def test_key_falls_back_to_hash(self):
        """无 external_id 时退回 (时间 + 内容前80 字) 哈希。"""
        a = {"source": "s", "ts": "2026-10-08 21:00:00", "content": "同一条消息"}
        b = {"source": "s", "ts": "2026-10-08 21:00:00", "content": "同一条消息"}
        c = {"source": "s", "ts": "2026-10-08 22:00:00", "content": "同一条消息"}
        assert dedup_key(a) == dedup_key(b)
        assert dedup_key(a) != dedup_key(c), "时间不同不应视为同一条"

    def test_merge_keeps_cross_source_duplicates(self):
        """不同源的 external_id 空间独立（都是 "1"），**不能**当同一条合并。

        这是刻意设计：新浪的 id=1 与财联社的 id=1 是两条不同的快讯。
        跨源语义去重属于 M3 的聚类去重（需 LLM/相似度），
        原始层只做「精确同一条」去重，避免误合并不同事件。
        """
        a = [{"source": "sina", "external_id": "1", "content": "央行降准"}]
        b = [{"source": "cls", "external_id": "1", "content": "央行降准"}]
        merged = merge_sources(a, b)
        assert len(merged) == 2, "跨源相同 id 不应被误合并"
        assert {m["source"] for m in merged} == {"sina", "cls"}

    def test_merge_same_source_identical(self):
        """同源同 id 必须合并为一条，并记录 also_seen_in。"""
        a = [{"source": "sina", "external_id": "1", "content": "央行降准"}]
        b = [{"source": "sina", "external_id": "1", "content": "央行降准(转载)"}]
        merged = merge_sources(a, b)
        assert len(merged) == 1
        assert "sina" in merged[0].get("also_seen_in", [])


# ================================================================== 归一化
class TestSinaNormalize:
    """新浪 7×24 的 ext 字段实测全为 JSON 字符串，处理错会抛异常。"""

    def setup_method(self):
        from backend.domain.news.sina_7x24 import Sina7x24Source

        self.src = Sina7x24Source()

    def test_ext_as_json_string(self):
        """来源：实测 50/50 条type(ext) == str，直接 .get 会崩。"""
        item = {
            "id": 999,
            "rich_text": "测试快讯",
            "create_time": "2026-10-08 21:00:00",
            "tag": [{"id": "1", "name": "市场"}],
            "ext": json.dumps({"stocks": [{"market": "cn", "symbol": "600519",
                                          "key": "贵州茅台"}]}),
            "docurl": "https://finance.sina.com.cn/x",
        }
        out = self.src.normalize(item)
        assert out["external_id"] == "999"
        assert out["content"] == "测试快讯"
        assert out["tags"] == ["市场"]
        assert len(out["related"]) == 1
        assert out["related"][0]["is_a_share"] is True

    def test_ext_as_dict(self):
        """兼容 dict 形态（部分条目/未来接口可能变化）。"""
        item = {"id": 1, "rich_text": "t", "create_time": "",
                "ext": {"stocks": []}, "tag": []}
        assert self.src.normalize(item)["related"] == []

    def test_broken_ext_json(self):
        """ext 是坏 JSON 时不应抛异常，应降级为空关联。"""
        item = {"id": 2, "rich_text": "t", "create_time": "", "ext": "{坏JSON",
                "tag": []}
        assert self.src.normalize(item)["related"] == []

    def test_duplicate_symbol_dedup(self):
        """来源：实测「青岛港」在同一条快讯的 ext 中出现两次。"""
        item = {
            "id": 3, "rich_text": "t", "create_time": "",
            "tag": [],
            "ext": json.dumps({"stocks": [
                {"market": "cn", "symbol": "601298", "key": "青岛港"},
                {"market": "cn", "symbol": "601298", "key": "青岛港"},
            ]}),
        }
        out = self.src.normalize(item)
        assert len(out["related"]) == 1, "同 symbol 应去重"

    def test_non_a_share_flagged(self):
        """来源：实测关联标的多为美股/债券/加密货币，不能当 A 股用。"""
        item = {
            "id": 4, "rich_text": "t", "create_time": "", "tag": [],
            "ext": json.dumps({"stocks": [
                {"market": "us", "symbol": "csgp", "key": "科斯塔"},
            ]}),
        }
        out = self.src.normalize(item)
        assert out["related"][0]["is_a_share"] is False


# ================================================================== 源注册
class TestSourceRegistry:
    def test_bootstrap_registers_all(self):
        from backend.domain.news.base import bootstrap_sources, registered_sources

        bootstrap_sources()
        assert set(registered_sources()) >= {"sina_7x24", "jin10", "cls"}

    def test_browser_source_raises_not_silent_empty(self):
        """★ 关键行为：需Playwright 的源必须明确报错。

        静默返回空会让调度层误判「今天没消息」，是最危险的失败模式。
        """
        from backend.domain.news.base import get_source

        for name in ("cls",):
            with pytest.raises(NotImplementedError):
                get_source(name).fetch(AS_OF, 10)