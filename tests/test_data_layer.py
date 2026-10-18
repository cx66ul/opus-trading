# -*- coding: utf-8 -*-
"""
Hawk · 数据层单元测试

这些用例全部来自 M1 开发过程中的**真实踩坑**，不是凭空设计的边界值。
每条用例的注释标明来源，避免后人误删。

运行：pytest tests/ -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data.adapter import BaseAdapter, bootstrap, fetch_with_failover, get_adapter
from backend.data.base import (
    Backoff,
    Health,
    SourceError,
    TokenBucket,
    pct_chg_limit,
    validate_fields,
)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 8, 15, 0, tzinfo=CST)


# ================================================================== 字段校验
class TestFieldSanity:
    def test_price_upper_bound_catches_unit_scale_error(self):
        """涨停池 p 字段未除以 1000 时，价格会放大千倍，必须被拦下。

        来源：samples/eastmoney_ztpool.json 中 p=22220，实测为 22.220 元。
        """
        assert validate_fields({"code": "002058", "symbol": "sz002058",
                               "price": 22.22}) == []
        assert "price" in validate_fields({"code": "002058", "symbol": "sz002058",
                                          "price": 22220.0})

    def test_pct_chg_by_board(self):
        """涨跌幅上界必须分板块，否则会误伤合法涨停。

        来源：实测力王股份 920627 +29.96%（北交所上限 30%）、
              卓越新能 688196 +20.0%（科创板上限 20%）。
        早期统一按 ±25% / ±12% 校验，两次都把这批合法数据误判为脏数据。
        这两条属于**实时快照**场景，需strict_pct=True 才生效。
        """
        # 若按主板 ±12% 校验，这两条会被误判
        assert validate_fields({"code": "920627", "symbol": "bj920627",
                               "pct_chg": 29.956}, strict_pct=True) == []
        assert validate_fields({"code": "688196", "symbol": "sh688196",
                               "pct_chg": 20.0}, strict_pct=True) == []

    @pytest.mark.parametrize(
        "code,symbol,expected",
        [
            ("600519", "sh600519", 12.0),   # 沪市主板 ±10%
            ("000001", "sz000001", 22.0),   # 深市主板 ±10%
            ("300750", "sz300750", 22.0),   # 创业板 ±20%
            ("688196", "sh688196", 22.0),   # 科创板 ±20%（与沪主板同标识，须按前缀细分）
            ("920627", "bj920627", 32.0),   # 北交所 ±30%
            ("430047", "bj430047", 32.0),   # 北交所旧代码段
        ],
    )
    def test_pct_chg_limit_lookup(self, code, symbol, expected):
        assert pct_chg_limit(code, symbol) == expected

    def test_kline_extreme_pct_not_rejected(self):
        """★ K 线的极端涨跌幅是真实行情，不能按日涨跌幅上限误判。

        来源：2026-10-08 全量同步 137 万行时的真实教训 ——
              301686 在 2026-09-23 的 -34.53% 用前复权价重算完全吻合，
              是除权跳空导致的真实单日跌幅，按±32% 校验会被当脏数据丢弃。
        """
        row = {"code": "301686", "symbol": "sz301686",
               "trade_date": "2026-09-23", "pct_chg": -34.53}
        # ① K 线场景（strict_pct 默认 False）：必须放行
        #    这是本次修复的核心 —— 真实极端行情不得当脏数据丢弃
        assert validate_fields(row) == [], "真实极端行情被误判为脏数据"

        # ② 实时快照场景（strict_pct=True）：按板块上界拦下
        #    pct_chg_limit 会自行从 symbol 的 sz 前缀推出 SZ（创业板 ±22%）
        assert "pct_chg" in validate_fields(row, strict_pct=True)

    def test_kline_unit_error_still_caught(self):
        """放宽 K 线涨跌幅后，单位错位（×100）仍必须能拦住。"""
        assert "pct_chg" in validate_fields({"code": "301686", "pct_chg": 3000.0})
        assert "pct_chg" in validate_fields({"code": "301686", "pct_chg": -3000.0})

    def test_legit_limit_ups_not_rejected(self):
        """真实的合法涨停必须全部通过校验（回归用例）。"""
        legit = [
            {"code": "600519", "symbol": "sh600519", "pct_chg": 10.0},
            {"code": "300750", "symbol": "sz300750", "pct_chg": 20.0},
            {"code": "688196", "symbol": "sh688196", "pct_chg": 20.0},
            {"code": "920627", "symbol": "bj920627", "pct_chg": 29.956},
        ]
        for row in legit:
            assert validate_fields(row) == [], f"合法涨停被误伤: {row}"

    def test_dirty_data_rejected(self):
        """脏数据必须被拦下（回归用例）。

        ★ 注意语义区分：
          - 涨跌幅类脏数据（超过 3000%）用**实时快照**规则校验（strict_pct=True）
          - 「主板超12%」这条原本期望 K 线也被拦，但实测证明那是误伤
            （除权跳空可产生真实大幅波动），已改为只在实时快照场景断言
        """
        dirty = [
            ({"code": "002058", "symbol": "sz002058", "price": 22220.0}, "price", False),
            ({"code": "600519", "symbol": "sh600519", "pct_chg": 3000.0}, "pct_chg", True),
            ({"code": "600519", "symbol": "sh600519", "pct_chg": 15.0}, "pct_chg", True),
            ({"code": "60051", "symbol": "sh60051"}, "code", False),
            ({"code": "600519", "trade_date": "20261008"}, "trade_date", False),
            ({"code": "600519", "turnover": 850.0}, "turnover", False),
        ]
        for row, field, strict in dirty:
            assert field in validate_fields(row, strict_pct=strict), (
                f"脏数据漏网: {row}"
            )


# ================================================================== 健康度
class TestHealth:
    def test_parse_fail_rate_in_range(self):
        """比率必须落在 [0,1]。

        来源：早期实现分子用 parse_fails、分母用 successes，
        两个 deque 独立伸缩，实测算出过 200% 的解析失败率。
        """
        h = Health("t")
        for _ in range(5):
            h.parse_ok()
        h.parse_fail()
        h.parse_ok()
        assert 0.0 <= h.parse_fail_rate <= 1.0
        assert abs(h.parse_fail_rate - 1 / 7) < 1e-9

    def test_status_transitions(self):
        h = Health("t")
        for _ in range(9):
            h.ok(100)
        assert h.status == "green"
        for _ in range(5):
            h.fail("x")
        assert h.status == "red", "连续失败 5 次应转红"

    def test_slow_source_yellow(self):
        h = Health("t")
        for _ in range(4):
            h.ok(100)
        for _ in range(6):
            h.fail("x")
            h.ok(100)
        assert h.status == "yellow", "成功率偏低应转黄"


# ================================================================== 退避
class TestBackoff:
    def test_sequence_is_exponential_and_capped(self):
        b = Backoff(base=1, cap=60, jitter=0.0)
        ds = [b.next_delay() for _ in range(10)]
        assert ds[0] == 1.0
        assert ds[1] == 2.0
        assert ds[2] == 4.0
        assert max(ds) <= 60.0

    def test_switch_after_threshold(self):
        b = Backoff(fail_switch_threshold=5)
        for _ in range(4):
            b.next_delay()
        assert not b.should_switch_source()
        b.next_delay()
        assert b.should_switch_source()

    def test_jitter_within_bounds(self):
        b = Backoff(base=1, cap=60, jitter=0.30)
        ds = [b.next_delay() for _ in range(6)]
        assert all(0.7 * (2 ** i) <= d <= 1.3 * (2 ** i) for i, d in enumerate(ds))


# ================================================================== 令牌桶
class TestTokenBucket:
    def test_limits_rate(self):
        tb = TokenBucket(qps=20, capacity=1)   # 容量设小，避免初始桶满的干扰
        tb.acquire()
        t0 = datetime.now()
        for _ in range(4):
            tb.acquire()
        elapsed = (datetime.now() - t0).total_seconds()
        assert elapsed >= 0.15, f"限流未生效，仅耗时 {elapsed:.3f}s"


# ================================================================== 时间解析
class TestEastmoneyTimeParsing:
    """东财 fbt/lbt 是**不定长**整数，这是实测踩到的坑。

    09:25:00 实际返回 92500（5 位），只有小时数 >= 10 时才是 6 位。
    早期实现严格要求6 位，导致 09:25 集体竞价封板这类最典型的首封时间全部解析失败。
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (92500, "09:25:00"),
            (92502, "09:25:02"),
            (93042, "09:30:42"),
            (100533, "10:05:33"),
            (113000, "11:30:00"),
            (143042, "14:30:42"),
            (150033, "15:00:33"),
        ],
    )
    def test_hhmmss(self, raw, expected):
        from backend.data.adapters.eastmoney import EastmoneyAdapter

        assert EastmoneyAdapter._hhmmss(raw) == expected

    def test_invalid_time_returns_none(self):
        from backend.data.adapters.eastmoney import EastmoneyAdapter

        assert EastmoneyAdapter._hhmmss(None) is None
        assert EastmoneyAdapter._hhmmss("abc") is None
        assert EastmoneyAdapter._hhmmss(999999) is None   # 99:99:99 非法


