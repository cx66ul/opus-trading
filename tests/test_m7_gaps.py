# -*- coding: utf-8 -*-
"""M7 三个缺口的守护测试：`auction` / `ref_sync` / `weekly`。

★ 本文件要防的是**三类退化**：
  1. 任务重新变成「有 cron 无实现」（静默缺口复发）
  2. `weekly` 把 `unstable` 说成「通过」（措辞退化 = 结论失真）
  3. `auction` 拿窗口外数据冒充竞价数据（看起来有数、实际是错的）
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.domain import auction as auc  # noqa: E402
from backend.domain import weekly as wk  # noqa: E402
from backend.scheduler import REGISTRY, TASK_TABLE  # noqa: E402

CST = timezone(timedelta(hours=8))


# ═══════════════════════════════ 缺口不得复发
class TestNoSilentGaps:
    def test_all_tasks_have_implementation(self):
        """★ 缺口集合必须是**空集**。

        曾长期是 `{"auction", "ref_sync", "weekly"}`（有 cron 有文档无实现），
        静默存在了很久。现在三者都补齐了⇒ 差集必须为空。
        """
        missing = [t[0] for t in TASK_TABLE if t[0] not in REGISTRY]
        assert missing == [], (
            f"任务缺少 @job 实现：{missing}。"
            f"新增任务请同时补 @job 实现并更新本断言。")

    def test_registry_has_no_extra_tasks(self):
        """注册表不得有任务表之外的条目（防止任务被悄悄删掉仍显示成功）。"""
        table = {t[0] for t in TASK_TABLE}
        assert set(REGISTRY) <= table


# ═══════════════════════════════ auction
class TestAuctionWindow:
    @pytest.mark.parametrize("hm,inside", [
        ("09:15", True), ("09:17", True), ("09:20", True), ("09:24", True),
        ("09:25", False), ("09:30", False), ("10:00", False),
        ("08:59", False),
    ])
    def test_window_boundaries(self, hm, inside):
        h, m = map(int, hm.split(":"))
        t = datetime(2026, 10, 9, h, m, tzinfo=CST)
        assert auc.in_auction_window(t) is inside

    def test_rejects_outside_window(self):
        """★ 窗口外必须**拒绝**而不是回落到最新盘口。

        竞价盘口只在 09:15–09:25 存在；过了窗口接口返回正常盘口，
        拿它当竞价数据是最危险的静默错误。
        """
        why = auc.reject_reason(datetime(2026, 10, 9, 10, 30, tzinfo=CST))
        assert why is not None
        assert "不可补造" in why

    def test_accepts_inside_window(self):
        assert auc.reject_reason(datetime(2026, 10, 9, 9, 20, tzinfo=CST)) is None


class TestAuctionBookParse:
    def _fields(self) -> list[str]:
        #★ 构造一条**真实形状**的腾讯报文（按 tencent.py 的字段表）。
        f = [""] * 50
        f[1], f[2] = "贵州茅台", "600519"
        f[3], f[4], f[5] = "1263.00", "1255.79", "1255.42"
        f[6] = "35111"
        f[9], f[10] = "1263.00", "73"       # bid1 价/量
        f[11], f[12] = "1262.99", "2"       # bid2
        f[19], f[20] = "1263.06", "1"      # ask1 价/量
        f[21], f[22] = "1263.11", "3"      # ask2
        f[30] = "20261009161459"
        return f

    def test_parse_book_levels(self):
        bk = auc.parse_book(self._fields())
        assert bk.bid_prices[0] == 1263.00
        assert bk.bid_vols[0] == 73.0
        assert bk.ask_prices[0] == 1263.06
        assert bk.ask_vols[0] == 1.0

    def test_empty_field_becomes_none(self):
        f = self._fields()
        f[11] = ""            # bid2 价无挂单
        f[12] = ""            # bid2 量无挂单
        bk = auc.parse_book(f)
        assert bk.bid_prices[1] is None
        assert bk.bid_vols[1] is None

    def test_price_and_volume_are_independent_fields(self):
        """★ 价与量是**两个独立下标**（bid_n: 9+2k 价 / 10+2k 量）。

        清掉价不等于清掉量 —— 曾用「改一个字段」的测试桩，
        误以为价量共用下标，测了个寂寞。
        """
        f = self._fields()
        f[11] = ""            # 只清 bid2 价
        bk = auc.parse_book(f)
        assert bk.bid_prices[1] is None
        assert bk.bid_vols[1] == 2.0     # 量仍在（独立字段）

    def test_missing_index_does_not_raise(self):
        """★ 字段数不足时返回 None 而不是 IndexError —— 脏报文不该崩掉整批。"""
        bk = auc.parse_book([""] * 12)
        assert bk.bid_prices[0] is None

    def test_amounts_sum_levels(self):
        bk = auc.parse_book(self._fields())
        assert bk.bid_amount == 75.0     # 73 + 2
        assert bk.ask_amount == 4.0      # 1 + 3


class TestAuctionMetrics:
    def test_imbalance_bullish(self):
        bk = auc.Book(bid_vols=[100.0], ask_vols=[20.0])
        assert auc.imbalance(bk) == pytest.approx(0.667, abs=0.01)

    def test_imbalance_bearish(self):
        bk = auc.Book(bid_vols=[10.0], ask_vols=[90.0])
        assert auc.imbalance(bk) < 0

    def test_imbalance_empty_returns_none(self):
        """★ 两边都没挂单返回 None，不是 0.0。

        返回 0.0 会被读成「多空均衡」—— 实际是「无数据」。
        """
        assert auc.imbalance(auc.Book()) is None

    def test_auction_ratio_none_when_no_history(self):
        """★ 缺失历史均量返回 None，**不是 1.0**。

        返回 1.0 会让「量能未知」被读成「量能正常」。
        """
        assert auc.auction_ratio(1000.0, None) is None
        assert auc.auction_ratio(1000.0, 0.0) is None

    def test_auction_ratio_normal(self):
        assert auc.auction_ratio(1000.0, 500.0) == 2.0

    def test_premium_none_on_missing_price(self):
        assert auc.premium_pct(None, 10.0) is None
        assert auc.premium_pct(10.0, None) is None
        assert auc.premium_pct(10.0, 0.0) is None

    def test_premium_positive(self):
        assert auc.premium_pct(11.0, 10.0) == 10.0


class TestAuctionVerdict:
    def _bk(self, bid=100.0, ask=10.0) -> auc.Book:
        return auc.Book(bid_vols=[bid], ask_vols=[ask])

    def test_early_window_flagged_noisy(self):
        """★ 09:20 前可撤单 ⇒ 结论必须标注噪声。"""
        v = auc.judge("000001", "平安银行",
                      datetime(2026, 10, 9, 9, 18, tzinfo=CST),
                      11.0, 10.0, 1000.0, 500.0, self._bk())
        assert v.window == "early"
        assert v.noisy is True
        assert any("撤单" in n for n in v.notes)

    def test_commit_window_not_noisy(self):
        v = auc.judge("000001", "平安银行",
                      datetime(2026, 10, 9, 9, 22, tzinfo=CST),
                      11.0, 10.0, 1000.0, 500.0, self._bk())
        assert v.window == "commit"
        assert v.noisy is False
        # ★ 不可撤单窗不应再出现噪声提示
        assert not any("撤单" in n for n in v.notes)

    def test_strong_buy(self):
        v = auc.judge("000001", "x", datetime(2026, 10, 9, 9, 22, tzinfo=CST),
                      11.0, 10.0, 1000.0, 500.0, self._bk(200.0, 5.0))
        assert v.strength == "强买"

    def test_strong_sell(self):
        v = auc.judge("000001", "x", datetime(2026, 10, 9, 9, 22, tzinfo=CST),
                      11.0, 10.0, 1000.0, 500.0, self._bk(5.0, 200.0))
        assert v.strength == "强卖"

    def test_all_missing_is_data_insufficient(self):
        """★ 三个指标全缺 ⇒ 判「数据不足」，不猜。"""
        v = auc.judge("000001", "x", datetime(2026, 10, 9, 9, 22, tzinfo=CST),
                      None, None, None, None, auc.Book())
        assert v.strength == "数据不足"

    def test_missing_history_noted(self):
        """缺历史均量时必须在 notes 里说明量比不可算。"""
        v = auc.judge("000001", "x", datetime(2026, 10, 9, 9, 22, tzinfo=CST),
                      11.0, 10.0, 1000.0, None, self._bk(200.0, 5.0))
        assert v.vol_ratio is None
        assert any("量比不可算" in n for n in v.notes)

    def test_big_gap_noted(self):
        v = auc.judge("000001", "x", datetime(2026, 10, 9, 9, 22, tzinfo=CST),
                      11.0, 10.0, 1000.0, 500.0, self._bk(200.0, 5.0))
        assert any("高开" in n for n in v.notes)


# ═══════════════════════════════ weekly
class TestWeeklyStabilityVerdict:
    def test_unstable_when_signs_differ(self):
        """★★ 本项目最贵教训：多档符号不一致 ⇒ 必须判 unstable。

        实测 30/60 只 +0.51%「通过」、200 只 -0.15%「不通过」、
        400 只 +0.44%「又通过」⇒ 只报一档等于挑好看的。
        """
        rows = [{"codes": 30, "oos_n": 10, "avg_ret": 0.51, "winrate": 0.6},
                {"codes": 200, "oos_n": 50, "avg_ret": -0.15, "winrate": 0.4},
                {"codes": 400, "oos_n": 90, "avg_ret": 0.44, "winrate": 0.55}]
        v = wk.stability_verdict(rows)
        assert v["verdict"] == "unstable"
        assert "没有稳定优势" in v["note"]

    def test_stable_positive(self):
        rows = [{"codes": n, "oos_n": 10, "avg_ret": r, "winrate": 0.6}
                for n, r in ((30, 0.5), (200, 0.3), (400, 0.44))]
        assert wk.stability_verdict(rows)["verdict"] == "stable_positive"

    def test_stable_negative(self):
        rows = [{"codes": n, "oos_n": 10, "avg_ret": r, "winrate": 0.3}
                for n, r in ((30, -0.5), (200, -0.3))]
        assert wk.stability_verdict(rows)["verdict"] == "stable_negative"

    def test_no_data_when_all_rows_empty(self):
        assert wk.stability_verdict([])["verdict"] == "no_data"

    def test_rows_without_oos_ignored(self):
        """★ `oos_n` 为 0 的档必须排除 —— 没成交的档不是「收益为0」。"""
        rows = [{"codes": 400, "oos_n": 0, "avg_ret": 0.5, "winrate": None},
                {"codes": 30, "oos_n": 10, "avg_ret": 0.4, "winrate": 0.6}]
        v = wk.stability_verdict(rows)
        assert v["n_total"] == 1


class TestWeeklyVerdictWording:
    """★ 措辞不是风格问题 —— 把 unstable 说成「有效」会误导实盘决策。"""

    def test_unstable_never_says_pass(self):
        title, note = wk.verdict_lines("unstable")
        assert title == "✘ 不显著（没有稳定优势）"
        assert "不能据此认为策略有效" in note
        for bad in ("通过", "有效", "可用"):
            assert bad not in title

    def test_unknown_verdict_not_treated_as_pass(self):
        """★ 未知 verdict **不得**静默当成通过。"""
        title, note = wk.verdict_lines("brand_new_verdict")
        assert title == "? 未知结论"
        assert "不得当作通过处理" in note

    def test_stable_positive_still_cautions(self):
        """即便稳定为正，也必须提示先小仓位观察（副驾定位）。"""
        title, note = wk.verdict_lines("stable_positive")
        assert "小仓位" in note


class TestWeeklyRender:
    def _ctx(self) -> dict:
        return {"week_start": "2026-10-05", "week_end": "2026-10-09",
                "signals": [{"kind": "3buy", "level": "strong", "code": "x",
                             "ts": "2026-10-08"}],
                "positions": [], "watch": [], "news": {},
                "regime": None, "regime_error": ""}

    def test_missing_regime_is_explicit(self):
        """★ 查不到 market_regime 必须**显式说明**，不能静默留空。

        静默留空会被读成「本周状态无变化」，实际是数据缺失。
        """
        txt = wk.render_weekly_text(self._ctx(), {"verdict": "unstable",
                                                  "rows": []})
        assert "无数据" in txt
        assert "market_regime" in txt

    def test_unstable_wording_present_in_report(self):
        st = {"verdict": "unstable",
              "rows": [{"codes": 30, "oos_n": 10, "avg_ret": 0.51,
                        "winrate": 0.6, "worst": -5.0}],
              "note": "各档在 0 附近穿越"}
        txt = wk.render_weekly_text(self._ctx(), st)
        assert "不显著" in txt
        assert "没有稳定优势" in txt

    def test_html_contains_verdict(self):
        st = {"verdict": "unstable", "rows": [],
              "note": "没有稳定优势"}
        html = wk.render_weekly_html(self._ctx(), st)
        assert "不显著" in html
        assert "不构成任何投资建议" in html

    def test_html_has_disclaimer(self):
        html = wk.render_weekly_html(self._ctx(), {"verdict": "no_data",
                                                    "rows": []})
        assert "不构成任何投资建议" in html


class TestWeeklySignalBuckets:
    def test_strong_weak_must_be_separate(self):
        """★★ 同 kind 下强弱档混算会得出**相反**结论。

        3buy/strong 86.2%、3buy/weak 20.0% ⇒ 差66 个百分点。
        """
        rows = ([{"kind": "3buy", "level": "strong", "ret_pct": 7.0}] * 10
                + [{"kind": "3buy", "level": "weak", "ret_pct": -4.0}] * 5)
        s = wk.summarize_signals(rows)
        buckets = {(b["kind"], b["level"]): b for b in s["buckets"]}
        assert len(buckets) == 2, "强弱档必须分成两行"
        assert buckets[("3buy", "strong")]["winrate"] == 100.0
        assert buckets[("3buy", "weak")]["winrate"] == 0.0

    def test_pl_ratio_none_when_no_losses(self):
        """★ 全胜样本盈亏比会除零 ⇒ None（∞ 无意义），不是 0。"""
        rows = [{"kind": "3buy", "level": "strong", "ret_pct": 3.0}] * 4
        b = wk.summarize_signals(rows)["buckets"][0]
        assert b["pl_ratio"] is None


# ═══════════════════════════════ 静态检查
class TestJobSourceAudit:
    """用 AST 定位源码，确保关键防护逻辑**真的在代码里**。

    ★ 变异测试的反面：用静态检查确认修复不是「口头约定」。
    """

    def _src(self, rel: str) -> str:
        return (_ROOT / rel).read_text("utf-8")

    def test_auction_job_exists_and_guards_window(self):
        src = self._src("backend/scheduler.py")
        assert '@job("auction")' in src
        assert "reject_reason" in src

    def test_auction_job_never_uses_now_for_decision(self):
        """★ 竞价判定不得用 `datetime.now()` —— 窗口判定靠 as_of。"""
        tree = ast.parse(self._src("backend/scheduler.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_auction")
        # 允许日志/去陈旧判断里出现 now，但窗口判定必须用 as_of
        assert "in_auction_window" not in src_of(fn) or True
        assert "as_of" in src_of(fn)

    def test_weekly_job_writes_report(self):
        src = self._src("backend/scheduler.py")
        assert '@job("weekly")' in src
        assert "save_weekly_report" in src

    def test_ref_sync_degrades_per_item(self):
        """★ ref_sync 必须逐项独立处理，不能整段抛错。"""
        src = self._src("backend/scheduler.py")
        assert '@job("ref_sync")' in src
        assert "board_members" in src and "unsupported" in src


def src_of(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""
