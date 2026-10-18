# -*- coding: utf-8 -*-
"""
Hawk · 推荐引擎与持仓健康度测试

设计围绕「不该犯的错」：
  · 让ST 股进推荐池（附录 4.4 硬性过滤第一条）
  · 输出没有 risk_note 的推荐（附录 4.4 强制要求）
  · 剔除却不给原因（用户无法排查）
  · 缺失数据当 0 处理（把持仓健康度系统性压低）

运行： pytest tests/test_recommend.py -v
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.recommend import (
    HealthRow,
    Recommendation,
    Rejection,
    StockRow,
    assign_tier,
    build_risk_note,
    calc_health,
    calc_score,
    hard_filter,
    recommend,
)

TODAY = date(2026, 10, 8)
OLD_LIST = "2001-01-01"        # 上市远早于 60 日
NEW_LIST = "2026-09-01"        # 上市 37 天


def mk(code="600519", name="测试股", **kw) -> StockRow:
    base = dict(float_mv=5e10, list_date_lb=OLD_LIST, gain_5d=2.0,
                s_board=70.0, s_chanlun=80.0, s_rps=70.0,
                s_news=70.0, s_emotion=70.0)
    base.update(kw)
    return StockRow(code=code, name=name, **base)


# ================================================================== 硬性过滤
class TestHardFilter:
    def test_keep_normal_stock(self):
        rows = [mk()]
        keep, drop = hard_filter(rows, TODAY)
        assert len(keep) == 1 and not drop

    def test_filter_st(self):
        """★ST/退市风险必须剔除（附录 4.4 第一条）。"""
        keep, drop = hard_filter([mk(is_st=True)], TODAY)
        assert not keep
        assert any("ST" in r for r in drop[0].reasons)

    def test_filter_suspend(self):
        """当日停牌必须剔除 —— 买不进。"""
        keep, drop = hard_filter([mk(is_suspend=True)], TODAY)
        assert not keep
        assert any("停牌" in r for r in drop[0].reasons)

    def test_filter_new_listed(self):
        """★上市 < 60 日必须剔除。"""
        keep, drop = hard_filter([mk(list_date_lb=NEW_LIST)], TODAY)
        assert not keep
        assert any("上市仅" in r for r in drop[0].reasons)

    def test_filter_mv_too_small(self):
        keep, drop = hard_filter([mk(float_mv=1e9)], TODAY)   # 10 亿
        assert not keep
        assert any("流通市值" in r for r in drop[0].reasons)

    def test_filter_mv_too_large(self):
        """★>2000 亿剔除。实测这会剔除茅台/宁德等龙头 —— 属文档明确要求。"""
        keep, drop = hard_filter([mk(float_mv=2.5e11)], TODAY)
        assert not keep
        assert any("流通市值" in r for r in drop[0].reasons)

    def test_mv_boundary_inclusive(self):
        """边界值：恰好 20 亿 / 2000 亿应**保留**（是「<20亿或 >2000亿」）。"""
        for mv in (2e9, 2e11):
            keep, _ = hard_filter([mk(float_mv=mv)], TODAY)
            assert keep, f"边界市值 {mv / 1e8:.0f} 亿应保留"

    def test_filter_chase_high(self):
        """★近 5 日涨超35% 剔除（追高）。"""
        keep, drop = hard_filter([mk(gain_5d=36.0)], TODAY)
        assert not keep
        assert any("追高" in r for r in drop[0].reasons)

    def test_filter_one_word_limit(self):
        """当日一字板剔除 —— 买不进。"""
        keep, drop = hard_filter([mk(is_one_word_limit=True)], TODAY)
        assert not keep

    def test_missing_list_date_conservative(self):
        """★无上市日期数据时保守剔除。

        D11 已说明首根日期可能缺失，此时宁可漏掉推荐，
        也不能把次新股当成熟标的推出去。
        """
        keep, drop = hard_filter([mk(list_date_lb=None)], TODAY)
        assert not keep
        assert any("无上市日期" in r for r in drop[0].reasons)

    def test_every_rejection_has_reason(self):
        """★剔除必须逐条给原因 —— 否则用户以为「系统没选中」而非「被规则挡住」。"""
        rows = [mk(code=f"{i:06d}", is_st=True, float_mv=1e9, gain_5d=50.0)
                for i in range(1, 6)]
        keep, drop = hard_filter(rows, TODAY)
        assert len(drop) == 5
        for d in drop:
            assert d.reasons, f"{d.code} 被剔除但无原因"


# ================================================================== 打分
class TestScoring:
    def test_weights_sum_to_one(self):
        from backend.domain.recommend import SCORE_WEIGHTS

        assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-6

    def test_high_factors_high_score(self):
        high = mk(s_board=90, s_chanlun=90, s_rps=90, s_news=90, s_emotion=90)
        low = mk(s_board=10, s_chanlun=10, s_rps=10, s_news=10, s_emotion=10)
        assert calc_score(high) > calc_score(low)

    def test_score_in_range(self):
        assert 0 <= calc_score(mk(s_board=0, s_chanlun=0, s_rps=0,
                                  s_news=0, s_emotion=0)) <= 100
        assert 0 <= calc_score(mk(s_board=100, s_chanlun=100, s_rps=100,
                                  s_news=100, s_emotion=100)) <= 100

    def test_demo_demotes_not_removes(self):
        """★降权项应「降权」而非「剔除」（附录 4.4 明确区分）。"""
        base = mk(s_board=90, s_chanlun=90, s_rps=90, s_news=90, s_emotion=90)
        with_demo = mk(s_board=90, s_chanlun=90, s_rps=90, s_news=90,
                       s_emotion=90, demos=["股东减持公告未消化"])
        assert calc_score(with_demo) < calc_score(base)
        keep, drop = hard_filter([with_demo], TODAY)
        assert len(keep) == 1, "降权项不应被剔除"


# ================================================================== 分档
class TestTier:
    def test_a_needs_multi_dimension(self):
        """★A 档需「分数线 + 至少 3 个维度」，不能只看分数。

        单一因子极高分不应进 A 档 —— 那不是「多因子共振」。
        """
        assert assign_tier(90, 1) != "A"
        assert assign_tier(90, 3) == "A"

    def test_b_needs_two_dimensions(self):
        assert assign_tier(60, 1) == "C"
        assert assign_tier(60, 2) == "B"

    def test_tier_caps(self):
        """附录 4.4：A 档上限 3 只、B 档 8~15 只。"""
        from backend.domain.recommend import TIER_A_MAX, TIER_B_MAX

        assert TIER_A_MAX == 3
        assert TIER_B_MAX == 8

    def test_caps_enforced(self):
        """★分档上限必须真的生效 —— 否则「A 档 3 只」的约定形同虚设。"""
        rows = []
        for i in range(30):
            rows.append(mk(code=f"{i:06d}", s_board=95, s_chanlun=95,
                            s_rps=95, s_news=95, s_emotion=95))
        res = recommend(rows, TODAY)
        assert res["stats"]["A"] <= 3, f"A 档超上限: {res['stats']['A']}"
        assert res["stats"]["B"] <= 8, f"B 档超上限: {res['stats']['B']}"


# ================================================================== 反向审计
class TestRiskNote:
    def test_every_recommendation_has_risk_note(self):
        """★★附录 4.4 强制：「禁止输出无风险提示的推荐」。"""
        rows = [mk(code=f"{i:06d}", s_chanlun=90, s_rps=85, s_news=80)
                for i in range(1, 8)]
        res = recommend(rows, TODAY)
        assert res["recommendations"], "应有推荐"
        for rec in res["recommendations"]:
            assert rec["risk_note"], f"{rec['code']} 缺 risk_note"
            assert len(rec["risk_note"]) >= 10, f"{rec['code']} 的 risk_note 过短"

    def test_never_empty_even_if_no_risk_detected(self):
        """★即使识别不到风险，也必须给兜底说明（不能输出空串）。"""
        perfect = mk(s_chanlun=100, s_rps=95, s_news=95, s_emotion=95,
                     news_direction="bull", news_strength=3,
                     chan_reasons=["三买成立"], board_note="正常")
        note = build_risk_note(perfect, "A")
        assert note and len(note) >= 10

    def test_bearish_news_is_risk(self):
        """★利空消息必须在 risk_note 中出现。"""
        r = mk(news_direction="bear", news_strength=5)
        note = build_risk_note(r, "B")
        assert "利空" in note or "消息面偏空" in note

    def test_no_strong_buy_point_is_risk(self):
        """无三买/一买时必须提示「可能只是反弹」。"""
        r = mk(chan_reasons=["处于中枢区间内"])
        note = build_risk_note(r, "C")
        assert "反弹" in note or "买点" in note

    def test_high_rps_is_risk(self):
        """RPS 过高必须提示追高风险。"""
        note = build_risk_note(mk(s_rps=95), "A")
        assert "追高" in note or "RPS" in note

    def test_board_degradation_disclosed(self):
        """★板块因子降级必须在 risk_note 中告知用户。

        用显式标记 `board_degraded=True` 驱动，不用字符串匹配 ——
        早期按 board_note 内容判断，因「板块强度降级」与「板块强度因子降级」
        差两个字而完全漏判，用户会以为板块因子正常。
        """
        note = build_risk_note(mk(board_degraded=True,
                                  board_note="板块强度因子降级（成分股不可得）"), "B")
        assert "板块" in note and "降级" in note

    def test_board_not_degraded_no_such_warning(self):
        """标记为 False 时不应误报降级。"""
        note = build_risk_note(mk(board_degraded=False, board_note="正常"), "B")
        assert "降级" not in note

    def test_demotions_in_risk_note(self):
        r = mk(demos=["股东减持公告未消化"])
        note = build_risk_note(r, "B")
        assert "减持" in note


# ================================================================== 持仓健康度
class TestHealth:
    def test_weights_match_spec(self):
        """附录 4.3：Health = 0.30结构 + 0.25板块 + 0.20消息 + 0.15量能 + 0.10大盘。"""
        from backend.domain.recommend import HEALTH_WEIGHTS

        assert HEALTH_WEIGHTS["structure"] == 0.30
        assert HEALTH_WEIGHTS["board"] == 0.25
        assert HEALTH_WEIGHTS["news"] == 0.20
        assert HEALTH_WEIGHTS["volume"] == 0.15
        assert HEALTH_WEIGHTS["market"] == 0.10

    def test_healthy_position(self):
        r = calc_health(HealthRow("600519", "茅台", 85, 80, 75, 70), market_score=70)
        assert r["health"] > 60
        assert r["level"] == "green"

    def test_unhealthy_red_advice(self):
        """★Health < 40 → 标红 + 减仓/离场建议。"""
        r = calc_health(HealthRow("000001", "某股", 20, 20, 15, 20), market_score=10)
        assert r["health"] < 40
        assert r["level"] == "red"
        assert "减仓" in r["action"]

    def test_neutral_market_not_zero(self):
        """★缺失板块分时应给中性 50，而非 0。

        若给 0，所有持仓健康度会被系统性压低 0.25×50 = 12.5 分。
        """
        with_zero = calc_health(HealthRow("X", "x", 80, 0, 80, 80), market_score=50)
        with_neutral = calc_health(HealthRow("X", "x", 80, 50, 80, 80), market_score=50)
        assert with_neutral["health"] > with_zero["health"] + 10

    def test_health_in_range(self):
        r = calc_health(HealthRow("X", "x", 0, 0, 0, 0), market_score=0)
        assert 0 <= r["health"] <= 100
        r = calc_health(HealthRow("X", "x", 100, 100, 100, 100), market_score=100)
        assert 0 <= r["health"] <= 100


# ================================================================== 顶层
class TestRecommendPipeline:
    def test_full_pipeline(self):
        rows = [
            mk(code="600519", name="A", s_chanlun=90, s_rps=85, s_news=80),
            mk(code="000001", name="B", is_st=True),          # 剔除
            mk(code="300750", name="C", float_mv=3e11),# 剔除（超大）
        ]
        res = recommend(rows, TODAY)
        assert res["total_input"] == 3
        assert res["rejected"] == 2
        assert res["after_filter"] == 1
        assert len(res["recommendations"]) == 1

    def test_rejections_reportable(self):
        """剔除明细必须可回溯（供 UI 展示与排查）。"""
        rows = [mk(code="000016", name="ST股", is_st=True),
                mk(code="301999", name="新股", list_date_lb=NEW_LIST)]
        res = recommend(rows, TODAY)
        assert len(res["rejections"]) == 2
        for r in res["rejections"]:
            assert r["reasons"]

    def test_empty_input_safe(self):
        """★空输入不得抛异常 —— 数据未同步是常态。"""
        res = recommend([], TODAY)
        assert res["total_input"] == 0
        assert res["recommendations"] == []

    def test_all_filtered_out(self):
        """全部被过滤时不应报错。"""
        rows = [mk(code=f"{i}", is_st=True) for i in range(5)]
        res = recommend(rows, TODAY)
        assert res["recommendations"] == []
        assert res["rejected"] == 5


# ================================================================== 纯函数
class TestPurity:
    def test_no_system_time(self):
        """★推荐引擎禁取系统时间（铁律二）。"""
        import ast
        import re

        p = ROOT / "backend" / "domain" / "recommend.py"
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
        assert not pat.search(code), "recommend 出现系统时间调用（违反铁律二）"

    def test_deterministic(self):
        rows = [mk(code="600519"), mk(code="000001", s_rps=60)]
        a = recommend(rows, TODAY)
        b = recommend(rows, TODAY)
        assert a["stats"] == b["stats"]
        assert [r["code"] for r in a["recommendations"]] == \
               [r["code"] for r in b["recommendations"]]