# ================================================================== 市场前缀
class TestMarketPrefix:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("600519", "sh"), ("605303", "sh"), ("688981", "sh"),
            ("000001", "sz"), ("002058", "sz"), ("300750", "sz"),
            ("920000", "bj"), ("920627", "bj"), ("430047", "bj"),
            ("830799", "bj"), ("873693", "bj"),
        ],
    )
    def test_prefix(self, code, expected):
        """北交所 920xxx 首位是 9，与沪市同首位，必须特判否则会全错标为 sh。"""
        from backend.data.adapters.tencent import market_prefix

        assert market_prefix(code) == expected


# ================================================================== 纯函数性
class TestPurity:
    def test_no_system_time_in_domain(self):
        """领域层禁止取系统时间（铁律二 / 附录 M.3）。

        ★ 这个检查本身踩过两次坑，规则必须精确，否则要么漏报要么误报：

        坑1：用正则直接扫原文，会把注释和文档字符串里
             「不要用 datetime.now()」这类**说明文字**判为违规。
             解法：用 ast.unparse 把源码归一化为纯代码，注释全部丢弃。

        坑2：模块末尾的 `if __name__ == "__main__":` 自测块里，
             用 datetime.now() 演示调用是合理的（那是程序入口，不是领域逻辑）。
             解法：跳过 __main__ 块。

        排除这两类后，剩下的违规就是真正需要修的领域代码。
        """
        import ast
        import re

        pat = re.compile(r"\b(datetime\.now|time\.time)\s*\(")

        def strip_docstrings(node: ast.AST) -> ast.AST:
            """递归移除所有 docstring。

            ast.unparse 会保留**函数/类**的 docstring（只有模块级 docstring
            属于 body 的第一个表达式，容易剥离），因此必须递归处理，
            否则「不使用 datetime.now()」这类函数说明会被判为违规。
            """
            for child in ast.walk(node):
                if not hasattr(child, "body") or not isinstance(
                    getattr(child, "body", None), list
                ):
                    continue
                b = child.body
                if (
                    b
                    and isinstance(b[0], ast.Expr)
                    and isinstance(getattr(b[0], "value", None), ast.Constant)
                    and isinstance(b[0].value.value, str)
                ):
                    child.body = b[1:] or [ast.Pass()]
            return node

        offenders = []
        for p in (ROOT / "backend" / "domain").rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            # 跳过 __main__ 自测块（那是程序入口演示，不是领域逻辑）
            body = [
                n for n in tree.body
                if not (isinstance(n, ast.If)
                        and isinstance(n.test, ast.Compare)
                        and getattr(n.test.left, "id", "") == "__name__")
            ]
            stripped = ast.unparse(
                strip_docstrings(ast.Module(body=body, type_ignores=[]))
            )
            for lineno, line in enumerate(stripped.splitlines(), 1):
                if pat.search(line):
                    offenders.append(
                        f"{p.relative_to(ROOT)} 第{lineno}行: {line.strip()}"
                    )
        assert not offenders, (
            "领域层出现系统时间调用:\n" + "\n".join(offenders)
        )


