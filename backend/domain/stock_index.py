# -*- coding: utf-8 -*-
"""
Hawk · 股票名称 → 代码映射（M3 前置）

为什么必须有这个模块：
    「消息 → 个股」关联链路上有三处都拿不到 6 位代码：
      · LLM 抽取出来的是股票名称/简称（如「宁德时代」）
      · 新浪 7×24 的 ext.stocks 给的是 name 字段（key）与 symbol（常为美股代码）
      · 同花顺 hybk 行业字段给的是行业名，不含代码
    因此必须有一张本地映射表把名称归一到代码。

实测基础（2026-10-08，全市场 5571 只）：
    · **名称零重名** —— 5571 只股票无任何重名，这使「名称→代码」可��靠
    · 名称长度 3~6 字
    · ST 股带 ST / *ST 前缀（如「*ST康佳A」），新闻里常只写「康佳A」

★ 设计要点：别名必须显式生成，不靠模糊匹配。
  模糊匹配（编辑距离/子串）在这个场景下风险很高：
  「中国平安」与「中国人寿」编辑距离仅 2，一旦误匹配，
  会把一条消息挂到错误的公司上 —— 这类错误在 TradingView 上极常见。
  因此本模块只做**确定性映射**，并对未命中项明确返回 None，由上层决定是否放弃。

用途：
    1. LLM 输出的 related_names → related_codes
    2. 源给的 ext.stocks.name → related_codes
    3. M6 推荐模块的行业→成分股反查
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ST / *ST / S / S* 前缀（实测存在 ST、*ST 两种；S* / S 为科创板风险警示）
_ST_PREFIX = re.compile(r"^\*?ST\s*", re.IGNORECASE)
_RISK_PREFIX = re.compile(r"^S\*?\s*", re.IGNORECASE)
_SPACES = re.compile(r"\s+")

# ★ 品牌简称的**地域**前缀白名单
#   起因：新闻与 LLM 常写「茅台」，而库里全名是「贵州茅台」。
#   若不能识别简称，防幻觉校验会失效 —— 实测「茅台」配002049（紫光国微）
#   能通过校验，正是因为「茅台」匹配不上任何全名。
#
#   ★ 只放地域词，不用行业词：「中国平安」拆成「平安」太危险
#     （平安银行、平安保险都会命中），风险远大于收益。
#   ★ 剥离后不足 2 字的不生成别名（「京」这类歧义太大）。
_BRAND_PREFIXES = (
    "贵州", "四川", "云南", "广西", "广东", "海南", "福建", "浙江", "江苏",
    "山东", "河南", "河北", "湖南", "湖北", "安徽", "陕西", "甘肃", "青海",
    "内蒙古", "西藏", "宁夏", "新疆", "重庆", "北京", "上海", "天津", "辽宁",
    "吉林", "黑龙江", "山西", "江西",
)
_ALIAS_MIN_LEN = 2


@dataclass
class StockNameIndex:
    """名称 → 代码索引。别名表在构造时一次性生成。"""

    by_name: dict[str, str] = field(default_factory=dict)          # 全名 -> 代码
    by_alias: dict[str, list[str]] = field(default_factory=dict)     # 别名 -> [代码]
    names: dict[str, str] = field(default_factory=dict)             # 代码 -> 全名
    # ★ 字段名不能叫 is_st —— 会与方法 is_st() 冲突（实测踩过：
    #   'function' object does not support item assignment）
    st_flags: dict[str, bool] = field(default_factory=dict)         # 代码 -> 是否ST

    # ---------------- 索引构建 ----------------
    @classmethod
    def build(cls, rows: list[tuple]) -> "StockNameIndex":
        """从 (code, name, is_st) 三元组构建索引。"""
        idx = cls()
        for code, name, st_flag in rows:
            if not code or not name:
                continue
            name = str(name).strip()
            code = str(code).strip()

            # 实测名称零重名；此处仍做保护，重名时保留首个并记录冲突
            if name in idx.by_name and idx.by_name[name] != code:
                idx.by_alias.setdefault(name, []).append(code)
                continue
            idx.by_name[name] = code
            idx.names[code] = name
            idx.st_flags[code] = bool(st_flag)

            for alias in cls._gen_aliases(name):
                idx.by_alias.setdefault(alias, []).append(code)
        # 去重别名列表
        for k, v in list(idx.by_alias.items()):
            seen, uniq = set(), []
            for c in v:
                if c not in seen:
                    seen.add(c)
                    uniq.append(c)
            idx.by_alias[k] = uniq
        return idx

    @staticmethod
    def _gen_aliases(name: str) -> set[str]:
        """生成别名集合。

        举例（实测名称）：
            「*ST康佳A」 -> {康佳A, *ST康佳A, ST康佳A}   （新闻常不带 *）
            「贵州茅台」 -> {贵州茅台, 茅台}              （★ 品牌简称）
            「N 某某」  -> {N某某}                      （去掉内部空格）
        """
        out: set[str] = {name}
        raw = name.strip()

        # 去掉空格与全角空格
        nospace = _SPACES.sub("", raw).replace("　", "")
        if nospace and nospace != raw:
            out.add(nospace)

        # ST 系：*ST康佳A -> 康佳A；ST人福 -> 人福
        stripped = _ST_PREFIX.sub("", nospace)
        stripped = _RISK_PREFIX.sub("", stripped)
        if stripped and stripped != nospace:
            out.add(stripped)                 # 新闻里最常见的写法

        # ★ 品牌简称：去掉常见地域前缀（「贵州茅台」->「茅台」）
        #   起因：实测防幻觉校验时发现，LLM/新闻常写「茅台」，
        #   而库里全名是「贵州茅台」，全名匹配不上导致幻觉代码（002049 紫光国微）
        #   混过校验。必须能识别简称。
        #   仅剥离**地域白名单**，不做通用分词，避免过度猜测。
        for pref in _BRAND_PREFIXES:
            if nospace.startswith(pref):
                short = nospace[len(pref):]
                if len(short) >= _ALIAS_MIN_LEN:
                    out.add(short)
                break
        return {a for a in out if a}

    # ---------------- 查询 ----------------
    def lookup(self, name: str) -> str | None:
        """名称 → 代码。查不到返回 None（**绝不猜**）。"""
        if not name:
            return None
        n = _SPACES.sub("", str(name).strip()).replace("　", "")
        if n in self.by_name:
            return self.by_name[n]
        if n in self.by_alias:
            cands = self.by_alias[n]
            # 别名对应多个代码时不做选择 —— 宁可无结果也不误匹配
            return cands[0] if len(cands) == 1 else None
        return None

    def lookup_many(self, names: list[str]) -> dict[str, str]:
        """批量查。返回 {代码: 原始名称}，只含成功命中的。"""
        out: dict[str, str] = {}
        for n in names or []:
            c = self.lookup(n)
            if c:
                out[c] = n
        return out

    def resolve_code_or_name(self, token: str) -> str | None:
        """token 可能是代码也可能是名称 —— 先试代码，再试名称。

        LLM 经常直接输出 6 位代码（这样反而更可靠），也常输出名称。
        两种都要支持。
        """
        if not token:
            return None
        t = str(token).strip()
        if re.fullmatch(r"\d{6}", t):
            return t if t in self.names else None
        # 带市场前缀的形式 sh600519 / SZ000001
        m = re.fullmatch(r"(?i)(sh|sz|bj)\s*(\d{6})", t)
        if m:
            code = m.group(2)
            return code if code in self.names else None
        return self.lookup(t)

    # ---------------- 元信息 ----------------
    def name_of(self, code: str) -> str | None:
        return self.names.get(code)

    def is_st(self, code: str) -> bool:
        return self.st_flags.get(code, False)

    def __len__(self) -> int:
        return len(self.names)

    def stats(self) -> dict:
        return {
            "股票数": len(self.names),
            "全名数": len(self.by_name),
            "别名数": len(self.by_alias),
            "ST 股数": sum(1 for v in self.st_flags.values() if v),
        }


# ------------------------------------------------------------------ 全局单例
_INDEX: StockNameIndex | None = None


def get_index(rebuild: bool = False) -> StockNameIndex:
    """从 SQLite 的 stock 表构建并缓存。

    股票列表变化时（新增/退市）传 rebuild=True 重建。
    """
    global _INDEX
    if _INDEX is not None and not rebuild:
        return _INDEX
    from backend.data.sqlite import get_conn

    rows = get_conn().execute(
        "SELECT id, name, is_st FROM stock WHERE status != 'delisted'"
    ).fetchall()
    _INDEX = StockNameIndex.build([(r[0], r[1], r[2]) for r in rows])
    return _INDEX


if __name__ == "__main__":
    idx = get_index(rebuild=True)
    print("=== 索引统计 ===")
    for k, v in idx.stats().items():
        print(f"  {k:8s} {v:>6,}")

    print("\n=== ST 前缀剥离（新闻里常写「康佳A」而非「*ST康佳A」）===")
    for probe in ["*ST康佳A", "ST人福", "康佳A", "人福", "*ST田野"]:
        print(f"  {probe:12s} -> {idx.lookup(probe)}")

    print("\n=== 常见简称 ===")
    for probe in ["贵州茅台", "中国平安", "宁德时代", "比亚迪", "五粮液"]:
        print(f"  {probe:8s} -> {idx.lookup(probe)}")

    print("\n=== 代码与名称双向（LLM 可能直接给代码）===")
    for probe in ["600519", "300750", "sh600519", "SZ000001"]:
        print(f"  {probe:12s} -> {idx.resolve_code_or_name(probe)}")

    print("\n=== 未命中必须返回 None，绝不猜 ===")
    for probe in ["不存在的公司", "中国", "", "茅台", "美国"]:
        print(f"  {probe!r:14s} -> {idx.lookup(probe)}")

    print("\n★ 为什么不用模糊匹配：")
    print("  「中国平安」与「中国人寿」编辑距离仅 2。")
    print("  一旦误匹配，消息会挂到错误公司上 —— 宁可漏，不可错。")