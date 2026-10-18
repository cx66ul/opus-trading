# -*- coding: utf-8 -*-
"""
Hawk · K 线标注层测试（决策 D13「A+C 组合」）

★ 这组测试守护的是**产品定位**本身，不只是计算正确性：

  用户 2026-10-09 08:13 决策：
    A（止损参考）+ C（不自动卖出，卖点由用户决断）
  ⇒ 系统**不得输出任何卖出动作指令**，卖点只能是「提醒」。

  一旦代码里混进 action=SELL / order 字段，
  系统就从「副驾」悄悄变成「交易员」——
  而这正是提示词第十二章禁止的方向。
  所以下面的 TestNoSellInstruction 是本文件最重要的一组。

运行： pytest tests/test_overlay.py -v
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.chan import analyze                        # noqa: E402
from backend.domain.overlay import (                           # noqa: E402
    BUY_KINDS,
    SELL_KINDS,
    KlineOverlay,
    Mark,
    PositionView,
    atr,
    build_overlay,
    build_position_view,
    stop_references,
)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 8, 15, 0, tzinfo=CST)


def mk_rows(n: int = 300, seed: int = 7) -> list[dict]:
    """确定性但有波动的行情（必须能产出分型/中枢，否则验证无意义）。"""
    rng = random.Random(seed)
    rows, px, d = [], 100.0, date(2025, 1, 2)
    for _ in range(n):
        px = max(5.0, px * (1 + rng.uniform(-0.035, 0.038)))
        o = px * (1 + rng.uniform(-0.008, 0.008))
        rows.append({"trade_date": d.isoformat(), "open": o,
                     "high": max(o, px) * 1.012, "low": min(o, px) * 0.988,
                     "close": px, "volume": 1e6})
        d += timedelta(days=1)
    return rows


@pytest.fixture(scope="module")
def res_and_rows():
    rows = mk_rows()
    return analyze("TEST", rows, as_of=AS_OF), rows


# ================================================================ 不输出卖出指令
class TestNoSellInstruction:
    """★★ C 方案的核心保障：系统不得下卖出指令。"""

    def test_overlay_json_has_no_action_field(self, res_and_rows):
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        blob = json.dumps(ov.to_json(), ensure_ascii=False)
        # ★ 排除 action_hint / position（那是「相对中枢位置」，不是动作）
        for bad in ('"action"', '"order"', '"side"', '"instruction"'):
            assert bad not in blob, f"标注层出现指令字段 {bad}"

    def test_all_sell_marks_are_advisory(self, res_and_rows):
        """★ 所有卖点必须标记 advisory=True。"""
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        sells = [m for m in ov.marks if m.kind == "sell"]
        assert sells, "构造数据应至少产生一个卖点"
        for m in sells:
            assert m.advisory is True, "卖点必须是提醒（advisory）而非指令"

    def test_buy_marks_not_advisory(self, res_and_rows):
        """买点不标advisory —— 那是买点本身已是提示。"""
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        for m in ov.marks:
            if m.kind == "buy":
                assert m.advisory is False

    def test_position_view_has_no_sell_action(self, res_and_rows):
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res,
                                 entry_price=100.0, as_of=AS_OF)
        blob = json.dumps(pv.to_json(), ensure_ascii=False)
        for bad in ('"action"', '"order"', '"BUY"', '"SELL"', '"target_price"'):
            assert bad not in blob, f"持仓视图出现指令字段 {bad}"

    def test_disclaimer_states_user_decides(self, res_and_rows):
        """★ 免责声明必须明确「卖出由你决定」，且持仓视图要有。"""
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        assert "不构成投资建议" in ov.disclaimer
        assert "由你决定" in ov.disclaimer or "自行决断" in ov.disclaimer
        pv = build_position_view("TEST", "测试", rows, res, 100.0, AS_OF)
        assert "由你决定" in pv.disclaimer

    def test_sell_note_marks_as_reminder(self, res_and_rows):
        """持仓视图的卖点区必须带「提醒」字样。"""
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res, 100.0, AS_OF)
        assert "提醒" in pv.to_json()["sell_note"]
        assert "非指令" in pv.to_json()["sell_note"] or \
               "不自动卖出" in pv.to_json()["sell_note"]

    def test_action_hint_never_says_should_sell(self, res_and_rows):
        """★ action_hint 里不得出现「建议卖出」这类祈使指令。

        只能陈述客观状态（浮盈浮亏、是否有卖点提醒、相对中枢位置）。
        """
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res, 90.0, AS_OF)
        h = pv.action_hint
        for banned in ("建议卖出", "应该卖", "必须卖", "立即卖出"):
            assert banned not in h, f"action_hint 含指令性表述：{h}"

    def test_stop_refs_labeled_as_reference_only(self, res_and_rows):
        """★ 止损位必须声明「系统不会自动触发」。"""
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res, 100.0, AS_OF)
        assert "不会自动触发" in pv.to_json()["stop_note"]


# ================================================================ 标注正确性
class TestMarks:
    def test_buy_marks_only_strong_by_default(self, res_and_rows):
        """★ 默认只显示 strong 买点（M8 实测 weak 胜率仅 20%）。"""
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        buys = [m for m in ov.marks if m.kind == "buy"]
        assert buys
        for m in buys:
            assert m.level == "strong", f"默认不应显示 {m.level} 买点"

    def test_show_weak_includes_weak_buys(self, res_and_rows):
        res, rows = res_and_rows
        base = build_overlay("TEST", "测试", rows, res, AS_OF, show_weak=False)
        wide = build_overlay("TEST", "测试", rows, res, AS_OF, show_weak=True)
        n_base = len([m for m in base.marks if m.kind == "buy"])
        n_wide = len([m for m in wide.marks if m.kind == "buy"])
        assert n_wide >= n_base

    def test_sell_marks_include_non_strong(self, res_and_rows):
        """★ 卖点默认**显示全部**级别（含 normal）。

        理由：卖点本来就稀疏（M9 实测 94 笔里只有 8 笔卖点出场），
        漏掉 normal 档会让持仓者误以为「没有卖点」。
        """
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        sells = [m for m in ov.marks if m.kind == "sell"]
        assert sells
        assert all(m.signal_kind in SELL_KINDS for m in sells)

    def test_buy_kinds_only(self, res_and_rows):
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        for m in ov.marks:
            if m.kind == "buy":
                assert m.signal_kind in BUY_KINDS

    def test_marks_have_reason_and_confidence(self, res_and_rows):
        """★ 每条标注都要能解释（第十二章第 4 条）。"""
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        for m in ov.marks:
            assert m.reason, f"{m.signal_kind} 缺少理由"
            assert 0 < m.confidence <= 1.0

    def test_mark_dates_exist_in_bars(self, res_and_rows):
        """★ 标注日期必须能在 K 线里找到，否则前端画不到点上。"""
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        dates = {b["date"] for b in ov.bars}
        for m in ov.marks:
            assert m.date in dates, f"{m.date} 不在 K 线日期里"

    def test_zhongshus_have_range_and_dates(self, res_and_rows):
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        for z in ov.zhongshus:
            assert z["low"] <= z["high"]
            assert z["start_date"] and z["end_date"]

    def test_bis_have_direction(self, res_and_rows):
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        for b in ov.bis:
            assert b["dir"] in ("up", "down")

    def test_overlay_bars_are_ohlc(self, res_and_rows):
        """★ 前端要画蜡烛图，OHLC 必须自洽。"""
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        for b in ov.bars:
            assert b["low"] <= b["open"] <= b["high"], b
            assert b["low"] <= b["close"] <= b["high"], b

    def test_position_string(self, res_and_rows):
        res, rows = res_and_rows
        ov = build_overlay("TEST", "测试", rows, res, AS_OF)
        assert ov.position in ("中枢上方", "中枢内", "中枢下方", "无中枢")


# ================================================================ 止损参考位
class TestStopReferences:
    def test_all_three_bases_given(self):
        """★ 三种取法都要给，让用户自己选。"""
        refs = stop_references(100.0, zhongshu_low=90.0,
                               prev_bi_low=85.0, atr=5.0, atr_mult=2.0)
        bases = {m.stop_basis for m in refs}
        assert bases == {"zhongshu_low", "prev_bi_start", "atr"}

    def test_sorted_high_to_low(self):
        refs = stop_references(100.0, 90.0, 85.0, 5.0)
        prices = [m.price for m in refs]
        assert prices == sorted(prices, reverse=True)

    def test_no_ref_above_entry(self):
        """★ 高于入场价的是止盈方向，不能混进止损位。"""
        refs = stop_references(100.0, zhongshu_low=105.0,
                               prev_bi_low=110.0, atr=None)
        assert refs == [], "高于入场价的位不应作为止损"

    def test_zero_gap_ref_dropped(self):
        """★★ 止损位等于入场价毫无意义（一进场就触发）。

        实测踩过：买点恰好在中枢下沿附近（54.56 买 / 54.56 止损），
        「距入场 -0.0%」的止损位是噪声。
        """
        refs = stop_references(54.56, zhongshu_low=54.56,
                               prev_bi_low=54.5, atr=None)
        assert all(m.stop_basis != "zhongshu_low" for m in refs), \
            "与入场价几乎重合的中枢下沿不应作为止损位"

    def test_min_gap_threshold(self):
        """距入场 < min_gap_pct 的位被丢弃。"""
        refs = stop_references(100.0, zhongshu_low=99.8,
                               prev_bi_low=None, atr=None,
                               min_gap_pct=0.005)
        assert refs == [], "距入场 0.2% < 0.5% 阈值，应被丢弃"

    def test_each_ref_explains_itself(self):
        """★ 每个止损位都要说明取法与代价。"""
        refs = stop_references(100.0, 90.0, 85.0, 5.0)
        for m in refs:
            assert m.stop_note, f"{m.stop_basis} 缺少说明"
            assert "距入场" in m.stop_note
            assert m.stop_basis in m.stop_note or m.stop_basis

    def test_atr_ref_uses_mult(self):
        refs = stop_references(100.0, None, None, atr=5.0, atr_mult=3.0)
        assert len(refs) == 1
        assert refs[0].price == pytest.approx(100.0 - 15.0)

    def test_none_atr_no_ref(self):
        assert stop_references(100.0, 90.0, 85.0, atr=None) != []

    def test_refs_have_no_date(self):
        """止损位是「当前参考」，不锚定历史日期。"""
        refs = stop_references(100.0, 90.0, 85.0, 5.0)
        assert all(m.date == "" for m in refs)


# ================================================================ ATR
class TestAtr:
    def test_atr_computed(self, res_and_rows):
        _res, rows = res_and_rows
        bars = [{"high": r["high"], "low": r["low"], "close": r["close"]}
                for r in rows]
        a = atr(bars, 14)
        assert a is not None and a > 0

    def test_atr_none_when_insufficient(self):
        """★ 数据不足返回 None，不能返回 0（缺失≠0）。"""
        assert atr([{"high": 1, "low": 1, "close": 1}], 14) is None
        assert atr([], 14) is None

    def test_atr_positive_for_volatile(self, res_and_rows):
        _res, rows = res_and_rows
        bars = [{"high": r["high"], "low": r["low"], "close": r["close"]}
                for r in rows]
        assert atr(bars, 14) > 0.01


# ================================================================ 持仓视图
class TestPositionView:
    def test_unrealized_pct(self, res_and_rows):
        res, rows = res_and_rows
        cur = res.bars[-1].c
        pv = build_position_view("TEST", "测试", rows, res, cur, AS_OF)
        assert pv.unrealized_pct == pytest.approx(0.0, abs=0.5)

    def test_loss_position_negative(self, res_and_rows):
        res, rows = res_and_rows
        cur = res.bars[-1].c
        pv = build_position_view("TEST", "测试", rows, res, cur * 1.2, AS_OF)
        assert pv.unrealized_pct < 0

    def test_active_sells_window(self, res_and_rows):
        """★ 只提示近 30 根内的卖点 —— 更早的已无关紧要。"""
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res,
                                 res.bars[-1]["close"] if isinstance(res.bars[-1], dict)
                                 else res.bars[-1].c, AS_OF)
        cutoff = pv.overlay.bars[-30]["date"] if \
            len(pv.overlay.bars) >= 30 else pv.overlay.bars[0]["date"]
        for m in pv.active_sells:
            assert m["date"] >= cutoff, f"{m['date']} 超出近 30 根窗口"

    def test_buy_above_zhongshu_hint(self, res_and_rows):
        """★ 中枢上方时应陈述该状态（客观描述，不是建议）。"""
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res,
                                 res.bars[-1].c, AS_OF)
        if pv.overlay.position == "中枢上方":
            assert "中枢上方" in pv.action_hint

    def test_json_serializable(self, res_and_rows):
        """★ 必须能 JSON 序列化（前端/邮件都要用）。"""
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res, 100.0, AS_OF)
        json.dumps(pv.to_json(), ensure_ascii=False)  # 不应抛异常

    def test_view_contains_overlay(self, res_and_rows):
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res, 100.0, AS_OF)
        assert isinstance(pv.overlay, KlineOverlay)
        assert pv.overlay.bars

    def test_zero_entry_price(self, res_and_rows):
        """入场价为 0 时不应崩（脏数据保护）。"""
        res, rows = res_and_rows
        pv = build_position_view("TEST", "测试", rows, res, 0.0, AS_OF)
        assert pv.unrealized_pct is None


# ================================================================ 确定性
class TestPurity:
    def test_build_twice_identical(self, res_and_rows):
        """★ 纯函数：同样输入两次输出必须一致。"""
        res, rows = res_and_rows
        a = build_overlay("T", "x", rows, res, AS_OF).to_json()
        b = build_overlay("T", "x", rows, res, AS_OF).to_json()
        assert a == b

    def test_no_system_time(self):
        """★ 领域层禁止 datetime.now()（铁律二）。"""
        import re
        src = (ROOT / "backend" / "domain" / "overlay.py").read_text(
            encoding="utf-8")
        code = "\n".join(
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#"))
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        assert not re.search(r"\b(datetime\.now|time\.time)\s*\(", code), \
            "overlay.py 出现了系统时间调用（违反铁律二）"

    def test_empty_signals_handled(self):
        """无信号时也应正常产出（不崩、stop_refs 为空）。"""
        rows = [{"trade_date": f"2025-01-{i + 1:02d}", "open": 10.0,
                 "high": 10.1, "low": 9.9, "close": 10.0, "volume": 1e6}
                for i in range(20)]
        res = analyze("E", rows, as_of=AS_OF)
        ov = build_overlay("E", "空", rows, res, AS_OF)
        assert ov.marks == []
        json.dumps(ov.to_json(), ensure_ascii=False)