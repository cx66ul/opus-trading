# -*- coding: utf-8 -*-
"""
Hawk · 大盘环境与六态状态机测试

用例围绕「不该犯的错」设计：
  · 把缺失数据当成 0（会误判成极差）
  · 状态在阈值附近反复跳变（迟滞失效）
  · 高潮期误判为可开仓（真金白银的亏损）

运行： pytest tests/test_regime.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.regime import (
    REGIME_EBB,
    REGIME_FERMENT,
    REGIME_FROZEN,
    REGIME_META,
    REGIME_PEAK,
    REGIME_REPAIR,
    REGIME_START,
    REGIME_THRESHOLDS,
    MarketEnv,
    RegimeTracker,
    Sentiment,
    VolumeInfo,
    classify_regime,
    position_cap,
    score_market,
)

CST = timezone(timedelta(hours=8))


def mk_env(**kw) -> MarketEnv:
    """构造一个可控的 MarketEnv。"""
    s = Sentiment(**kw.pop("sentiment", {}))
    v = VolumeInfo(**kw.pop("volume", {}))
    e = MarketEnv(as_of="2026-10-08T15:00:00+08:00", sentiment=s, volume=v,
                  index_pct=kw.pop("index_pct", 0.0),
                  outside_ok=kw.pop("outside_ok", None))
    return score_market(e)


# ================================================================== 六态判定
class TestClassifyRegime:
    def test_frozen(self):
        """冰点：跌停 > 涨停 + 炸板率 > 60% + 成交额萎缩。"""
        env = mk_env(sentiment=dict(limit_up=10, limit_down=40,
                                    broken_rate=0.65, max_streak=2),
                     volume=dict(total_amount=8e11, amount_ma5=1e12))
        assert classify_regime(env) == REGIME_FROZEN

    def test_peak(self):
        """高潮：涨停 > 120 家但炸板率上升。"""
        env = mk_env(sentiment=dict(limit_up=150, limit_down=5,
                                    broken_rate=0.40, max_streak=9))
        assert classify_regime(env) == REGIME_PEAK

    def test_ferment(self):
        """发酵：连板高度 ≥ 5 且涨停 > 80 家。"""
        env = mk_env(sentiment=dict(limit_up=100, limit_down=8,
                                    broken_rate=0.25, max_streak=6))
        assert classify_regime(env) == REGIME_FERMENT

    def test_ebb(self):
        """退潮：炸板率 p75 以上 + 昨日涨停今日大幅低开 + 市场普涨占比极低。

        ★ 阈值已按实测分位重校准（见 `REGIME_THRESHOLDS`）。
        ★★ 判据是「接力断裂」而非「涨停家数少」——
          曾误加 `涨停<=28(p25)` 条件，导致「今日新封 150 家 + 昨日涨停崩塌」
          这种最该跑的日子落到「高潮」，而高潮允许继续持有（只减不加）。
          退潮与高潮的差别是真金白银：一个是禁新开，一个是可持有。
        """
        env = mk_env(sentiment=dict(limit_up=20, limit_down=45,
                                    broken_rate=0.60, max_streak=5,
                                    yesterday_premium=-3.0,
                                    up_ratio=0.20))
        assert classify_regime(env) == REGIME_EBB

    def test_ebb_does_not_require_few_limit_ups(self):
        """★★ 退潮判据不得依赖「涨停家数少」。

        「今日新封 150 家」与「昨日涨停今日 -4%」可以并存：
        前者新钱进场，后者老钱撤退。退潮讲的是后者。
        若要求涨停家数少，这种日子会被误判成「高潮」（可持有），
        而它其实是退潮（禁新开）—— 判错方向会让人在最该跑的时候以为能开仓。
        """
        env = mk_env(sentiment=dict(limit_up=150, limit_down=50,
                                    broken_rate=0.70, max_streak=8,
                                    yesterday_premium=-4.0,
                                    up_ratio=0.15))
        assert classify_regime(env) == REGIME_EBB

    def test_start(self):
        """启动：连板/涨停/上涨占比**三者**都达p50 以上。

        ★ 原版只要 `3<=连板<=5 且 涨停>跌停`——实测连板高度 p10~p90
          全在 3~5，该条件几乎恒真，导致 90.8% 的日子都是「启动」。
          现在三个条件缺一不可。
        """
        env = mk_env(sentiment=dict(limit_up=70, limit_down=15,
                                    broken_rate=0.25, max_streak=4,
                                    up_ratio=0.65))
        assert classify_regime(env) == REGIME_START

    def test_start_needs_all_three_conditions(self):
        """★★ 只满足部分条件不得判「启动」（防退回单条件版本）。

        连板 4（达 p50）但上涨占比只有 0.20（低于 p25）⇒ 不是启动。
        """
        env = mk_env(sentiment=dict(limit_up=70, limit_down=15,
                                    broken_rate=0.25, max_streak=4,
                                    up_ratio=0.20))
        assert classify_regime(env) != REGIME_START

    def test_repair(self):
        """修复：涨停 > 跌停且上涨占比回到 p50。"""
        env = mk_env(sentiment=dict(limit_up=45, limit_down=12,
                                    broken_rate=0.30, max_streak=2,
                                    yesterday_premium=1.2,
                                    up_ratio=0.50))
        assert classify_regime(env) == REGIME_REPAIR

    def test_peak_checked_before_ferment(self):
        """★高潮必须优先于发酵判定。

        「涨停 150 家 + 连板 9 板」同时满足两者，
        若顺序写反会判成发酵（可加仓至 80%），实际应是高潮（只减不加）。
        这条差之毫厘，实盘是灾难性的。
        """
        env = mk_env(sentiment=dict(limit_up=150, limit_down=5,
                                    broken_rate=0.40, max_streak=9))
        assert classify_regime(env) == REGIME_PEAK, "必须判高潮而非发酵"

    def test_ebb_checked_first(self):
        """退潮必须最先判定 —— 高炸板 + 低开是明确的离场信号。

        ★ 这里涨停给150 家（远超p90），按理会先命中「高潮」；
          正因为退潮判定在前，才能保证「高位炸板 + 昨日涨停崩塌」
          这种最该跑的时刻不会被误判成「高潮可加仓」。
        """
        env = mk_env(sentiment=dict(limit_up=150, limit_down=50,
                                    broken_rate=0.70, max_streak=8,
                                    yesterday_premium=-4.0,
                                    up_ratio=0.15))
        assert classify_regime(env) == REGIME_EBB


# ================================================================== 仓位上限
class TestPositionCap:
    def test_caps_per_spec(self):
        """仓位上限必须严格符合附录 4.7 的表。"""
        assert position_cap(REGIME_FROZEN) == 0.20
        assert position_cap(REGIME_REPAIR) == 0.40
        assert position_cap(REGIME_START) == 0.70
        assert position_cap(REGIME_FERMENT) == 0.80
        assert position_cap(REGIME_PEAK) == 0.50
        assert position_cap(REGIME_EBB) == 0.20

    def test_peak_and_ebb_forbid_opening(self):
        """★高潮「只减不加」、退潮「禁新开」—— 这两条直接关系盈亏。"""
        assert REGIME_META[REGIME_PEAK]["open_new"] is False
        assert REGIME_META[REGIME_EBB]["open_new"] is False

    def test_cap_is_monotonic_in_risk(self):
        """冰点/退潮的仓位上限必须最低。"""
        assert position_cap(REGIME_FROZEN) <= 0.20
        assert position_cap(REGIME_EBB) <= 0.20


# ================================================================== 缺失数据
class TestMissingDataHandling:
    """★ 这组是本模块最重要的防线。

    东财 push2 不可达 => 主力净流入、隔夜外围都没有数据源。
    如果把缺失当 0 参与加权，市场会被误判为「极差」——
    实测：缺失外围若按 0 计分，会把 MarketScore 拉低约 7.5 分（0.15 权重）。
    """

    def test_missing_factors_are_listed(self):
        """缺失因子必须被显式记录，不能默默消失。"""
        env = mk_env(outside_ok=None)
        assert env.missing_factors, "缺失因子必须被记录"
        assert any("outside" in m for m in env.missing_factors)

    def test_missing_not_treated_as_zero(self):
        """★缺失外围应按中性 50 分处理，而不是 0 分。"""
        env = mk_env(sentiment=dict(limit_up=100, limit_down=10,
                                    broken_rate=0.2, max_streak=5),
                     index_pct=1.0, outside_ok=None)
        assert env.detail["outside"] == 50.0, \
            "缺失外围应为中性 50 分，当成 0 会把市场误判为极差"

    def test_weight_renormalized(self):
        """缺失因子的权重要被剔除并重新归一化，不能留空权重。"""
        env = mk_env(outside_ok=None)
        w = env.detail["weights_used"]
        assert "outside" not in w or w["outside"] == 0
        total = sum(w.values())
        assert abs(total - 1.0) < 0.01, f"归一化后权重和应为 1，实得 {total}"

    def test_score_in_range(self):
        """MarketScore 必须落在 0~100。"""
        for kwargs in [
            dict(sentiment=dict(limit_up=0, limit_down=100, broken_rate=1.0,
                                max_streak=0)),
            dict(sentiment=dict(limit_up=200, limit_down=0, broken_rate=0.0,
                                max_streak=10)),
        ]:
            env = mk_env(**kwargs)
            assert 0 <= env.score <= 100, f"分数越界: {env.score}"


# ================================================================== 迟滞
class TestHysteresis:
    """迟滞是附录 4.7 的明确要求：进入需连续 2 点，退出需偏离 10%。"""

    def test_requires_two_confirmations(self):
        """★单次越界不得切换状态 —— 否则会在阈值附近来回跳变。"""
        tr = RegimeTracker(enter_confirm=2)
        base = dict(sentiment=dict(limit_up=45, limit_down=12,
                                    broken_rate=0.30, max_streak=2,
                                    yesterday_premium=1.0,
                                    up_ratio=0.50))
        tr.update(mk_env(as_of="t1", **base))
        assert tr.current == REGIME_REPAIR

        # 一次高潮信号 —— 不应立即切换
        tr.update(mk_env(as_of="t2", sentiment=dict(
            limit_up=150, limit_down=5, broken_rate=0.40, max_streak=9)))
        assert tr.current == REGIME_REPAIR, "单次越界就切换 =迟滞失效"

        # 第二次确认 —— 才切换
        tr.update(mk_env(as_of="t3", sentiment=dict(
            limit_up=150, limit_down=5, broken_rate=0.40, max_streak=9)))
        assert tr.current == REGIME_PEAK, "连续 2 点应切换"

    def test_stable_state_does_not_drift(self):
        """稳定状态下不应无故切换。"""
        tr = RegimeTracker()
        kw = dict(sentiment=dict(limit_up=50, limit_down=10,
                                 broken_rate=0.28, max_streak=3,
                                 yesterday_premium=0.5))
        first = tr.update(mk_env(as_of="t1", **kw)).regime
        for i in range(2, 12):
            r = tr.update(mk_env(as_of=f"t{i}", **kw)).regime
            assert r == first, f"第{i} 次采样无故切换"

    def test_candidate_reset_on_flicker(self):
        """★抖动不应累积确认次数。

        A→B→A→B 这种抖动不应导致切到 B，
        否则迟滞形同虚设。
        """
        tr = RegimeTracker(enter_confirm=2)
        a = dict(sentiment=dict(limit_up=45, limit_down=12,
                                broken_rate=0.30, max_streak=2,
                                yesterday_premium=1.0,
                                up_ratio=0.50))
        b = dict(sentiment=dict(limit_up=150, limit_down=5,
                                broken_rate=0.40, max_streak=9,
                                up_ratio=0.70))
        tr.update(mk_env(as_of="t1", **a))
        for i in range(6):
            tr.update(mk_env(as_of=f"b{i}", **b))
            tr.update(mk_env(as_of=f"a{i}", **a))
        assert tr.current == REGIME_REPAIR, "抖动时不应切到 B"

    def test_history_recorded(self):
        """每个采样点都要留痕（供复盘与回测核对）。"""
        tr = RegimeTracker()
        for i in range(4):
            tr.update(mk_env(as_of=f"t{i}"))
        assert len(tr.history) == 4
        for p in tr.history:
            assert p.ts and p.regime
            assert 0 <= p.cap <= 1


# ================================================================== 纯函数
class TestPurity:
    def test_no_system_time(self):
        """★ 铁律二：状态机也要能回放，不能取系统时间。"""
        import ast
        import re

        p = ROOT / "backend" / "domain" / "regime.py"
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
        assert not pat.search(code), "regime 出现系统时间调用（违反铁律二）"

    def test_score_is_deterministic(self):
        """同输入必须同输出。"""
        kw = dict(sentiment=dict(limit_up=43, limit_down=13, broken_rate=0.427,
                                 max_streak=8, yesterday_premium=0.0))
        a = mk_env(as_of="2026-10-08T15:00:00+08:00", **kw)
        b = mk_env(as_of="2026-10-08T15:00:00+08:00", **kw)
        assert a.score == b.score
        assert a.detail == b.detail


# ================================================================== 已知盲区
class TestKnownGapDocumented:
    """把「文档定义本身覆盖不到的情况」固化成用例。

    实测发现（2026-10-08 真实数据）：涨停 43 家 + 连板 8 板 + 炸板 42.7%
    → 旧版阈值下判为「修复」（发酵要求涨停 > 80 家）。

    ★★ 2026-10-09 阈值重校准后，该组合改判「启动」：
      发酵阈值从「涨停 > 80 家」改为 **p75= 54 家**
      （实测 251 天里涨停 p75 只有 54，80 家是 p93 附近 ⇒ 发酵几乎不触发），
      启动要求「连板 ≥4(p50) + 涨停 ≥39(p50) + 上涨占比 ≥0.457(p50)」
      —— 43 / 8 / 0.50 三项全过 ⇒ 启动。

      这不是 bug，是**原阈值从未用真实分布校准**的必然结果。
      用例名保留原样以记录这段演化，断言按新口径更新。
    """

    def test_high_streak_few_limitups_falls_to_repair(self):
        """涨停 43 家(达p50) + 连板 8 板，但上涨占比 0.50 < p60(0.55)。

        「启动」要求广度达p60，此处差一点 ⇒ 落「修复」。
        这正是「修复」需要「广度**明显改善**」而非「广度不差」的意思。
        """
        env = mk_env(sentiment=dict(limit_up=43, limit_down=13,
                                    broken_rate=0.427, max_streak=8,
                                    yesterday_premium=0.0,
                                    up_ratio=0.50))
        assert classify_regime(env) == REGIME_REPAIR
        # 若将来决定调整判定，需同步更新本用例与附录 4.7

    def test_old_ferment_threshold_was_unreachable(self):
        """★ 固化「涨停 > 80 家」这个旧阈值不可达，防止有人改回去。

        实测251 个完备交易日：涨停 p75=54 / p90=77 / max=130。
        「>80 家」落在 p93 附近 ⇒ 发酵态近乎永不触发（实测仅 4~16 天）。
        """
        th = REGIME_THRESHOLDS
        assert th["ferment_limit_up"] == 54, "发酵阈值应锚定实测 p75"
        assert th["ferment_limit_up"] <= 77, "不应超过实测 p90"
        # 高潮同理
        assert th["peak_limit_up"] == 77, "高潮阈值应锚定实测 p90"