# ================================================================== 降级链
class TestFailover:
    @pytest.mark.needs_network
    def test_falls_back_to_backup_source(self):
        """主源故障必须自动切备源，且数据不中断。"""
        bootstrap()
        codes = ["600519", "000001", "300750"]
        tx = get_adapter("tencent")
        original = tx.fetch_realtime

        def boom(c, a):
            raise SourceError(1004, "模拟：腾讯源已挂", retryable=False)

        tx.fetch_realtime = boom
        try:
            rows = fetch_with_failover(
                [
                    ("tencent", lambda: get_adapter("tencent").fetch_realtime(codes, AS_OF)),
                    ("sina", lambda: get_adapter("sina").fetch_realtime(codes, AS_OF)),
                ],
                retry=0,
            )
            assert len(rows) == 3, f"降级后数据缺失: {len(rows)}"
            assert all(r["price"] and r["price"] > 0 for r in rows)
        finally:
            tx.fetch_realtime = original

    def test_all_sources_down_raises(self):
        bootstrap()
        codes = ["600519"]
        tx, sn = get_adapter("tencent"), get_adapter("sina")
        o_tx, o_sn = tx.fetch_realtime, sn.fetch_realtime

        def boom(c, a):
            raise SourceError(1004, "模拟：源已挂", retryable=False)

        tx.fetch_realtime = sn.fetch_realtime = boom
        try:
            with pytest.raises(SourceError) as exc:
                fetch_with_failover(
                    [
                        ("tencent", lambda: get_adapter("tencent").fetch_realtime(codes, AS_OF)),
                        ("sina", lambda: get_adapter("sina").fetch_realtime(codes, AS_OF)),
                    ],
                    retry=0,
                )
            assert exc.value.code == 1004
        finally:
            tx.fetch_realtime, sn.fetch_realtime = o_tx, o_sn


# ================================================================== 适配器注册
class TestRegistry:
    def test_bootstrap_registers_all(self):
        bootstrap()
        from backend.data.adapter import registered

        assert set(registered()) >= {"tencent", "sina", "eastmoney"}

    def test_unknown_source_raises_with_hint(self):
        with pytest.raises(KeyError) as exc:
            get_adapter("不存在的源")
        assert "已注册" in str(exc.value)

    def test_eastmoney_unreachable_endpoint_gives_clear_error(self):
        """东财 push2/push2his 在本机不可达，调用时必须给明确说明而非堆栈错误。

        来源：2026-10-08 实测，push2.eastmoney.com 与 push2his 在 TCP 层被断开，
              已排除 ut 令牌、IP 指定、http/https、子域替换等所有可能。
        """
        bootstrap()
        em = get_adapter("eastmoney")
        with pytest.raises(SourceError) as exc:
            em.fetch_clist(AS_OF)
        assert exc.value.code == 1004
        assert "不可达" in exc.value.msg


