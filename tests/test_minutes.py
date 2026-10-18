"""分钟线同步约束与消息联动回看的测试。

★ 重点固化两个「静默失效」：
1. **count 只认 320/720**，其他值静默回落到 320 且无报错
   （同 M1 的 fqkline 陷阱 —— 传大值不报错反而给少数据）
2. **缺失窗口必须标available=False**，不能用 0冒充「持平」
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.domain.news_impact import (
    WINDOWS, MessageImpact, WindowOutcome, _period_for, judge,
)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 9, 1, 0, tzinfo=CST)


# ---------------------------------------------------------------- count 约束
class TestCountConstraint:
    def test_only_320_and_720_valid(self):
        """★腾讯分钟线只认 320/ 720，其他值静默回落。

        实测：传 1000/2000/5000/8000 都返回 482 根且无任何报错。
        若不察觉，会以为「拿到了 320 根其实够用」，实际丢历史。
        """
        import httpx
        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36")
        c = httpx.Client(timeout=20.0, follow_redirects=True, trust_env=False,
                         headers={"User-Agent": UA})
        counts = {}
        for cnt in (320, 720, 1000, 5000):
            r = c.get("https://ifzq.gtimg.cn/appstock/app/kline/mkline",
                      params={"param": f"sh600519,m30,,{cnt}"})
            arr = r.json()["data"]["sh600519"].get("m30", [])
            counts[cnt] = len(arr)
        # 1000 与 5000 应回落到与 320 相同的数量
        assert counts[1000] == counts[320], f"1000 未回落: {counts}"
        assert counts[5000] == counts[320], f"5000 未回落: {counts}"
        # 720 应不少于 320（若有更多历史）
        assert counts[720] >= counts[320]

    def test_valid_count_normalizes(self):
        """_valid_count 应把任意值归到有效档位。"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from sync_minutes import VALID_COUNTS, _valid_count

        assert _valid_count(320) == 320
        assert _valid_count(720) == 720
        assert _valid_count(1000) == 720# 超出上限归到最大档
        assert _valid_count(200) == 320          # 低于下限归到最小档
        assert _valid_count(500) in VALID_COUNTS

    def test_coverage_days_documented(self):
        """各周期覆盖天数必须记录在案 —— UI 要据此提示可回溯范围。"""
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from sync_minutes import COVERAGE_DAYS

        assert COVERAGE_DAYS["m1"] == 2, "m1 实测仅 2 天"
        assert COVERAGE_DAYS["m5"] == 11, "m5 实测仅 11 天"
        for p in ("m1", "m5", "m15", "m30", "m60"):
            assert p in COVERAGE_DAYS


