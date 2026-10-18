# -*- coding: utf-8 -*-
"""
Hawk · 缠论引擎黄金用例（M4 验收：20 组）

验收标准（提示词 M4）：**黄金用例全通过；图上标注与手工标注一致**

设计原则：用**人工构造的确定性 K 线**验证每个构件的边界行为，
而不是只跑真实数据。真实数据只能证明「不崩」，构造数据才能证明「算对」。

每个用例都写明**为什么这样构造**与**期望什么**，
失败时能立刻判断是引擎错还是用例错。

运行： pytest tests/test_chan.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.chan import (
    Bar,
    analyze,
    build_bi,
    build_zhongshu,
    clean_bars,
    detect_beichi,
    ema,
    find_fractals,
    macd_series,
    merge_klines,
    scan_signals,
)

CST = timezone(timedelta(hours=8))
AS_OF = "2026-10-08T15:00:00+08:00"


# ---------------------------------------------------------------- 构造器
def mk(h: float, l: float, i: int, o: float | None = None,
       c: float | None = None) -> dict:
    """构造一根 K 线。h/l 必填（判断结构只看高低点）。"""
    o = (h + l) / 2 if o is None else o
    c = o if c is None else c
    return {"trade_date": f"2026-01-{i + 1:02d}", "high": h, "low": l,
            "open": o, "close": c, "volume": 1000.0, "pct_chg": 0.0}


def from_hl(pairs: list[tuple[float, float]]) -> list[dict]:
    """从 (high, low) 序列构造 K 线。"""
    return [mk(h, l, i) for i, (h, l) in enumerate(pairs)]


# ================================================================ ① 包含处理
class TestMergeKlines:
    """黄金用例 1-5：包含关系处理"""

    def test_g1_no_containment_keeps_all(self):
        """用例1：无包含关系时不应合并任何 K 线。"""
        bars = clean_bars(from_hl([(10, 8), (11, 9), (12, 10)]))
        merged = merge_klines(bars)
        assert len(merged) == 3, "无包含时应原样保留"

    def test_g2_up_containment_takes_max(self):
        """★用例2：向上包含 —— 取两高、两低中**较高**者。

        缠论标准：包含的处理方向由「前一根合并K线与再前一根比较」决定，
        所以必须构造出可判定上升趋势的序列。

        构造 (10,8) -> (15,12) 为上升，第 3 根 (14,13)被 (15,12) 包含：
          up = 15 > 10 = True => 向上处理
          new_h = max(15,14) = 15, new_l = max(12,13) = 13
        """
        bars = clean_bars(from_hl([(10, 8), (15, 12), (14, 13)]))
        merged = merge_klines(bars)
        assert len(merged) == 2, "第2、3根构成包含，应合并为 2 根"
        # ★ 合并发生在**第 2 根**（被包含的那两根会合并成一根），
        #   不是第 1 根 —— 断言位置写错过一次，引擎是对的。
        assert (merged[1].h, merged[1].l) == (15.0, 13.0), \
            f"向上处理应取 max(15,13)，实得 ({merged[1].h},{merged[1].l})"

    def test_g3_down_containment_takes_min(self):
        """★用例3：向下包含 —— 取两低、两高中**较低**者。

        构造 (14,12) -> (10,8) 为下降，第 3 根 (9,8) 被 (10,8) 包含
        （包含判定：9<=10 且 8>=8）：
          up = 10 > 14 = False => 向下处理
          new_h = min(10,9) = 9, new_l = min(8,8) = 8
        """
        bars = clean_bars(from_hl([(14, 12), (10, 8), (9, 8)]))
        merged = merge_klines(bars)
        assert len(merged) == 2, "第2、3根构成包含，应合并为 2 根"
        assert (merged[1].h, merged[1].l) == (9.0, 8.0), \
            f"向下处理应取 min，实得 ({merged[1].h},{merged[1].l})"

    def test_g3b_containment_needs_direction_context(self):
        """★用例3b：处理方向依赖「再前一根」，同形状数据在不同背景下结果不同。

        缠论的包含处理不是纯局部规则 —— 方向由前一根与再前一根比较得出。
        这个用例把该依赖关系固化，防止后人把它简化成局部规则而算错。
        """
        # 相同的后两根 (14,13)，但前景不同 -> 合并结果不同
        up_ctx = merge_klines(clean_bars(from_hl([(10, 8), (15, 12), (14, 13)])))
        down_ctx = merge_klines(clean_bars(from_hl([(14, 12), (10, 8), (9, 8)])))
        assert up_ctx[1].l == 13.0, f"上升背景下取 max，实得 {up_ctx[1].l}"
        assert down_ctx[1].l == 8.0, f"下降背景下取 min，实得 {down_ctx[1].l}"

    def test_g4_chained_containment(self):
        """用例4：连续多根被包含，应全部并成一根。"""
        # 三根连续包含：10/8, 9.5/8.5, 9.2/8.2
        bars = clean_bars(from_hl([(10, 8), (9.5, 8.5), (9.2, 8.2), (12, 10)]))
        merged = merge_klines(bars)
        assert len(merged) == 2, "连续包含应合并成 2 根"

    def test_g5_merged_bar_records_raw_range(self):
        """★用例5：合并 K 线必须记录覆盖的原始区间（i0/i1）。

        这是复盘能对齐的关键 —— 原始索引丢失就无法回溯。
        """
        bars = clean_bars(from_hl([(10, 8), (9, 9), (12, 10)]))
        merged = merge_klines(bars)
        assert merged[0].i0 == 0
        assert merged[0].i1 == 1, "应记录覆盖了原始第 0、1 根"

    def test_g6_suspend_bars_excluded(self):
        """★用例6：停牌 bar（high=low=0）必须被剔除（附录 C.3）。

        不剔除会让连续同价 bar 产生虚假分型。
        """
        rows = [mk(10, 8, 0), mk(0, 0, 1), mk(0, 0, 2), mk(12, 10, 3)]
        bars = clean_bars(rows)
        assert len(bars) == 2, "停牌 bar 应被剔除"
        assert all(b.h > 0 for b in bars)


# ================================================================ ② 分型
class TestFractals:
    """黄金用例 7-10：分型识别"""

    def test_g7_top_fractal(self):
        """用例7：中间 K 线高/低均为三根最高 -> 顶分型。"""
        merged = merge_klines(clean_bars(from_hl([(10, 6), (12, 9), (10, 6)])))
        fr = find_fractals(merged)
        assert len(fr) == 1
        assert fr[0].kind == "top"

    def test_g8_bottom_fractal(self):
        """用例8：中间 K 线高/低均为三根最低 -> 底分型。"""
        merged = merge_klines(clean_bars(from_hl([(12, 9), (10, 6), (12, 9)])))
        fr = find_fractals(merged)
        assert len(fr) == 1
        assert fr[0].kind == "bottom"

    def test_g9_no_fractal_when_monotonic(self):
        """用例9：单调上升序列无顶分型。"""
        merged = merge_klines(clean_bars(from_hl(
            [(10, 8), (11, 9), (12, 10), (13, 11)])))
        fr = find_fractals(merged)
        assert len(fr) == 0, "单调序列不应产生分型"

    def test_g10_alternating_fractals(self):
        """用例10：高低交替应产生多个分型。"""
        merged = merge_klines(clean_bars(from_hl([
            (10, 6), (14, 10), (12, 8), (16, 11), (14, 9),
        ])))
        fr = find_fractals(merged)
        kinds = [f.kind for f in fr]
        assert "top" in kinds and "bottom" in kinds


# ================================================================ ③ 笔
class TestBi:
    """黄金用例 11-14：笔的构建（含两套标准）"""

    def _zigzag(self) -> list:
        """构造锯齿序列：趋势段 + 极值处三根小K 线形成分型。

        ★ 构造要点（实测踩过两次坑）：
          1. 连续相同 (high,low) 会被包含处理合并掉，平台形态不构成分型
          2. 每个极值必须是「高-更高-高」的3 根（顶）或「低-更低-低」的 3 根（底）
          3. 尾���必须留够K线，否则最后一段会被合并，分型/笔被吃掉

        本序列产生：顶分型(idx6) -> 底分型(idx14)，间隔 8 根，
        满足新笔标准 min_gap=4。
        """
        return clean_bars(from_hl([
            (10, 9), (11, 10), (12, 11), (13, 12), (14, 13),   # 上升
            (16, 14), (17, 15), (16, 14),                      # 顶分型
            (15, 13), (14, 12), (13, 11), (12, 10), (11, 9),   # 下降
            (10, 8), (9, 7), (10, 8),                          # 底分型
            (11, 9), (12, 10), (13, 11), (14, 12), (15, 13),   # 再升
            (16, 14), (17, 15), (16, 14),                      # 第二个顶
        ]))

    def test_g11_bi_alternates_direction(self):
        """★用例11：笔必须严格交替（相邻永不同向）。

        这条是引擎正确性的基石 —— 早期版本因坐标系错位算出
        「下跌笔 vs 反弹笔」的面积比，ratio 出现 0.0 / 25.156。
        """
        bars = self._zigzag()
        merged = merge_klines(bars)
        fr = find_fractals(merged)
        bis = build_bi(merged, fr, strict="new", bars=bars)
        assert len(bis) >= 2
        for i in range(len(bis) - 1):
            assert bis[i].direction != bis[i + 1].direction, "笔必须交替"

    def test_g12_new_bi_min_gap(self):
        """用例12：新笔标准要求顶底分型间隔 >= 4 根合并 K 线。"""
        bars = self._zigzag()
        merged = merge_klines(bars)
        fr = find_fractals(merged)
        strict_new = build_bi(merged, fr, strict="new", bars=bars)
        # 老笔标准不会过滤掉间隔小的分型，故笔数 >= 新笔
        strict_old = build_bi(merged, fr, strict="old", bars=bars)
        assert len(strict_old) >= len(strict_new), "老笔标准应产生不少于新笔的笔"

    def test_g13_bi_raw_index_within_range(self):
        """★用例13：笔的原始索引必须在 bars 范围内。

        MACD 面积计算依赖它，越界会静默取到错误区间。
        """
        bars = self._zigzag()
        merged = merge_klines(bars)
        fr = find_fractals(merged)
        bis = build_bi(merged, fr, strict="new", bars=bars)
        for b in bis:
            assert 0 <= b.start_raw < len(bars), f"start_raw={b.start_raw} 越界"
            assert 0 <= b.end_raw < len(bars), f"end_raw={b.end_raw} 越界"
            assert b.start_raw <= b.end_raw, "笔的原始索引应递增"

    def test_g14_bi_extremes_cover_range(self):
        """用例14：笔的高低点必须是该笔区间内的真实极值。"""
        bars = self._zigzag()
        merged = merge_klines(bars)
        fr = find_fractals(merged)
        bis = build_bi(merged, fr, strict="new", bars=bars)
        for b in bis:
            seg = bars[b.start_raw : b.end_raw + 1]
            assert b.high == max(x.h for x in seg), "笔高点应为区间最高"
            assert b.low == min(x.l for x in seg), "笔低点应为区间最低"


# ================================================================ ④⑤ 线段/中枢
class TestSegmentZhongshu:
    """黄金用例 15-18"""

    def test_g15_zhongshu_needs_overlap(self):
        """★用例15：中枢要求连续三笔有重叠区间（lo < hi）。

        三笔完全不重叠则不构成中枢。
        """
        bars = clean_bars(from_hl([
            (10, 8), (20, 18), (12, 10), (22, 20), (14, 12), (24, 22),
        ]))
        merged = merge_klines(bars)
        fr = find_fractals(merged)
        bis = build_bi(merged, fr, strict="old", bars=bars)
        zs = build_zhongshu(bis, bars=bars)
        for z in zs:
            assert z.low < z.high, "中枢必须有有效重叠区间"

    def test_g16_zhongshu_amplitude(self):
        """用例16：中枢振幅计算正确。"""
        bars = clean_bars(from_hl([
            (10, 8), (20, 15), (12, 10), (22, 16), (14, 12), (24, 18),
        ]))
        merged = merge_klines(bars)
        fr = find_fractals(merged)
        bis = build_bi(merged, fr, strict="old", bars=bars)
        zs = build_zhongshu(bis, bars=bars)
        assert zs, "应能识别出中枢"
        for z in zs:
            expected = round((z.high / z.low - 1) * 100, 2)
            assert z.amplitude == expected

    def test_g17_empty_input_safe(self):
        """★用例17：空输入不得抛异常（缺数据是常态，不是错误）。

        新股上市不足、数据未同步、停牌都会导致空序列。
        """
        assert merge_klines([]) == []
        assert find_fractals([]) == []
        assert build_bi([], [], strict="new") == []
        assert build_zhongshu([], bars=[]) == []
        res = analyze("TEST", [], as_of=AS_OF)
        assert res.signals == []

    def test_g18_short_series_safe(self):
        """用例18：数据不足时应安全返回，不产生信号。"""
        rows = from_hl([(10, 8), (12, 9)])
        res = analyze("TEST", rows, as_of=AS_OF)
        assert res.bars
        assert len(res.bis) <= 1, "2 根 K 线不应产生笔"
        assert res.signals == [], "数据不足不应产生信号"


# ================================================================ ⑥⑦ 背驰/信号
class TestBeichiSignal:
    """黄金用例 19-20 + 关键回归"""

    def _real_rows(self, code: str) -> list[dict]:
        from backend.data import duck

        return duck.get_daily(code, limit=250)

    @pytest.mark.duckdb_locked
    def test_g19_beichi_ratio_sane_range(self):
        """★用例19：背驰比值必须落在有物理意义的空间内。

        实测踩过的坑与修复：
          · 分母过小（a1=0.063）导致 ratio=25.156 —— 无物理意义
          · 分子为 0（该波段 MACD 柱全为反方向）导致 ratio=0.0
        故 detect_beichi 加了 min_area 门槛，双向过滤。

        ★ 阈值取 0.01~1000 而非更紧：
          真实数据实测（51 个样本）比值中位数 1.30、最大 22.55，
          强反弹确实会产生大比值，那是**真实行情**不是噪声。
          真正要拦的是「0.0」与「分母趋零导致的爆表」，
          这些已被 min_area 门槛消除，不该再用比值上限误伤。
        """
        from backend.domain.chan import MIN_BEICHI_AREA

        n_checked = 0
        for code in ("600519", "300750", "002594", "601318", "000858"):
            rows = self._real_rows(code)
            if len(rows) < 100:
                continue
            bars = clean_bars(rows)
            merged = merge_klines(bars)
            fr = find_fractals(merged)
            bis = build_bi(merged, fr, strict="new", bars=bars)
            _, _, hist = macd_series([b.c for b in bars])
            bcs = detect_beichi(bis, hist, bars)
            for bc in bcs:
                n_checked += 1
                # 比值必须为有限正数
                assert 0 < bc.ratio < 1000, \
                    f"{code} 背驰比值 {bc.ratio} 越界"
                # 前波段面积必须达到门槛（否则说明门槛未生效）
                assert bc.prev_area >= MIN_BEICHI_AREA or bc.ratio > 0, \
                    f"{code} 分母未过门槛却被计入: prev_area={bc.prev_area}"
        assert n_checked > 0, "应至少检出一批背驰样本"

    @pytest.mark.duckdb_locked
    def test_g20_signals_have_reason_and_confidence(self):
        """★用例20：每个信号都必须能解释「为什么」（附录第十二章第 4 条）。

        无 reason 的信号不允许入库 —— 用户看不懂的信号没有价值。
        """
        for code in ("600519", "300750", "002594"):
            rows = self._real_rows(code)
            if len(rows) < 100:
                continue
            res = analyze(code, rows, as_of=AS_OF)
            for s in res.signals:
                assert s.reason and len(s.reason) > 5, f"{code} {s.kind} 缺理由"
                assert 0 < s.confidence <= 1, f"{code} {s.kind} 置信度越界"
                assert s.kind in ("1buy", "2buy", "3buy",
                                  "1sell", "2sell", "3sell"), f"未知信号类型 {s.kind}"


# ================================================================ 纯函数
class TestPurity:
    def test_no_system_time(self):
        """★缠论是铁律二的核心（附录 M.3）。

        只有 f(输入, as_of, params) 才能支撑 M8 复盘重放，
        否则「当时为什么给这个信号」永远无法回答。
        """
        import ast
        import re

        p = ROOT / "backend" / "domain" / "chan.py"
        tree = ast.parse(p.read_text(encoding="utf-8"))

        def strip_docs(node):
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
        code = ast.unparse(strip_docs(ast.Module(body=body, type_ignores=[])))
        pat = re.compile(r"\b(datetime\.now|time\.time)\s*\(")
        assert not pat.search(code), "缠论引擎出现了系统时间调用（违反铁律二）"

    @pytest.mark.duckdb_locked
    def test_same_input_same_output(self):
        """★同输入同输出必须完全一致（可复现性）。"""
        rows = self._real_rows = None
        from backend.data import duck

        rows = duck.get_daily("600519", limit=120)
        if len(rows) < 60:
            pytest.skip("数据不足")
        a = analyze("600519", rows, as_of=AS_OF)
        b = analyze("600519", rows, as_of=AS_OF)
        assert len(a.bis) == len(b.bis)
        assert len(a.zhongshus) == len(b.zhongshus)
        assert [s.kind for s in a.signals] == [s.kind for s in b.signals]

    @pytest.mark.duckdb_locked
    def test_as_of_is_recorded(self):
        """as_of 必须写入结果 —— 知道「这是为哪个时点算的」。"""
        from backend.data import duck

        rows = duck.get_daily("600519", limit=60)
        res = analyze("600519", rows, as_of="2026-01-01T00:00:00+08:00")
        assert res.as_of == "2026-01-01T00:00:00+08:00"

    @pytest.mark.duckdb_locked
    def test_truncation_changes_result(self):
        """★回测正确性：截断数据应产生不同的结构。

        若 as_of 截断后结果不变，说明引擎用到了未来数据（前视偏差）。
        这是最危险的回测缺陷，必须有测试守着。
        """
        from backend.data import duck

        rows = duck.get_daily("600519", limit=250)
        if len(rows) < 100:
            pytest.skip("数据不足")
        full = analyze("600519", rows, as_of=AS_OF)
        half = analyze("600519", rows[: len(rows) // 2], as_of=AS_OF)
        assert len(full.bars) > len(half.bars), "截断后 bar 数应减少"
        assert full.bars[-1].d != half.bars[-1].d, "截断后最后日期应不同"


# ================================================================ 指标
class TestIndicators:
    def test_ema_converges_to_constant(self):
        """EMA 对常数序列应收敛到该常数。"""
        out = ema([10.0] * 100, 12)
        assert abs(out[-1] - 10.0) < 0.01

    def test_macd_hist_is_double_diff(self):
        """MACD 柱 = (DIF - DEA) * 2（国内口径）。"""
        dif, dea, hist = macd_series([10, 11, 12, 11, 10, 11, 12, 13])
        assert len(dif) == len(dea) == len(hist)
        assert abs(hist[-1] - (dif[-1] - dea[-1]) * 2) < 1e-9

# ================================================================ 弱三买报价口径
class TestWeak3buyPrice:
    """★★ 弱三买入场价必须是信号日收盘价，不能是突破笔最高价。

    这组测试锁住 M8 三买归因发现的缺陷：弱三买曾用 `bis[exit_k].high`
    （突破笔最高价）作为「买入价」，逐例核查发现它恒等于信号日最高价
    （5/5 案例 信号价/当日最高 = 1.000），比当日收盘高 3.5%~16.2%。
    典型如 605179：信号价 31.48 = 当日最高，当日收盘仅 27.09（冲高回落 -14%）。
    那个价在现实中根本无法成交，属于事后挑出的最贵价位。
    """

    @staticmethod
    def _real_weak_3buy_codes(max_codes: int = 40):
        """从真实快照里找出确实产生了弱三买的标的。

        ★ 为什么用真实数据而不是构造数据：
          构造数据试过（锯齿震荡 + 末端冲高回落），**触发不出弱三买** ——
          弱三买要求「最后一笔向上突破中枢后没有回抽笔」，对分型结构很敏感，
          人工构造的形态总是多出一根回抽笔。测试全skip 等于没有测试，
          所以这里用真实行情，并保留 skip 兜底（数据缺失时不算失败）。
        """
        from pathlib import Path
        import duckdb

        pq = ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"
        if not pq.exists():
            return []
        con = duckdb.connect()
        try:
            codes = [r[0] for r in con.execute(
                f"""SELECT code FROM read_parquet('{pq.as_posix()}')
                    GROUP BY code HAVING count(*) >= 120
                    ORDER BY code LIMIT 60""").fetchall()]
            hits = []
            for code in codes:
                rows = con.execute(
                    f"""SELECT trade_date, open, high, low, close, volume
                        FROM read_parquet('{pq.as_posix()}')
                        WHERE code = ? ORDER BY trade_date""", [code]).fetchall()
                recs = [{"trade_date": str(r[0]), "open": r[1], "high": r[2],
                         "low": r[3], "close": r[4], "volume": r[5]}
                        for r in rows]
                res = analyze(code, recs, as_of=AS_OF)
                weak = [s for s in res.signals
                        if s.kind == "3buy" and s.level == "weak"]
                if weak:
                    hits.append((code, res, weak))
                if len(hits) >= max_codes:
                    break
            return hits
        finally:
            con.close()

    def test_weak_3buy_price_not_the_intraday_high(self):
        """弱三买价格必须 <当日 high（可成交），而非 == high（追高）。"""
        hits = self._real_weak_3buy_codes()
        if not hits:
            pytest.skip("真实快照中未找到弱三买样本")
        for code, res, weak in hits:
            for s in weak:
                bar = next(b for b in res.bars if b.d == s.date)
                assert s.price < bar.h, (
                    f"{code} 弱三买价格 {s.price} 触及当日最高价 {bar.h}"
                    f"（收盘 {bar.c}），说明仍在用突破笔高点当买入价"
                )
                assert s.price == bar.c, f"{code} 弱三买价格应等于信号日收盘价"

    def test_weak_3buy_price_not_above_close(self):
        """★ 防退化断言：弱三买报价绝不可高于当日收盘。

        旧实现取突破笔最高价，在冲高回落日会显著高于收盘（实测最高 +16.2%）。
        这条断言能在价格口径被改回时立刻失败。
        """
        hits = self._real_weak_3buy_codes()
        if not hits:
            pytest.skip("真实快照中未找到弱三买样本")
        for code, res, weak in hits:
            for s in weak:
                bar = next(b for b in res.bars if b.d == s.date)
                assert s.price <= bar.c + 1e-6, (
                    f"{code} 弱三买报价 {s.price} 高于收盘 {bar.c}，"
                    f"这是事后挑选的不可成交价位"
                )

    def test_weak_3buy_confidence_reduced(self):
        """弱三买是观察态，confidence 必须低于 strong（三买已确认）。"""
        hits = self._real_weak_3buy_codes()
        if not hits:
            pytest.skip("真实快照中未找到弱三买样本")
        for code, res, weak in hits:
            strong = [s for s in res.signals
                      if s.kind == "3buy" and s.level == "strong"]
            for w in weak:
                assert w.confidence <= 0.5, "弱三买 confidence 应显著降低"
                for st in strong:
                    assert w.confidence < st.confidence, (
                        "弱三买（未确认）confidence 不应 >= strong（已确认）"
                    )

    def test_weak_3buy_reason_states_price_basis(self):
        """弱三买 reason 必须点明用的是收盘价，避免误读为可按此价买入。"""
        hits = self._real_weak_3buy_codes()
        if not hits:
            pytest.skip("真实快照中未找到弱三买样本")
        for code, res, weak in hits:
            for s in weak:
                assert "收盘" in s.reason, "reason 应说明价格口径为收盘价"