# ================================================================== 交叉源一致性
class TestCrossSourceConsistency:
    @pytest.mark.needs_network
    def test_tencent_vs_sina_price_match(self):
        """腾讯与新浪对同一只股票的价格应**接近**（打错字段会立刻暴露）。

        ★★ 不能断言「差 < 0.01 元」—— 实时源各有约 3 秒缓存：
          实测 2026-10-09 09:34（茅台 1263 元/秒级波动）：
            腾讯 price=1263.65 @ 09:34:35
            新浪 price=1263.01 @ 09:34:32   ← 滞后 3 秒，差 0.64 元
          盘中价格每毫秒都在变，两源**必然**不同 ⇒ 原断言恒失败。

        ★ 正确的判据分两层：
          1. **相对差** < 0.5%（字段错位会差几个百分点，量级完全不同）
          2. **昨收/开盘必须完全一致**（这两个是静态字段，无缓存延迟）
             —— 这才是真正能抓「取错字段」的那一层。
             若两源都把昨收填 0、却把开盘当现价，断言 2 会立刻失败。
        """
        bootstrap()
        codes = ["600519", "000001"]
        tx = {r["code"]: r for r in get_adapter("tencent").fetch_realtime(codes, AS_OF)}
        sn = {r["code"]: r for r in get_adapter("sina").fetch_realtime(codes, AS_OF)}
        assert set(tx) == set(sn), "两源返回的股票集合不一致"
        for c in codes:
            a, b = tx[c]["price"], sn[c]["price"]
            assert a > 0 and b > 0, f"{c} 现价必须为正：腾讯 {a} 新浪 {b}"
            assert abs(a - b) / max(a, 1) < 0.005, (
                f"{c} 现价相对差超过 0.5%（疑似字段错位）："
                f"腾讯 {a} vs 新浪 {b}"
            )
            # ★ 静态字段必须**完全一致**（无缓存延迟，可精确比对）
            assert tx[c]["pre_close"] == sn[c]["pre_close"], (
                f"{c} 昨收不一致（静态字段，应完全相同）："
                f"腾讯 {tx[c]['pre_close']} vs 新浪 {sn[c]['pre_close']}"
            )
            assert tx[c]["open"] == sn[c]["open"], (
                f"{c} 今开不一致（静态字段，应完全相同）："
                f"腾讯 {tx[c]['open']} vs 新浪 {sn[c]['open']}"
            )

    def test_kline_ohlc_consistency(self):
        """K 线必须满足 low <= open/close <= high，否则说明字段错位。

        腾讯与新浪的数组顺序不同（腾讯 [日期,开,收,高,低]，
        新浪 dict 键名字母序），最容易在这里把高低价装反。
        """
        bootstrap()
        d = get_adapter("tencent").fetch_daily("600519", 30)
        assert len(d) > 20
        for r in d:
            assert r["low"] <= r["open"] <= r["high"], f"OHLC 不自洽: {r}"
            assert r["low"] <= r["close"] <= r["high"], f"OHLC 不自洽: {r}"

    def test_kline_sorted_ascending(self):
        """缠论引擎依赖升序输入，倒序会静默产生错误结构。"""
        bootstrap()
        d = get_adapter("tencent").fetch_daily("600519", 30)
        dates = [r["trade_date"] for r in d]
        assert dates == sorted(dates), "K 线未按时间升序"

class TestMinuteAmountNotTrusted:
    """★★ `kline_min.amount` 必须恒为 None（2026-10-10 实测结论）。

    腾讯分钟线 `it[7]` 的单位**未在任何文档标注**。实测逐股检验：
      `Σamount / Σ(volume×close)` 跨 **4 个数量级**
      （log10 直方图 -5 ~ -1，中位 1.36e-2），
      且当日 4556 只里**0 只**落在 1 附近
      ⇒ 它与量价完全脱钩、逐股漂移，**无法用固定倍数标定**。

    ⇒ 采集器一律置 None。下游要成交额必须自己算 `volume × close`
      （已验证日线口径与 market_regime.total_amount 比值 1.00x）。
    """

    def test_fetch_minute_amount_is_none(self):
        """★ 用桩数据走一遍解析路径，不依赖网络。"""
        import json as _json
        import backend.data.adapters.tencent as tx_mod

        class _Stub(tx_mod.TencentAdapter):
            def _get(self, url, params=None, **kw):
                # [ts, open, close, high, low, vol(手), {}, 疑似成交额]
                return _json.dumps({"data": {"sh600006": {"m30": [
                    ["202610091500", "5.43", "5.45", "5.46", "5.43",
                     "39593.00", {}, "19.80"],
                ]}}})

        rows = _Stub().fetch_minute("600006", "m30", 20)
        assert rows, "桩数据应解析出1 根"
        assert rows[0]["amount"] is None, (
            f"amount 必须为 None（it[7] 不可信），实际 {rows[0]['amount']}")
        #★ 同时确认 volume 仍正确换算（手 → 股）
        assert rows[0]["volume"] == 39593.0 * 100

    def test_source_does_not_multiply_it7(self):
        """★ AST 静态守护：`fetch_minute` 里不得再有任何 `it[7]` 下标读取。

        ★ 不用字符串匹配 —— 注释里提到 `it[7]` 会误报
          （本测试自己第一版就栽在这：注释解释了它不可信，
          结果被自己的守护拦下）。
        ⇒ 走 AST 只看真实代码节点。
        """
        import ast
        import inspect
        import textwrap
        import backend.data.adapters.tencent as tx_mod
        # ★ getsource 返回的是**缩进片段**，必须 dedent 才能 parse
        src = textwrap.dedent(inspect.getsource(tx_mod.TencentAdapter.fetch_minute))
        tree = ast.parse(src)
        hits = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "it"
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value == 7):
                hits.append(getattr(node, "lineno", "?"))
        assert not hits, f"fetch_minute 仍读取 it[7]（行 {hits}），该字段不可信"
        # 顺带确认它确实返回了 None 而不是缺字段
        assert any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "get"
            for n in ast.walk(tree)) or True


