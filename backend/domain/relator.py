# -*- coding: utf-8 -*-
"""
Hawk · 消息 → 个股关联（附录 4.6 · 三层证据）

实测背景（2026-10-08，新浪 7×24 30 条样本）：
    源自带 ext.stocks 关联的仅 14/30 条，其中**含 A 股的仅 2/30**，
    其余是美股/债券/加密货币。
    ⇒ 源关联**只能作辅助**，主链路必须是「LLM 抽取 + 名称映射 + 同板块校验」。

三层证据与强度分级（附录 4.6 要求每条结论可解释）：
    强证据  source   源直接给出且是 A 股代码 —— 最高可信，直接采信
    中证据  llm      LLM 抽取的名称/代码，且经本地化映射校验通过
    弱证据  sector   同板块推断 —— 仅作提示，不单独成结论

★ 为什么这样分级：
    消息打标错了会误导决策。宁可少关联（漏）也不可错关联（误）——
    错关联会让用户在毫不知情时关注一只完全无关的股票。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.domain.stock_index import get_index

# 板块关键词 → 板块名（用于弱证据的同板块校验）
# 数据来源：同花顺板块库（ths_board），此处只放高频词做快速匹配。
# 完整板块体系在 M5 实现。
SECTOR_KEYWORDS: dict[str, str] = {
    "银行": "银行", "券商": "证券", "保险": "保险", "地产": "房地产",
    "医药": "医药", "白酒": "白酒", "food": "食品", "食品": "食品",
    "芯片": "半导体", "半导体": "半导体", "电池": "电池", "新能源车": "汽车",
    "光伏": "电力设备", "军工": "国防军工", "煤炭": "煤炭", "钢铁": "钢铁",
    "有色": "有色金属", "化工": "化学制品", "汽车": "汽车", "家电": "家用电器",
    "通信": "通信", "软件": "软件服务", "机器人": "专用设备", "算力": "计算机设备",
    "算力": "计算机设备", "AI": "计算机设备", "人工智能": "计算机设备",
    "大模型": "计算机设备", "数据中心": "计算机设备", "光模块": "通信",
    "消费": "食品", "旅游": "商贸旅游", "酒店": "商贸旅游", "券商": "证券",
    "物流": "交通运输", "航运": "交通运输", "港口": "交通运输",
    "造纸": "轻工制造", "包装": "轻工制造", "家电": "家用电器",
    "农业": "农林牧渔", "养殖": "农林牧渔", "基建": "建筑装饰",
    "工程": "建筑装饰", "水泥": "建筑材料", "玻璃": "建筑材料",
}


@dataclass
class Evidence:
    """单条关联证据。"""

    code: str
    name: str | None
    tier: str                # source | llm | sector
    reason: str

    @property
    def weight(self) -> float:
        """证据权重，用于综合打分。"""
        return {"source": 1.0, "llm": 0.7, "sector": 0.3}[self.tier]


@dataclass
class Relation:
    """一条消息的关联结果。"""

    code: str
    name: str | None
    strength: float                     # 综合置信度 0~1
    tier: str# 最强证据层级
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "strength": round(self.strength, 3),
            "tier": self.tier,
            "reasons": self.reasons,
        }


class NewsRelator:
    """消息 → 个股关联器。

    ★ 纯函数要求（铁律二）：as_of 由调用方注入，不取系统时间。
    """

    def __init__(self) -> None:
        self.index = get_index()
        # 名称 → 常见误配对照（防止「代码合法但与消息矛盾」这类幻觉）
        # 场景：LLM 说「茅台」却给 002049（紫光国微）—— 代码合法，但明显矛盾。
        # 仅靠「代码在库中」无法拦住，必须做内容一致性校验。
        self._fuzzy_cache: dict[str, str] = {}

    # ---------------- 主入口 ----------------
    def relate(self, content: str,
               source_related: list[dict] | None = None,
               llm_names: list[str] | None = None,
               llm_codes: list[str] | None = None,
               as_of: datetime | None = None) -> list[Relation]:
        """综合三层证据，返回按置信度降序的关联列表。

        source_related: 源给的关联 [{symbol, name, market, is_a_share}, ...]
        llm_names:      LLM 抽取的公司名称/简称
        llm_codes:      LLM 直接给出的 6 位代码
        """
        evidences: dict[str, list[Evidence]] = {}

        def add(ev: Evidence) -> None:
            evidences.setdefault(ev.code, []).append(ev)

        # ---- ① 强证据：源关联 ----
        for r in source_related or []:
            code, why = None, ""
            #优先用 is_a_share 标记
            if r.get("is_a_share"):
                sym = str(r.get("symbol") or "")
                m = re.search(r"(\d{6})", sym)
                if m and self.index.resolve_code_or_name(m.group(1)):
                    code = m.group(1)
                    why = f"源直接标注 A 股关联（symbol={sym}）"
            # 其次尝试名称映射（源给的 name 字段可能是中文名）
            if not code and r.get("name"):
                code = self.index.lookup(str(r["name"]))
                if code:
                    why = f"源关联名称「{r['name']}」经本地映射命中"
            if code:
                add(Evidence(code, self.index.name_of(code), "source", why))

        # ---- ② 中证据：LLM 抽取 ----
        # LLM 给的名称要经本地化校验（防幻觉，附录 4.6）
        for n in llm_names or []:
            code = self.index.lookup(str(n))
            if code:
                add(Evidence(code, self.index.name_of(code), "llm",
                             f"LLM 抽取名称「{n}」命中本地股票库"))

        # ★★ 内容一致性校验：LLM 常「名称说 A、代码给 B」（实测：说茅台给 002049，
        #   而 002049 实为紫光国微）。单看代码合法，单看名称合法，但两者矛盾。
        #   若消息文本里出现了某个名称，而 LLM 给的代码对应的名字与该名称无关，
        #   判定为幻觉并丢弃 —— 宁可漏关联，不可错关联。
        for c in llm_codes or []:
            code = self.index.resolve_code_or_name(str(c))
            if not code:
                continue                    # 编造的代码，丢弃
            # ★ 消息里必须真的提到这家公司，否则视为 LLM 臆测。
            #   实测风险：面对「央行开展公开市场操作」这类宏观消息，
            #   7B 模型常顺手填一个它熟悉的代码（如 600519），
            #   若照单全收，用户会莫名关注一只完全无关的股票。
            #   这是「静默错误」—— 不报错，只污染推荐列表，比崩溃更糟。
            if not self._mentioned_in_content(code, content, llm_names or []):
                continue
            if self._contradicts_content(code, content, llm_names or []):
                continue# 矛盾 —— 丢弃，不落库
            add(Evidence(code, self.index.name_of(code), "llm",
                         f"LLM 抽取代码「{c}」在本地股票库中且消息确有提及"))

        # ---- ③ 弱证据：同板块（仅在已有强/中证据时才补充）----
        if evidences:
            hit_sectors = self._detect_sectors(content)
            if hit_sectors:
                for code, evs in list(evidences.items()):
                    nm = self.index.name_of(code) or ""
                    if self._stock_in_sectors(nm, hit_sectors):
                        add(Evidence(code, nm, "sector",
                                     f"与已关联个股同属板块 {'/'.join(hit_sectors)}"))

        # ---- 汇总 ----
        out: list[Relation] = []
        for code, evs in evidences.items():
            top = max(evs, key=lambda e: e.weight)
            # 多层证据叠加增强：同一代码被多个层级命中 -> 置信度更高
            tiers = {e.tier for e in evs}
            base = top.weight
            bonus = 0.15 * (len(tiers) - 1)
            strength = min(1.0, base + bonus)
            out.append(Relation(
                code=code,
                name=self.index.name_of(code),
                strength=strength,
                tier=top.tier,
                reasons=[f"[{e.tier}] {e.reason}" for e in evs],
            ))
        out.sort(key=lambda r: -r.strength)
        return out

    def _mentioned_in_content(self, code: str, content: str,
                              llm_names: list[str]) -> bool:
        """消息内容里是否真的提到了这家公司（其全名或简称）。

        LLM 给代码时必须过这一关 —— 对宏观/无关消息，
        模型会顺手填一个它熟悉的代码，照单全收就会产生「幽灵关联」。
        """
        cname = self.index.name_of(code)
        if not cname:
            return False
        c = content or ""

        # ① 全名出现
        if cname in c:
            return True
        # ② LLM 自己在 names 里提到了同一个实体（视为已提及）
        for n in llm_names or []:
            if str(n).strip() == cname or str(n).strip() in cname or cname in str(n):
                return True
        # ③ 简称出现（「贵州茅台」的简称「茅台」）
        cands = self._extract_company_names(c)
        if any(cname in n or n in cname for n in cands):
            return True
        return False

    def _contradicts_content(self, code: str, content: str,
                             llm_names: list[str]) -> bool:
        """判断「LLM 给的代码」是否与消息内容矛盾。

        实测的问题case：LLM 被问「茅台」相关消息时输出 002049（紫光国微）。
        这类错误单靠「代码合法性」拦不住，必须看代码对应的公司名
        是否与消息文本中的公司名匹配。

        判定规则（保守，宁可放过也不误杀）：
          · 若消息里出现的公司名能映射到别的代码，且该代码 ≠ 本代码 -> 矛盾
          · 否则不判矛盾（可能消息里根本没提公司名）
        """
        cname = self.index.name_of(code) or ""
        if not cname:
            return False

        # 消息文本里出现、且能映射到 A 股的公司名
        for n in self._extract_company_names(content):
            mapped = self.index.lookup(n)
            if mapped and mapped != code:
                # 消息里明确提到某公司，但 LLM 给的是另一只 —— 矛盾
                return True
        return False

    def _extract_company_names(self, content: str) -> list[str]:
        """从消息文本里抽取可能的公司名（用本地股票库反查）。

        做法：遍历索引中的名称，检查是否在内容里出现。
        5571 个名称逐一findin 在 200~500 字的消息里很快（不足 1ms），
        比维护一份「公司名词典」更可靠 —— 词典必然过时。

        ★ 必须同时扫「全名 + 别名」：
          实测教训 —— 消息写「茅台」而库里全名是「贵州茅台」，
          只扫全名会漏掉，导致防幻觉校验失效（002049紫光国微被放过）。
        """
        c = content or ""
        if not c:
            return []
        found = []
        # 全名（>= 2 字即可，A股名称最短 3 字）
        for name in self.index.by_name:
            if len(name) >= 2 and name in c:
                found.append(name)
        # 别名（含品牌简称「茅台」、去 ST 前缀「康佳A」）
        #
        # ★ 别名不做长度阈值过滤：
        #   品牌简称天然短（「茅台」只有 2 字），设 >=3 的阈值会把它漏掉
        #   —— 实测正是这个 bug 导致幻觉代码 002049（紫光国微）混过校验：
        #   消息写「茅台」，库里全名是「贵州茅台」，全名匹配不上、
        #   简称又因太短被过滤，导致「消息明确提到茅台」这个事实没被发现。
        #
        #   误命中风险由后处理控制：长名优先 + 短名被长名包含则丢弃，
        #   且最终结果只用于「一致性校验」（发现矛盾就丢代码），不直接产生关联。
        for alias in self.index.by_alias:
            if len(alias) >= 2 and alias in c:
                found.append(alias)
        # 名称长者优先（「贵州茅台」优于「茅台」）
        found.sort(key=len, reverse=True)
        # 去掉被更长名称包含的短名（避免「茅台」与「贵州茅台」重复计票）
        kept: list[str] = []
        for n in found:
            if not any(n in k and n != k for k in kept):
                kept.append(n)
        return kept[:10]        # 够用即可，避免极端情况全量遍历

    # ---------------- 板块工具 ----------------
    @staticmethod
    def _detect_sectors(content: str) -> list[str]:
        c = content or ""
        hits = set()
        for kw, sec in SECTOR_KEYWORDS.items():
            if kw.lower() in c.lower():
                hits.add(sec)
        return sorted(hits)

    @staticmethod
    def _stock_in_sectors(name: str, sectors: list[str]) -> bool:
        """个股名是否落在命中板块的关键词上。

        轻量近似：完整「个股→板块」映射在 M5 用同花顺板块成分表实现，
        这里只用名称关键词做保守判断，宁可不加也不加错。
        """
        n = (name or "").strip()
        if not n:
            return False
        return any(kw and kw in n for kw in
                   [k for s in sectors for k, v in SECTOR_KEYWORDS.items() if v == s])

    # ---------------- 辅助 ----------------
    def is_valid_code(self, code: str) -> bool:
        return self.index.resolve_code_or_name(code) is not None


if __name__ == "__main__":
    from datetime import timezone, timedelta

    CST = timezone(timedelta(hours=8))
    as_of = datetime(2026, 10, 8, 22, 20, tzinfo=CST)
    r = NewsRelator()

    print("=" * 74)
    print("=== M3-2 三层证据关联实测 ===")
    print("=" * 74)

    cases = [
        {
            "title": "源关联（A股）",
            "content": "【山东港口青岛港：外贸航线数量持续增加】",
            "source_related": [
                {"symbol": "601298", "name": "青岛港", "market": "cn", "is_a_share": True}
            ],
        },
        {
            "title": "源关联（美股，应忽略）",
            "content": "欧洲理事会主席科斯塔：将于今年启动乌克兰入盟谈判",
            "source_related": [
                {"symbol": "csgp", "name": "科斯塔", "market": "us", "is_a_share": False}
            ],
        },
        {
            "title": "LLM 抽取名称（命中）",
            "content": "宁德时代发布神行Plus 电池",
            "llm_names": ["宁德时代"],
        },
        {
            "title": "LLM 幻觉代码（应丢弃）",
            "content": "茅台获批重���",
            "llm_codes": ["002049"],     # 002049 不是茅台，是紫光国微
        },
        {
            "title": "真实代码",
            "content": "茅台获批重组",
            "llm_codes": ["600519"],
        },
        {
            "title": "多层证据叠加（源 + LLM 同指一代码）",
            "content": "【青岛港：业绩预增】",
            "source_related": [
                {"symbol": "601298", "name": "青岛港", "market": "cn", "is_a_share": True}
            ],
            "llm_names": ["青岛港"],
        },
    ]

    for c in cases:
        rels = r.relate(
            c["content"],
            source_related=c.get("source_related"),
            llm_names=c.get("llm_names"),
            llm_codes=c.get("llm_codes"),
            as_of=as_of,
        )
        print(f"\n【{c['title']}】")
        print(f"  消息: {c['content'][:46]}")
        if not rels:
            print("关联: （无 —— 已丢弃不可信关联）")
        for rel in rels:
            print(f"  → {rel.code} {rel.name}置信度{rel.strength:.2f} 层级[{rel.tier}]")
            for rs in rel.reasons:
                print(f"      {rs}")

    print("\n" + "=" * 74)
    print("要点：美股关联被忽略、幻觉代码被丢弃、多层证据提升置信度")