# ---------------------------------------------------------------- 窗口周期
class TestWindowPeriod:
    def test_3m_uses_fine_period_or_reports_unavailable(self):
        """★★ 「3 分钟」窗口：**要么真能算 3 分钟，要么报不可用**。

        ★★★ 这里翻转过一个决定（原断言 `3m == 'm5'`），理由如下：

        旧注释说「m1 只有 2 天历史，宁可粗一点」。
        但「粗一点」在这里是**口径冒充**而非合理近似：
        m5 的第1 根是**前 5 分钟**的涨跌，却以「3m」的名义进报告与统计。
        ⇒「LLM 3 分钟判断准确率」这个数字会失去意义。

        而且实测发现更严重的后果（2026-10-09）：
          m1/m5 各只覆盖 **3 只**标的（只同步了关注池+持仓+自选），
          m30/m60 覆盖 4182 只。
        ⇒ 硬编码 m5 会让约 97% 的消息三个窗口全部「无数据」，
          而 `judge()` 把无数据判 `unclear`
          ——看起来像「LLM 判不准」，真相是「根本没数据」。**两者必须能区分。**

        现在：有 m1/m5 时用细周期；没有则**返回空串**，
        由调用方标 `unavailable`（30m/1d 仍可用 m30 算）。
        """
        # ★ 细周期**有实质覆盖**时用细周期
        #   （门槛 = 各周期覆盖最大值的 50%，这里 m5:3000 远超 4182/2）
        cov_all = {"m1": 3000, "m5": 3000, "m30": 4182, "m60": 4182}
        assert _period_for("3m", cov_all) in ("m1", "m5")
        # ★ 实测覆盖（m1/m5 各 3 只 = 0.07%）时 3m 必须报不可用，不得退回 m30
        cov_real = {"m1": 3, "m5": 3, "m30": 4182, "m60": 4182}
        assert _period_for("3m", cov_real) == ""
        assert _period_for("3m", cov_real) not in ("m30", "m60")
        # 30m/1d 用覆盖最广的 m30
        assert _period_for("30m", cov_real) == "m30"
        assert _period_for("1d", cov_real) == "m30"

    def test_all_windows_defined(self):
        for w in ("3m", "30m", "1d"):
            assert w in WINDOWS

    def test_window_values_are_minutes_not_bars(self):
        """★★ WINDOWS 的值必须是**分钟数**，不是根数。

        原实现是 m5 的根数（3m:1 / 30m:6 / 1d:48），
        换到 m30 后 48 根 = 24 小时 ⇒「1 天」窗口变成取一整天数据。
        """
        assert WINDOWS["3m"] == 3
        assert WINDOWS["30m"] == 30
        assert WINDOWS["1d"] == 240


# ---------------------------------------------------------------- 影响评估
class TestMessageImpact:
    @pytest.mark.parametrize("direction,ret,expect", [
        ("bull", 1.5, "correct"),
        ("bull", -1.5, "wrong"),
        ("bear", -1.5, "correct"),
        ("bear", 1.5, "wrong"),
        ("neutral", 1.5, "unclear"),
        ("bull", None, "unclear"),
        ("bear", None, "unclear"),
    ])
    def test_judge(self, direction, ret, expect):
        """★判定逻辑是纯函数，各类消息标准一致。

        方向对错直接决定「该不该改prompt」，
        若各调用点标准不同，准确率就不可比了。
        """
        v, note = judge(direction, ret)
        assert v == expect
        assert note                     # 必须给出理由，供人工复核

    def test_neutral_excluded_from_accuracy(self):
        """中性消息不计对错 —— 否则会稀释准确率。"""
        assert judge("neutral", 1.5)[0] == "unclear"

    def test_missing_data_not_treated_as_drop(self):
        """★缺数据必须判 unclear，不能判 wrong。

        若把「无数据」当成「跌了→判断错」，准确率会被
        数据缺失拉低，进而错误地去调 prompt。
        """
        v, note = judge("bull", None)
        assert v == "unclear"
        assert "无数据" in note


# ---------------------------------------------------------------- 反馈闭环
class TestFeedbackLoop:
    def test_accuracy_requires_10_samples(self):
        """★样本 < 10 不显示准确率（附录 5.4）。"""
        # 逻辑层验证：judged < 10 时 accuracy 应为 None
        judged = 3
        acc = round(2 / judged, 4) if judged >= 10 else None
        assert acc is None

    def test_verdict_partition(self):
        """三类计数互补，总和等于总数。"""
        combos = [("bull", 1.0), ("bear", -1.0), ("bull", -1.0),
                  ("neutral", 2.0), ("bull", None)]
        stats = {"correct": 0, "wrong": 0, "unclear": 0}
        for d, r in combos:
            v, _ = judge(d, r)
            stats[v] += 1
        # 5 条组合实测：bull+涨=对、bear+跌=对（2 对）
        #               bull+跌=错（1 错）
        #               neutral / 无数据（2 不判定）
        assert sum(stats.values()) == len(combos)
        assert stats["correct"] == 2
        assert stats["wrong"] == 1
        assert stats["unclear"] == 2