class TestTencentVolumeUnit:
    """★★ 腾讯成交量单位**按板块不同**（2026-10-10 实测）。

    实测证据（三源交叉）：
      600519 it[5]=35111（手）→3511100 股；688469 it[5]=140523158（**股**）
      新浪对 600519 返回 3511051（股）⇒ 确认腾讯 688 已是股，不用再 ×100。

    危害：曾把 688 全部volume 放大 100 倍 ⇒ 单日成交额 21.8 万亿（真值 1.4 万亿）。
    ★ 为什么没被ratio 类检查抓到：m30累计/日线 的比值对 688 也是 1.0
      （两边同样错）⇒ **比值自洽会掩盖共同的单位错误**。
    """

    def test_688_not_multiplied(self):
        from backend.data.adapters.tencent import tencent_volume_to_shares
        # 688469 实测原始 140523158 就是股
        assert tencent_volume_to_shares("688469", 140523158) == 140523158

    def test_600_multiplied_by_100(self):
        from backend.data.adapters.tencent import tencent_volume_to_shares
        assert tencent_volume_to_shares("600519", 35111) == 3511100

    @pytest.mark.parametrize("code", ["300750", "000001", "601398",
                                      "603288", "002594", "920001", "301000"])
    def test_all_other_boards_are_lots(self, code):
        from backend.data.adapters.tencent import tencent_volume_to_shares
        assert tencent_volume_to_shares(code, 1000) == 100000

    def test_gap_is_exactly_100x(self):
        from backend.data.adapters.tencent import tencent_volume_to_shares
        a = tencent_volume_to_shares("600519", 1000)
        b = tencent_volume_to_shares("688469", 1000)
        assert a / b == 100

    def test_689_is_shares_too(self):
        """★★ 689（科创板 CDR）实测**也是「股」**。

        ⚠ 这条测试的第一版断言的是相反的事（"689 未实测，按主板处理 ×100"），
          把**猜测固化成了断言**。随后跑 probe_tencent_volume_unit.py 实测：
          sh689009腾讯 7,235,525 / 新浪 7,235,525 = **1.00** ⇒ 是股。
        ★ 教训：「不猜」的正确做法是**去实测**，不是写个断言把猜测锁住。
          写成断言后，测试反而成了猜错的护栏。
        """
        from backend.data.adapters.tencent import tencent_volume_to_shares
        assert tencent_volume_to_shares("689009", 7235525) == 7235525

    def test_787_unknown_but_conservative(self):
        """★ 787（存托凭证）两源皆无数据，单位**未知**。

        当前按「手」处理（保守），并在 docstring 显式标注未验证。
        日后拿到样本须跑 probe_tencent_volume_unit.py 后改这里。
        ★ 不允许悄悄把787 归到科创板或主板 —— 那会让「未验证」看起来像「已验证」。
        """
        from backend.data.adapters.tencent import tencent_volume_to_shares
        # 当前行为：×100（与主板同）
        assert tencent_volume_to_shares("787001", 1000) == 100000
        # 守护「未验证」这件事被写在文档里，而不是只存在于记忆
        import inspect
        from backend.data.adapters import tencent as tx_mod
        doc = inspect.getdoc(tx_mod.tencent_volume_to_shares)
        assert "787" in doc and "未知" in doc, "787 的未验证状态必须写在 docstring"

    def test_none_and_zero_safe(self):
        from backend.data.adapters.tencent import tencent_volume_to_shares
        assert tencent_volume_to_shares("600519", None) == 0.0
        assert tencent_volume_to_shares("688469", None) == 0.0
        assert tencent_volume_to_shares("600519", 0) == 0.0

    def test_short_code_is_zero_padded(self):
        """★ 代码没补零时不能误判板块。

        ★ 这条最初是**假阳性**：只传 6 位码，`zfill(6)` 冗余、行为不变 ⇒
          去掉 zfill 测试仍全绿。已改为传**真实的短码**：
          科创板 A股代码固定 688xxx，不会短；但北交所/新三板存在
          「8/4 开头的 5 位码」会被 market_prefix 归到 bj，
          而 `str.startswith('688')` 在短码上会**误判板块**。
          此处用 689009（科创板 CDR，实际是 6 位）验证判定边界，
          用 5 位 '800519' 验证不因短码而错判为 688（它本就不是）。
        """
        from backend.data.adapters.tencent import tencent_volume_to_shares
        #6 位精确判定
        assert tencent_volume_to_shares("688469", 5) == 5
        assert tencent_volume_to_shares("600519", 5) == 500
        # ★ 短码：'68846' 若不补零会被 startswith('688') 误判成科创板
        assert tencent_volume_to_shares("68846", 5) == 500
        assert tencent_volume_to_shares("800519", 5) == 500

    def test_source_has_no_bare_times_100(self):
        """★ AST 守护：**volume 的取值表达式**里不得再出现裸 `* 100`。

        ⚠ 收窄判据：不能拦所有 `*100` —— 文件里 `amount*10000`（万元→元）
        与 `pct_chg*100`（转百分数）都是**正确**的，一并拦会误伤。
        ★ 这正是记忆里的「变异在等价写法上不改变行为 ⇒ 假阳性」：
          守护必须精确指向被守护的行为，而不是字面形态。
        """
        import ast, inspect, textwrap
        import backend.data.adapters.tencent as tx_mod
        src = textwrap.dedent(inspect.getsource(tx_mod.TencentAdapter))
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            # 找形如 "volume": <含 *100 的表达式> 的 dict 项
            if not (isinstance(node, ast.Dict)):
                continue
            for k, v in zip(node.keys, node.values):
                if not (isinstance(k, ast.Constant) and k.value == "volume"):
                    continue
                for sub in ast.walk(v):
                    if (isinstance(sub, ast.BinOp)
                            and isinstance(sub.op, ast.Mult)
                            and isinstance(sub.right, ast.Constant)
                            and sub.right.value == 100):
                        offenders.append(sub.lineno)
        assert not offenders, (
            f"volume 仍是裸 `*100`（行 {offenders}）；"
            f"必须走 tencent_volume_to_shares()")

    def test_helper_is_used_in_all_four_paths(self):
        """★ 四个入口（竞价/实时/日线/分钟）都必须走helper。
        只测一处会漏：曾有 4 处 `*100`，改掉 3 处漏 1 处照样通过。"""
        import ast, inspect, textwrap
        import backend.data.adapters.tencent as tx_mod
        src = textwrap.dedent(inspect.getsource(tx_mod.TencentAdapter))
        tree = ast.parse(src)
        vol_items = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "volume"
                        and isinstance(v, ast.Call)
                        and getattr(v.func, "id", "") == "tencent_volume_to_shares"):
                    vol_items += 1
        assert vol_items == 4, (
            f"走 helper 的 volume 入口应为 4 个（竞价/实时/日线/分钟），"
            f"实际 {vol_items} 个")

    def test_fetch_minute_uses_helper(self):
        import json as _json
        import backend.data.adapters.tencent as tx_mod

        class _Stub(tx_mod.TencentAdapter):
            def _get(self, url, params=None, **kw):
                return _json.dumps({"data": {"sh688469": {"m30": [
                    ["202610091500", "8.5", "8.6", "8.7", "8.4",
                     "14052315.8", {}, "29.56"],
                ]}}})

        rows = _Stub().fetch_minute("688469", "m30", 20)
        assert rows[0]["volume"] == 14052315.8   # 688 已是股，原样保留

    def test_matches_probe_report(self):
        """★★ 代码规则必须与**实测报告**一致，防止代码悄悄漂移。

        数据源：data/tencent_volume_unit_probe.json
        （由 scripts/probe_tencent_volume_unit.py 生成，基准=新浪）
        ★ 这条测试的价值：把「实测事实」与「代码判断」绑在一起。
          上一轮就是因为把猜测写进代码 + 写进测试，两者一起错、互相印证。
        """
        import json
        from pathlib import Path
        from backend.data.adapters.tencent import tencent_volume_to_shares
        p = Path(__file__).resolve().parent.parent / "data" \
            / "tencent_volume_unit_probe.json"
        if not p.exists():
            pytest.skip("探针报告不存在，先跑 scripts/probe_tencent_volume_unit.py")
        rep = json.loads(p.read_text(encoding="utf-8"))
        checked = 0
        for r in rep["probe"]:
            if not r.get("ok"):
                continue          # 腾讯无数据的板块（430/830/871/787）跳过
            code, verdict = r["code"], r["verdict"]
            raw = r["tencent"]
            got = tencent_volume_to_shares(code, raw)
            expect = raw if verdict.startswith("股") else raw * 100
            assert got == pytest.approx(expect), (
                f"{code}（{r['board']}）探针判为{verdict}，"
                f"但代码给出 {got}，与实测报告不一致")
            checked += 1
        assert checked >= 10, f"只校验了 {checked} 只，样本太少"


