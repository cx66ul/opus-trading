# -*- coding: utf-8 -*-
"""
Hawk · M3 消息→个股关联测试

核心用例都来自实测踩坑，尤其是防幻觉那一类——
这类 bug 不会报错，只会让用户莫名其妙地关注一只无关股票。

运行： pytest tests/test_relator.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.relator import NewsRelator
from backend.domain.stock_index import StockNameIndex

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 8, 22, 30, tzinfo=CST)


@pytest.fixture(scope="module")
def relator():
    return NewsRelator()


# ================================================================== 名称索引
class TestStockNameIndex:
    def setup_method(self):
        self.idx = StockNameIndex.build([
            ("600519", "贵州茅台", 0),
            ("300750", "宁德时代", 0),
            ("000016", "*ST康佳A", 1),
            ("600079", "ST人福", 1),
            ("601318", "中国平安", 0),
            ("000001", "平安银行", 0),
        ])

    def test_full_name_lookup(self):
        assert self.idx.lookup("贵州茅台") == "600519"

    def test_st_prefix_stripped(self):
        """新闻常写「康佳A」而非「*ST康佳A」。"""
        assert self.idx.lookup("康佳A") == "000016"
        assert self.idx.lookup("人福") == "600079"

    def test_brand_short_alias(self):
        """★ 新闻与 LLM 常写「茅台」，库里全名是「贵州茅台」。

        这条别名是防幻觉校验的前提 ——
        没有它，消息里的「茅台」匹配不上任何全名，
        就无法发现「代码给成002049」这种矛盾。
        """
        assert self.idx.lookup("茅台") == "600519"

    def test_industry_words_not_stripped(self):
        """★ 不能剥离行业词：「中国平安」不能拆成「平安」。

        「平安」会同时命中中国平安与平安银行，歧义太大。
        """
        assert self.idx.lookup("中国平安") == "601318"
        # 「平安」应指向两个代码 => lookup 返回 None（不猜）
        assert self.idx.lookup("平安") is None

    def test_ambiguous_returns_none(self):
        """别名对应多个代码时返回 None，绝不猜。"""
        assert self.idx.lookup("平安") is None

    def test_code_resolution(self):
        """LLM 可能直接给代码，可能带市场前缀。"""
        assert self.idx.resolve_code_or_name("600519") == "600519"
        assert self.idx.resolve_code_or_name("sh600519") == "600519"
        assert self.idx.resolve_code_or_name("SZ300750") == "300750"
        assert self.idx.resolve_code_or_name("贵州茅台") == "600519"

    def test_unknown_returns_none(self):
        assert self.idx.lookup("不存在的公司") is None
        assert self.idx.lookup("") is None
        assert self.idx.resolve_code_or_name("999999") is None

    def test_whitespace_normalized(self):
        assert self.idx.lookup(" 贵州茅台 ") == "600519"


# ================================================================== 三层证据
class TestRelation:
    def test_source_a_share_accepted(self, relator):
        rels = relator.relate(
            "【山东港口青岛港：外贸航线数量持续增加】",
            source_related=[{"symbol": "601298", "name": "青岛港",
                             "market": "cn", "is_a_share": True}],
            as_of=AS_OF,
        )
        assert rels and rels[0].code == "601298"
        assert rels[0].tier == "source"
        assert rels[0].strength >= 1.0

    def test_us_stock_ignored(self, relator):
        """实测：源关联里有大量美股/债券/加密货币，不能当 A 股用。"""
        rels = relator.relate(
            "欧洲理事会主席科斯塔：将于今年启动乌克兰入盟谈判",
            source_related=[{"symbol": "csgp", "name": "科斯塔",
                             "market": "us", "is_a_share": False}],
            as_of=AS_OF,
        )
        assert rels == [], "美股关联不应产生 A 股关联"

    def test_llm_name_hit(self, relator):
        rels = relator.relate("宁德时代发布神行Plus 电池",
                              llm_names=["宁德时代"], as_of=AS_OF)
        assert rels and rels[0].code == "300750"
        assert rels[0].tier == "llm"

    def test_multi_evidence_boosts_confidence(self, relator):
        """同一代码被源与 LLM 同时命中 -> 置信度更高、证据更全。"""
        rels = relator.relate(
            "【青岛港：业绩预增】",
            source_related=[{"symbol": "601298", "name": "青岛港",
                             "market": "cn", "is_a_share": True}],
            llm_names=["青岛港"],
            as_of=AS_OF,
        )
        assert rels and rels[0].code == "601298"
        assert len(rels[0].reasons) >= 2, "两层证据都应保留"
        assert rels[0].strength >= 1.0

    def test_sorted_by_confidence(self, relator):
        rels = relator.relate("宁德时代与青岛港都有公告",
                              llm_names=["宁德时代", "青岛港"], as_of=AS_OF)
        scores = [r.strength for r in rels]
        assert scores == sorted(scores, reverse=True)


# ================================================================== 防幻觉
class TestAntiHallucination:
    """★ 这组用例是 M3 最重要的防线。

    幻觉不会报错，只会让用户莫名其妙关注一只无关股票 ——
    这种「静默错误」比崩溃危险得多。
    """

    def test_fabricated_code_dropped(self, relator):
        """LLM 编造的代码（不在本地库）必须丢弃。"""
        rels = relator.relate("某公司发布公告", llm_codes=["999999"], as_of=AS_OF)
        assert rels == [], "编造代码必须丢弃"

    def test_mismatched_code_dropped(self, relator):
        """★ 核心用例：名称说「茅台」，代码给 002049（实为紫光国微）。

        代码本身合法（002049 在库中），单看任何一项都通过，
        只有把「消息内容」和「代码对应的公司」对照才能发现矛盾。

        修复历程：
          1. 最初无校验 —— 放过
          2. 加内容校验，但只扫全名 —— 消息写「茅台」，
             库里全名「贵州茅台」匹配不上，仍放过
          3. 加品牌简称别名「茅台」—— 仍放过
          4. 发现别名字符串扫描被 len>=3 阈值过滤 —— 放行
          5. 去掉别名长度阈值 —— 终于拦住
        """
        rels = relator.relate("茅台获批重组", llm_codes=["002049"], as_of=AS_OF)
        assert rels == [], "名称与代码矛盾时必须丢弃（002049 是紫光国微，不是茅台）"

    def test_correct_code_kept(self, relator):
        """对照组：名称与代码一致时应正常保留。"""
        rels = relator.relate("茅台获批重组", llm_codes=["600519"], as_of=AS_OF)
        assert rels and rels[0].code == "600519"

    def test_company_name_extraction_finds_brand(self, relator):
        """简称也必须能被抽出来（这是上一条用例成立的前提）。"""
        names = relator._extract_company_names("茅台获批重组")
        assert any("茅台" in n for n in names), \
            f"应抽到「茅台」，实际抽出 {names}"

    def test_no_relation_when_content_unrelated(self, relator):
        """消息里没有任何公司名时，不应凭代码硬造关联。"""
        rels = relator.relate("央行开展公开市场操作", llm_codes=["600519"],
                              as_of=AS_OF)
        assert rels == [], "消息无公司名时不应强行关联"


# ================================================================== 板块弱证据
class TestSectorEvidence:
    def test_sector_keyword_detected(self, relator):
        secs = relator._detect_sectors("宁德时代发布新电池技术")
        assert "电池" in secs

    def test_no_sector_keyword(self, relator):
        secs = relator._detect_sectors("央行开展公开市场操作")
        assert secs == []


# ================================================================== 纯函数
class TestPurity:
    def test_no_system_time(self):
        """关联器属领域层，禁取系统时间（铁律二）。"""
        import ast
        import re

        p = ROOT / "backend" / "domain" / "relator.py"
        tree = ast.parse(p.read_text(encoding="utf-8"))

        def strip_docstrings(node):
            for child in ast.walk(node):
                if not hasattr(child, "body") or not isinstance(
                    getattr(child, "body", None), list
                ):
                    continue
                b = child.body
                if (b and isinstance(b[0], ast.Expr)
                        and isinstance(getattr(b[0], "value", None), ast.Constant)
                        and isinstance(b[0].value.value, str)):
                    child.body = b[1:] or [ast.Pass()]
            return node

        body = [
            n for n in tree.body
            if not (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                    and getattr(n.test.left, "id", "") == "__name__")
        ]
        code = ast.unparse(strip_docstrings(ast.Module(body=body, type_ignores=[])))
        pat = re.compile(r"\b(datetime\.now|time\.time)\s*\(")
        assert not pat.search(code), "relator 出现了系统时间调用"