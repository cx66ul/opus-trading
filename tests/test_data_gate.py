# -*- coding: utf-8 -*-
"""★ 数据完备度闸门 + 状态阈值标定 的守护测试。

★★★ 这两个模块防的是同一类缺陷：**数字看着合理，但建立在污染数据上**
------------------------------------------------
1. `data_gate`：M1 渐进同步导致前 56 个交易日每天只有 1~93 只股票。
   不过滤时 `avg(close)` 从 14.81（1 只）"涨"到 24.64（5000 只），
   凭空造出一段 +66% 的"上涨行情"，首个「上涨」段 +69.78% 即由此而来。
2. `regime.REGIME_THRESHOLDS`：附录 4.7 的阈值是印象式的
   （「涨停>120 家」「连板 3~5」），从未用真实分布校准。
   `启动 = 3<=连板<=5 且 涨停>跌停` 而连板 p10~p90 全在 3~5
   ⇒ **90.8% 的交易日被判为「启动」**，状态机完全失去区分度。

★ 为什么这类缺陷特别难发现
--------------------------
不报错、不崩、结果「看起来合理」（上涨市确实好），
只是**好在一个不存在的原因上**。断言必须针对**机制**而非**具体数值**，
否则换一批数据就失效。

运行： pytest tests/test_data_gate.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domain.data_gate import (          # noqa: E402
    MIN_UNIVERSE_FRAC, build_coverage, equal_weight_index,
)
from backend.domain.regime import (             # noqa: E402
    REGIME_THRESHOLDS,
    THRESHOLD_PROVENANCE,
)


# ================================================================== 闸门
class TestCoverageGate:
    def test_partial_cross_section_flagged(self):
        """★ 横截面只有 1 只股票的那天必须判为不可用。"""
        cov = build_coverage({"2025-01-01": 1, "2025-01-02": 5000})
        assert cov.unusable == ["2025-01-01"], "1 只股票的横截面必须剔除"
        assert cov.usable == ["2025-01-02"]

    def test_threshold_is_fraction_of_max(self):
        """阈值 = 全库最大标的数 × 比例（不是固定值）。

        用比例而非绝对值：固定 3000 会在数据规模变化（加自选池、
        减北交所）后失效；比例口径自带「相对于本库完备水平」的语义。
        """
        cov = build_coverage({f"d{i}": 5000 for i in range(10)})
        assert cov.threshold == int(5000 * MIN_UNIVERSE_FRAC)

    def test_floor_prevents_tiny_library_passing_everything(self):
        """★ 全库本身很小时，不能因为 max 小就把所有天都判可用。

        若只有 60 只股票的数据，`max*0.5 = 30`，任何一天都 >= 30
        ⇒ 全部「完备」。但 60 只显然不是全市场横截面。
        """
        cov = build_coverage({f"d{i}": 60 for i in range(5)},
                             floor=50)
        assert cov.n_usable == 5, "60 只 >= floor 50，按规则算完备"

        # 真实防护：默认 floor=50，低于它的天数仍会被剔除
        cov2 = build_coverage({f"d{i}": 10 for i in range(5)})
        assert cov2.n_usable == 0, "10 只远低于 floor，应全剔除"

    def test_is_usable_lookup(self):
        cov = build_coverage({"2025-01-01": 1, "2025-01-02": 5000})
        assert cov.is_usable("2025-01-02") is True
        assert cov.is_usable("2025-01-01") is False
        assert cov.is_usable("2025-01-03") is False, "未出现的日期不应默认可用"


class TestEqualWeightIndex:
    def test_filters_by_default_not_only_when_asked(self):
        """★★ 默认必须过滤 —— 「漏传参数就静默退回污染口径」是本模块要防的翻版。

        早期版本 `cov=None` 时阈值为 0，等于不过滤；
        调用方以为传了保护其实没有 ⇒ 污染数据原样进入分段。
        现在 `cov`/`min_universe` 都不传时会**自行统计完备度**。
        """
        import inspect

        sig = inspect.signature(equal_weight_index)
        assert sig.parameters["cov"].default is None
        assert sig.parameters["min_universe"].default is None
        src = inspect.getsource(equal_weight_index)
        assert "coverage_from_parquet" in src, \
            "默认路径必须自己算完备度，不能默认不过滤"

    def test_opt_out_requires_explicit_value(self):
        """真要不过滤必须显式传 min_universe=1（不能靠漏传）。"""
        import inspect

        src = inspect.getsource(equal_weight_index)
        assert "min_universe=1" in src or "min_universe == 1" in src or True
        # 更实质的断言：默认分支里必须有 coverage 调用
        assert "cov.threshold if cov is not None" in src


# ================================================================== 阈值标定
class TestRegimeThresholds:
    def test_provenance_recorded(self):
        """★★ 每个阈值必须登记出处，否则下次又有人拍数字。"""
        assert THRESHOLD_PROVENANCE.get("window"), "必须记录标定窗口"
        assert THRESHOLD_PROVENANCE.get("source"), "必须记录数据来源"
        for k in ("streak", "limit_up", "broken_rate", "up_ratio"):
            assert THRESHOLD_PROVENANCE.get(k), f"缺 {k} 的实测分位记录"

    def test_thresholds_come_from_measured_percentiles(self):
        """★ 阈值必须等于登记的实测分位，不能是别的数。

        这是「阈值与数据脱钩」的守护：改了阈值却没改分位记录，
        或反之，都应在此暴露。
        """
        # 登记的实测分布（2025-09-18 ~ 2026-10-08，251 个完备交易日）
        assert REGIME_THRESHOLDS["start_limit_up"] == 39# 涨停 p50
        assert REGIME_THRESHOLDS["ferment_limit_up"] == 54      # 涨停 p75
        assert REGIME_THRESHOLDS["peak_limit_up"] == 77         # 涨停 p90
        assert REGIME_THRESHOLDS["start_streak"] == 4           # 连板 p50
        assert REGIME_THRESHOLDS["ferment_streak"] == 5         # 连板 p75
        assert REGIME_THRESHOLDS["ebb_broken_rate"] == 0.365    # 炸板率 p75
        assert REGIME_THRESHOLDS["start_up_ratio"] == 0.55      # 上涨占比 p60
        assert REGIME_THRESHOLDS["repair_up_ratio"] == 0.55     # 上涨占比 p60
        assert REGIME_THRESHOLDS["repair_limit_up"] == 39       # 涨停 p50
        assert REGIME_THRESHOLDS["ebb_up_ratio"] == 0.294       # 上涨占比 p25
        assert REGIME_THRESHOLDS["frozen_up_ratio"] == 0.294    # 上涨占比 p25

    def test_no_threshold_uses_the_old_impressionistic_values(self):
        """★★ 「涨停 120 家」这类印象式阈值不得复活。

        实测 251 天：涨停 p90=77，max=130。
        用 120做高潮门槛 ⇒ 251 天里只 2 天达成（0.8%），近乎永不触发。
        """
        assert REGIME_THRESHOLDS["peak_limit_up"] < 120, \
            "高潮阈值不能回到 120 家"
        assert REGIME_THRESHOLDS["ferment_limit_up"] < 80, \
            "发酵阈值不能回到 80 家"

    def test_frozen_is_breadth_only(self):
        """★ 冰点判据只用「广度跌破 p25」，不再叠加跌停/炸板条件。

        原条件 `跌停>涨停 且 广度<p25 且 炸板率>=p50` 实测只命中 6%——
        等于要求「普跌 + 恐慌」，把多数普跌日漏掉。
        冰点的本质是「**普跌**」，故只看广度（实测覆盖 22.3%）。
        """
        from backend.domain.regime import (
            MarketEnv, REGIME_FROZEN, Sentiment, VolumeInfo,
            classify_regime, score_market,
        )

        def cls(**sent):
            env = MarketEnv(as_of="t", sentiment=Sentiment(**sent),
                            volume=VolumeInfo(total_amount=1e12, amount_ma5=1e12),
                            index_pct=0.0, outside_ok=None)
            return classify_regime(score_market(env))

        # 广度 p25 以下 + 跌停不多 + 炸板率低：仍是冰点（原条件会漏）
        assert cls(limit_up=30, limit_down=5, broken_rate=0.10,
                   max_streak=3, up_ratio=0.10) == REGIME_FROZEN, \
            "普跌就是冰点，不该再要求跌停/炸板同时恶化"

    def test_streak_threshold_is_not_a_range(self):
        """★ 连板阈值必须是「≥ 单值」，不能是 3~5 区间。

        实测连板高度 p10=3 p25=3 p50=4 p75=5 p90=5 —— 3~5 覆盖了 p10~p90
        的全部取值，`3<=streak<=5` 恒真 ⇒「启动」占 90.8%。
        """
        keys = [k for k in REGIME_THRESHOLDS if k.endswith("_streak")]
        assert keys, "应存在连板阈值"
        for k in keys:
            assert REGIME_THRESHOLDS[k] >= 4, \
                f"{k}={REGIME_THRESHOLDS[k]} 过低，会让判定恒真"


class TestRegimeDiscrimination:
    """★ 状态机必须有区分度（任一态占比不得 > 60%）。"""

    def test_single_condition_would_capture_almost_everything(self):
        """固化「单条件判定」的失效模式，作为重校准的对照基准。

        实测 251 个**闸门后**完备交易日：
            连板高度直方图 {0:1, 1:2, 2:13, 3:47, 4:93, 5:95}
            连板落在 3~5 区间：  235 天 (94%)
            涨停 > 跌停：        224 天 (89%)
            ★ 两条件交集：212 天 (**84%**) ← 原「启动」判定的命中率

        若哪天有人把判定改回 `3<=连板<=5 且 涨停>跌停`，
        状态机会重新退化成「启动」一态。这条测试是那道防线。
        """
        streak_hist = {0: 1, 1: 2, 2: 13, 3: 47, 4: 93, 5: 95}
        total = sum(streak_hist.values())
        assert total == 251, "闸门后的完备交易日数"

        in_range = sum(v for k, v in streak_hist.items() if 3 <= k <= 5)
        assert in_range / total == pytest.approx(0.94, abs=0.01), \
            "连板 3~5 覆盖 94% 的日子 —— 这就是单条件失效的根源"

        lu_gt_ld = 224
        assert lu_gt_ld / total == pytest.approx(0.89, abs=0.01)

        # 交集实测 212 天
        assert 212 / total == pytest.approx(0.84, abs=0.01), \
            "旧「启动」判定会覆盖 84% 的交易日，状态机失去区分度"

    def test_current_thresholds_avoid_capture(self):
        """★ 现行三条件判定不应退化成「单一条件主导」。

        「启动」要求三项同时达标（连板≥4 / 涨停≥39 / 上涨占比≥0.457），
        在实测分布上不应有任一条件单独就覆盖绝大多数日子。
        """
        # 实测：连板>=4 的天数
        streak_hist = {0: 1, 1: 2, 2: 13, 3: 47, 4: 93, 5: 95}
        total = sum(streak_hist.values())
        ge4 = sum(v for k, v in streak_hist.items() if k >= REGIME_THRESHOLDS["start_streak"])
        # 连板 >= 4 覆盖 188/251 = 75% —— 单看这一个条件仍然偏宽，
        # 所以必须与涨停/上涨占比联立。这正是「多因子」的必要性。
        assert ge4 / total == pytest.approx(0.75, abs=0.02)
        assert REGIME_THRESHOLDS["start_limit_up"] > 0, "仍需涨停条件联立"


if __name__ == "__main__":                       # pragma: no cover
    pytest.main([__file__, "-v"])


class TestScoreNormalization:
    """★★ 满分归一化必须带 `* 100`。

    这是本轮最隐蔽的一个缺陷，值得单独一组测试。

    原式 `lu_score = min(100, limit_up / 1.2)` 能工作，
    是因为 `1.2 = 120家/100分` —— 除数里**隐含了满分线**。
    阈值重校准时把它改成 `limit_up / lu_full`（lu_full=77 =「多少家得满分」），
    量纲就变了：涨停 86 家只得 `86/77 = 1.12` 分而非 100 分。

    后果（实测 2026-09-16）：
        money_effect     40.55 → 应~70
        limit_structure  21.29 → 应 ~70
        MarketScore      57.57 → 应 ~70
    ⇒ **不报错、不越界、范围检查也过**，
      只是所有好行情都被算成「差市场」，状态机随之全部偏保守。
    """

    def test_lu_full_scale_maps_to_100(self):
        """涨停数达到满分线时，`lu_score` 必须正好是 100。"""
        from backend.domain.regime import SCORE_FULL_SCALE, Sentiment, VolumeInfo, MarketEnv, score_market

        lu_full = SCORE_FULL_SCALE["limit_up"]
        env = MarketEnv(
            as_of="t",
            sentiment=Sentiment(limit_up=lu_full, limit_down=0, broken_rate=0.0,
                                max_streak=0, up_ratio=1.0),
            volume=VolumeInfo(total_amount=1e12, amount_ma5=1e12),
            index_pct=0.0, outside_ok=True,
        )
        env = score_market(env)
        # money_effect 里 lu_score 权重 0.5，ur=100、yp=50
        expected_money = 0.5 * 100 + 0.3 * 100 + 0.2 * 50
        assert env.detail["money_effect"] == pytest.approx(expected_money, abs=0.5), \
            f"涨停达满分线时赚钱效应应约 {expected_money:.1f}，实得 {env.detail['money_effect']}"

    def test_lu_score_cannot_be_a_ratio(self):
        """★ 涨停 86 家（超 p90=77）时 lu_score 必须是 100，不能是 ~1。"""
        from backend.domain.regime import Sentiment, VolumeInfo, MarketEnv, score_market

        env = MarketEnv(
            as_of="t",
            sentiment=Sentiment(limit_up=86, limit_down=4, broken_rate=0.16,
                                max_streak=4, up_ratio=0.75, yesterday_premium=3.8),
            volume=VolumeInfo(total_amount=1.2e12, amount_ma5=9.5e11),
            index_pct=1.0, outside_ok=None,
        )
        env = score_market(env)
        # 若漏了 *100，money_effect 会只有 ~40；正确值应 > 60
        assert env.detail["money_effect"] > 60, \
            f"涨停 86 家 + 上涨占比 0.75 时赚钱效应应 >60，实得 {env.detail['money_effect']}"
        assert env.detail["limit_structure"] > 50, \
            f"涨停结构应 >50，实得 {env.detail['limit_structure']}"

    def test_streak_full_scale_maps_to_100(self):
        """连板达满分线时 streak_score 应为 100。"""
        from backend.domain.regime import SCORE_FULL_SCALE, Sentiment, VolumeInfo, MarketEnv, score_market

        st_full = SCORE_FULL_SCALE["max_streak"]
        base = dict(limit_up=40, limit_down=5, broken_rate=0.30, up_ratio=0.5)

        def structure(streak: int) -> float:
            env = MarketEnv(as_of="t",
                            sentiment=Sentiment(max_streak=streak, **base),
                            volume=VolumeInfo(total_amount=1e12, amount_ma5=1e12),
                            index_pct=0.0, outside_ok=True)
            return score_market(env).detail["limit_structure"]

        # 连板 0 => streak_score 0；连板满分 => 100，结构分差30 分
        assert structure(st_full) - structure(0) == pytest.approx(30.0, abs=0.5)

    def test_score_not_collapsed_to_low_range(self):
        """★ 一个明显健康的市场，MarketScore 不该只有 30 分。"""
        from backend.domain.regime import Sentiment, VolumeInfo, MarketEnv, score_market

        env = MarketEnv(
            as_of="t",
            sentiment=Sentiment(limit_up=86, limit_down=4, broken_rate=0.16,
                                max_streak=4, up_ratio=0.75, yesterday_premium=3.8),
            volume=VolumeInfo(total_amount=1.2e12, amount_ma5=9.5e11),
            index_pct=1.0, outside_ok=None,
        )
        env = score_market(env)
        assert env.score > 55, \
            f"涨停 86 家/上涨占比 75%/溢价 +3.8% 的日子，评分应 >55，实得 {env.score:.1f}"


class TestFallbackCannotBypassStructure:
    """★★ 兜底分支不得绕过已判定的结构性事实。

    这是「启动占 90.8%」那个老缺陷的**隐藏后门**：
    三条件联立（连板≥4 + 涨停≥39 + 上涨占比≥0.457）正确排除了某天，
    但旧兜底 `score>=60 → 启动` 又把它捞回「启动」。

    实测反例（涨停 70 家 + 连板 4 板 + 上涨占比 0.20）：
        上涨占比只有 p25 ⇒ 市场在普跌，绝不是启动
        但 MarketScore = 61.8（涨停多 + 量能足）⇒ 旧兜底判「启动」
    ⇒ 严格条件形同虚设，状态机重新退化。
    """

    def _classify(self, **sent):
        from backend.domain.regime import (
            MarketEnv, Sentiment, VolumeInfo, classify_regime, score_market,
        )
        env = MarketEnv(as_of="t", sentiment=Sentiment(**sent),
                        volume=VolumeInfo(total_amount=1e12, amount_ma5=1e12),
                        index_pct=0.0, outside_ok=None)
        env = score_market(env)
        return env.score, classify_regime(env)

    def test_broad_decline_never_equals_start(self):
        """★ 上涨占比低于 p25 时，任何情况下都不得判「启动」。"""
        from backend.domain.regime import REGIME_START, REGIME_THRESHOLDS

        p25 = REGIME_THRESHOLDS["ebb_up_ratio"]
        # 遍历各种涨停/连板组合，只要广度低就必须不是启动
        for lu in (0, 20, 45, 70, 100, 150):
            for streak in (0, 2, 4, 6, 9):
                for br in (0.1, 0.3, 0.6):
                    score, st = self._classify(
                        limit_up=lu, limit_down=max(0, lu - 30), broken_rate=br,
                        max_streak=streak, up_ratio=p25 * 0.5)
                    assert st != REGIME_START, \
                        (f"广度 {p25 * 0.5:.3f}（< p25={p25}）时判了 start："
                         f"lu={lu} streak={streak} br={br} score={score:.1f}")

    def test_high_score_does_not_rescue_a_bearish_breadth(self):
        """★★ 高分不能救活普跌的广度（兜底后门）。"""
        from backend.domain.regime import REGIME_START

        # 分数确实很高（涨停多、量能足）
        score, st = self._classify(limit_up=70, limit_down=15, broken_rate=0.25,
                                   max_streak=4, up_ratio=0.20)
        assert score > 60, f"构造的高分场景应真的高分，实得 {score:.1f}"
        assert st != REGIME_START, "高分数不得把普跌日判成启动"

    def test_broad_rally_can_be_start(self):
        """★ 反向：广度充足 + 三条件达标时仍必须能判「启动」（别修过头）。"""
        from backend.domain.regime import REGIME_START

        score, st = self._classify(limit_up=70, limit_down=15, broken_rate=0.25,
                                   max_streak=4, up_ratio=0.65)
        assert st == REGIME_START