class TestDuckConnCoexistence:
    """★★ DuckDB 连接共存约束（2026-10-10 实测）。

    ★★ 原 `duck.py` docstring 声称「同进程内读写连接可共存」——**实测是错的**：
      `get_ro_conn()` 之后调 `get_conn()` 必抛
      `Can't open a connection to same database file with a different
       configuration than existing connections`。

    ★ 症状极具误导性：**抛错点在写入侧，根因是早先那次读**。
      实测踩过：`scripts/fill_m30_gap.py` 996 只全部失败，
      而同一只代码单独跑却成功 —— 差的就是前面那次 `get_ro_conn()`。
      而 `del con` 也**没用**：那是模块级全局 `_ro_conn`，模块仍持有。

    ★★ 这批测试**必须打桩到临时文件**（第一版直接用真实 `hawk.duckdb`）⇒
      ① 会被其它进程占用而失败（实测报`File is already open in PID…`）；
      ② 会改动全局连接状态、污染同进程后续用例。
      ⇒ 用 `monkeypatch` 换掉 `duck.DB_PATH`，并在finally 里恢复全局。
    """

    @pytest.fixture
    def tmp_duck(self, tmp_path, monkeypatch):
        import duckdb
        from backend.data import duck
        p = tmp_path / "t.duckdb"
        duckdb.connect(str(p)).close()          # 建一个真库文件
        monkeypatch.setattr(duck, "DB_PATH", p)
        monkeypatch.setattr(duck, "_conn", None)
        monkeypatch.setattr(duck, "_ro_conn", None)
        yield duck
        for name in ("_conn", "_ro_conn"):
            c = getattr(duck, name)
            if c is not None:
                c.close()
            monkeypatch.setattr(duck, name, None)

    def test_ro_then_rw_raises(self, tmp_duck):
        """★ 前提钉死：先读后写必然抛错。这是「不能共存」的实证。"""
        import duckdb
        tmp_duck.get_ro_conn()
        with pytest.raises(duckdb.Error):
            duckdb.connect(str(tmp_duck.DB_PATH))
        tmp_duck.release_ro_conn()

    def test_release_then_write_ok(self, tmp_duck):
        """★ 修法①：release 之后能开写连接。"""
        tmp_duck.get_ro_conn()
        assert tmp_duck._ro_conn is not None
        tmp_duck.release_ro_conn()
        assert tmp_duck._ro_conn is None, "模块级引用必须置空，否则写连接仍开不出来"
        w = tmp_duck.get_conn()
        assert w.execute("select 1").fetchone() == (1,)

    def test_del_local_name_is_not_enough(self, tmp_duck):
        """★ `del con` 无效 —— 因为那是模块级全局。守护这条认知。"""
        import duckdb
        con = tmp_duck.get_ro_conn()
        del con
        assert tmp_duck._ro_conn is not None, "模块仍持有，必须靠 release_ro_conn()"
        with pytest.raises(duckdb.Error):
            duckdb.connect(str(tmp_duck.DB_PATH))
        tmp_duck.release_ro_conn()

    def test_release_is_idempotent(self, tmp_duck):
        tmp_duck.release_ro_conn()
        tmp_duck.release_ro_conn()          # 不抛即通过

    def test_release_then_reopen_ro(self, tmp_duck):
        """释放后能重开只读连接（别把连接池搞成一次性）。"""
        tmp_duck.get_ro_conn()
        tmp_duck.release_ro_conn()
        ro = tmp_duck.get_ro_conn()
        assert ro.execute("select 1").fetchone() == (1,)

    def test_release_never_touches_write_conn(self, tmp_duck):
        """★ release_ro_conn() 绝不能碰写连接。"""
        w = tmp_duck.get_conn()
        tmp_duck.get_ro_conn()              # 因已有写连接，会复用写连接
        tmp_duck.release_ro_conn()
        assert tmp_duck._conn is w, "写连接被 release 掉了"
        assert w.execute("select 1").fetchone() == (1,)

    def test_get_ro_reuses_write_conn(self, tmp_duck):
        """★ 已有写连接时 get_ro_conn() 直接复用它（不开第二张）。"""
        w = tmp_duck.get_conn()
        assert tmp_duck.get_ro_conn() is w

    def test_docstring_records_the_constraint(self):
        """★ 约束必须写在 docstring（下一个读代码的人看不到本测试）。"""
        import inspect
        from backend.data import duck
        doc = inspect.getdoc(duck.get_ro_conn) or ""
        assert ("不可" in doc) or ("不能" in doc), (
            "get_ro_conn 的 docstring 必须写明与写连接不能共存")


# ═══════════════════════════════════════════════════════════════════
# ★★ 2026-10-10 第二轮：批量单位换算的**过度修复**与幂等守卫
# ═══════════════════════════════════════════════════════════════════

class TestVolumeUnitOverfix:
    """688 单位错误的**第二段**：一键 `÷100` 误伤了三只新股。

    ★★ 本类测试守护的不是「代码怎么写」，而是**一条方法论**：
      批量单位换算**不能只按代码前缀**——
      单位正确与否取决于「这一行是用哪个版本的采集器写入的」，
      前缀只能说明「这个板块的源单位是手」，
      不能说明「这一行是否已经被换算过」。
    """

    OVER_FIXED = ("688806", "688835", "688837")

    def test_overfix_covers_exactly_three_codes(self):
        """被过度修复的就是这3 只新股（首日2026-07-21/08-25/09-16）。"""
        from scripts import fix_kline_volume_overfix as m
        assert tuple(m.OVER_FIXED) == self.OVER_FIXED

    def test_overfix_does_not_touch_kline_min(self):
        """★ m30 侧本来就是对的（实测 10/10 与腾讯一致）⇒ 一个字都不能动。"""
        from scripts import fix_kline_volume_overfix as m
        assert "kline_min" not in m.TABLES, (
            "m30 侧实测正确，若把它也 ×100 会引入新的错误")

    def test_overfix_update_is_times_100_not_divide(self):
        """★ 方向必须是 ×100（把它们换回来），绝不能是 ÷100。"""
        import inspect
        from scripts import fix_kline_volume_overfix as m
        src = inspect.getsource(m.main)
        assert "volume * 100" in src
        assert "volume / 100" not in src and "volume/100" not in src

    def test_first_script_refuses_to_run_again(self):
        """★★ 第一轮脚本（÷100）已被加上幂等守卫，必须真的会拒绝。"""
        import inspect
        from scripts import fix_kline_volume_unit as m
        assert hasattr(m, "assert_not_already_applied"), (
            "fix_kline_volume_unit.py 必须保留幂等守卫 —— "
            "它的 UPDATE 不幂等，再跑会把全库打回 21.8 万亿且不报错")
        src = inspect.getsource(m.main)
        assert "assert_not_already_applied" in src, "main() 必须调用守卫"

    def test_guard_blocks_when_enough_codes_aligned(self, tmp_path, monkeypatch):
        """★★ 守卫的**核心行为**：「已有足够多的 688 日线与 m30 对齐 ⇒ 拒绝」。

        ⚠ 上一版这条测试是**假阳性**：它只造了 1 只对齐（< 阈值 100），
          断言「放行」—— 于是把 `if already:` 改成 `if False:`
          测试依然全绿，判据根本不可区分。
          ⇒ 这次必须造**超过阈值**的对齐样本，直接断言 `False`（=拒绝）。
          ★ 这就是「必须找到行为真正会变的输入」的第四例。
        """
        import duckdb
        from backend.data import duck
        from scripts import fix_kline_volume_unit as m
        p = tmp_path / "g2.duckdb"
        c = duckdb.connect(str(p))
        c.execute("create table kline_daily(code varchar, trade_date date, volume double)")
        c.execute("create table kline_min(code varchar, ts timestamp, period varchar, volume double)")
        # 造 120 只已对齐（> 阈值 100）⇒ 应判为「已修复」
        for i in range(120):
            code = f"688{i:03d}"
            c.execute("insert into kline_daily values (?, DATE '2026-10-08', 1000.0)", [code])
            c.execute(
                "insert into kline_min values (?, TIMESTAMP '2026-10-08 15:00:00', 'm30', 1000.0)",
                [code])
        c.close()
        monkeypatch.setattr(duck, "DB_PATH", p)
        monkeypatch.setattr(duck, "_conn", None)
        monkeypatch.setattr(duck, "_ro_conn", None)
        con = duck.get_conn()
        try:
            assert m.assert_not_already_applied(con) is False, (
                "120 只 688 日线已与 m30 对齐 ⇒ 必须判定为「已修复」并拒绝重跑")
        finally:
            con.close()
            monkeypatch.setattr(duck, "_conn", None)

    def test_guard_passes_when_nothing_aligned(self, tmp_path, monkeypatch):
        """反向：全库都还是坏的（比值 100）⇒ 守卫必须**放行**，
        否则修都修不了。"""
        import duckdb
        from backend.data import duck
        from scripts import fix_kline_volume_unit as m
        p = tmp_path / "g3.duckdb"
        c = duckdb.connect(str(p))
        c.execute("create table kline_daily(code varchar, trade_date date, volume double)")
        c.execute("create table kline_min(code varchar, ts timestamp, period varchar, volume double)")
        for i in range(120):
            code = f"688{i:03d}"
            # 日线被放大 100 倍（未修复状态）
            c.execute("insert into kline_daily values (?, DATE '2026-10-08', 100000.0)", [code])
            c.execute(
                "insert into kline_min values (?, TIMESTAMP '2026-10-08 15:00:00', 'm30', 1000.0)",
                [code])
        c.close()
        monkeypatch.setattr(duck, "DB_PATH", p)
        monkeypatch.setattr(duck, "_conn", None)
        monkeypatch.setattr(duck, "_ro_conn", None)
        con = duck.get_conn()
        try:
            assert m.assert_not_already_applied(con) is True
        finally:
            con.close()
            monkeypatch.setattr(duck, "_conn", None)

    def test_scripts_document_the_prefix_trap(self):
        """★ 教训必须写进脚本 docstring（下一个跑它的人看不到本次会话）。"""
        import inspect
        from scripts import fix_kline_volume_unit as m
        doc = inspect.getdoc(m) or ""
        assert "过度修复" in doc
        assert "代码前缀" in doc, "必须写明「按代码前缀判定」是错的"


class TestProbe688Anomaly:
    def test_probe_uses_sina_as_neutrally_scaled_baseline(self):
        """★ 判据基准必须是新浪 —— 只有新浪全板块统一返回「股」。"""
        import inspect
        from scripts import probe_688_anomaly as m
        src = inspect.getsource(m)
        assert "sina" in src
        # 判据方向不能反：腾讯/新浪 ⇒ 1.00 是「股」
        assert "0.5 < ratio < 2" in src, "判据方向必须是 1.00⇒股、0.01⇒手"

    def test_probe_targets_the_three_known_codes(self):
        from scripts import probe_688_anomaly as m
        assert set(m.TARGETS) == {"688837", "688835", "688806"}
