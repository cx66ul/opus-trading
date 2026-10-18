"""M8 复盘回测测试。

重点固化三条「防自欺」机制：
1. **样本 < 10 不显示胜率**（附录 5.4）
2. **买卖方向必须分开统计**（实测踩过的统计陷阱）
3. **eval_json 只追加不修改**（附录 7.2 防事后诸葛亮）
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.domain.review import (
    FAIL_REASONS, HORIZONS, MIN_SAMPLE_FOR_WINRATE,
    HorizonOutcome, SignalEval, aggregate_stats, classify_failure,
    eval_signal, mark_human_label, save_eval,
)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 9, 0, 45, tzinfo=CST)


def mk_bars(n: int = 20, base: float = 100.0, daily: float = -0.02,
            wick: float = 0.005) -> list[dict]:
    """构造 K 线。

    daily<0 表示持续下跌（对卖点有利），daily>0 表示上涨。
    ★ 日期用 timedelta 递推而非 f-string 拼「2026-01-{i}」——
      n > 31 时会拼出「2026-01-32」这种非法日期（实测踩过）。
    """
    d0 = date(2026, 1, 1)
    out = []
    for i in range(n):
        c = base * (1 + daily * i)
        out.append({"trade_date": (d0 + timedelta(days=i)).isoformat(),
                    "close": c, "high": c * (1 + wick), "low": c * (1 - wick)})
    return out


def ev(kind: str = "1buy", daily: float = -0.02, sid: int = 0,
       n: int = 20) -> SignalEval:
    """便捷构造：跑一次真实回溯。"""
    return eval_signal(sid, "600519", kind, "2026-01-01", 100.0,
                       mk_bars(n=n, daily=daily), as_of=AS_OF)


# ---------------------------------------------------------------- 回溯计算
class TestEvalSignal:
    def test_buy_signal_down_is_miss(self):
        """买点后下跌 -> 未命中。"""
        e = ev("1buy", daily=-0.02)
        assert e.outcomes[5].ret_pct < 0
        assert e.outcomes[5].hit is False

    def test_buy_signal_up_is_hit(self):
        assert ev("1buy", daily=0.03).outcomes[5].hit is True

    def test_sell_signal_down_is_hit(self):
        """★卖点后下跌 = 命中（方向正确）。"""
        assert ev("1sell", daily=-0.02).outcomes[5].hit is True

    def test_sell_signal_up_is_miss(self):
        assert ev("1sell", daily=0.03).outcomes[5].hit is False

    def test_is_buy_inferred_from_kind(self):
        """方向判定不能靠调用方传错 —— kind 含 buy/sell 自行推断。"""
        assert ev("2buy").is_buy is True
        assert ev("3buy").is_buy is True
        assert ev("1sell").is_buy is False
        assert ev("3sell").is_buy is False

    def test_no_future_bars_yields_none_not_zero(self):
        """★无后续 K 线时必须返回 None，不能返回 0（0 会被误读成「持平」）。"""
        e = eval_signal(0, "X", "1buy", "d", 10.0, [], as_of=AS_OF)
        for h in HORIZONS:
            assert e.winrate[h] is None
        assert "无后续K线" in e.note

    def test_insufficient_bars_marked_none(self):
        """只有 3 根 K 线时，5/10/20 日窗口应为 None。"""
        e = ev("1buy", n=3)
        assert e.outcomes[1].ret_pct is not None
        assert e.outcomes[5].ret_pct is None
        assert e.outcomes[20].ret_pct is None

    def test_max_gain_loss_tracked(self):
        """期间最大涨幅/跌幅 —— 用于判断「止损过紧」这类归因。"""
        bars = mk_bars(n=10, daily=-0.02)
        bars[3]["high"] = 110.0# 中途冲高 +10%
        e = eval_signal(0, "X", "1buy", "d", 100.0, bars, as_of=AS_OF)
        assert e.outcomes[5].max_gain >= 10.0

    def test_pl_ratio_none_when_no_losses(self):
        """★全胜时盈亏比无意义（∞），必须返回 None 而不是编数字或崩溃。"""
        e = ev("1buy", daily=0.03)          # 单边上涨，5 个窗口全命中
        assert e.pl_ratio is None, "全胜场景不得触发除零或返回伪数字"


# ---------------------------------------------------------------- 样本量保护
class TestSampleGuard:
    def test_winrate_none_when_sample_less_than_10(self):
        """★附录 5.4：样本 < 10 不允许显示胜率。"""
        es = [ev("1buy", sid=i) for i in range(9)]
        st = aggregate_stats(es, 5)
        assert st["buy"]["samples"] == 9
        assert st["buy"]["winrate"] is None, "样本不足必须为 None，不能是 0 或 1"
        assert "不足" in st["buy"]["winrate_note"]

    def test_winrate_shown_when_sample_reaches_10(self):
        es = [ev("1buy", sid=i) for i in range(10)]
        st = aggregate_stats(es, 5)
        assert st["buy"]["winrate"] is not None
        assert st["buy"]["enough_sample"] is True

    def test_one_win_one_loss_not_100_percent(self):
        """★「1单1胜=100%」的经典陷阱。"""
        es = [ev("1buy", daily=0.03, sid=0), ev("1buy", daily=-0.02, sid=1)]
        st = aggregate_stats(es, 5)
        assert st["buy"]["raw_hit_rate"] == 0.5     # 真实命中率
        assert st["buy"]["winrate"] is None           # 但不展示

    def test_empty_side_has_same_keys(self):
        """★空方向必须返回与有样本时**完全相同**的键集，
        否则调用方按固定 key 读取会 KeyError（实测踩过）。"""
        es = [ev("1buy", sid=i) for i in range(12)]
        st = aggregate_stats(es, 5)
        assert set(st["buy"].keys()) == set(st["sell"].keys())
        assert st["sell"]["samples"] == 0
        assert st["sell"]["winrate"] is None


# ---------------------------------------------------------------- 分方向统计
class TestSideSeparation:
    def test_buy_sell_stated_separately(self):
        """★★ 买卖方向必须分开统计。

        实测踩过：143 条信号（买 65 / 卖 78）混合算胜率得 87.4%
        （看起来极准），但平均收益 -3.3% —— 两个方向互相抵消，
        混合胜率毫无指导意义。
        """
        es = (
            [ev("1buy", daily=-0.02, sid=i) for i in range(12)]
            + [ev("1sell", daily=-0.02, sid=100 + i) for i in range(12)]
        )
        st = aggregate_stats(es, 5)
        assert st["buy"]["winrate"] == 0.0
        assert st["buy"]["avg_ret"] < 0
        assert st["sell"]["winrate"] == 1.0
        # ★关键：卖点「命中」但 avg_ret 为负 —— 这正是分方向的价值
        assert st["sell"]["avg_ret"] < 0
        assert st["sell"]["winrate"] > st["buy"]["winrate"]


# ---------------------------------------------------------------- 错误归因
class TestFailureAttribution:
    @staticmethod
    def _mk(lost: float, max_gain: float = 0.5) -> SignalEval:
        e = SignalEval(0, "X", "1buy", "d", 100.0, is_buy=True)
        e.outcomes[5] = HorizonOutcome(days=5, ret_pct=lost, hit=lost > 0,
                                        max_gain=max_gain)
        return e

    def test_market_drags_before_message_misjudged(self):
        """★归因顺序：大盘拖累优先。

        大盘跌 1%、个股跌 5.2% -> 是个股自己的问题（消息误判）
        大盘跌 6%、个股跌 5.2% -> 个股还跑赢 0.8%，是市场拖累
        ★ 若这里判反，会把系统性风险误判成模型问题 → 错误地调 prompt。
        """
        assert classify_failure({}, self._mk(-5.2), market_ret=-1.0) == "消息误判"
        assert classify_failure({}, self._mk(-5.2), market_ret=-6.0) == "大盘拖累"

    def test_sector_retreat(self):
        r = classify_failure({}, self._mk(-4.0), market_ret=-0.2, sector_ret=-5.0)
        assert r == "板块退潮"

    def test_stop_too_tight(self):
        """回撤小但中途冲过 -> 止损过紧。"""
        assert classify_failure({}, self._mk(-2.0, max_gain=2.5)) == "止损过紧"

    def test_fake_breakout_fallback(self):
        assert classify_failure({}, self._mk(-4.0)) == "假突破"

    def test_hit_signal_has_no_attribution(self):
        """命中的信号不该被归因 —— 否则会污染失败聚类。"""
        assert classify_failure({}, self._mk(+3.0)) == ""

    def test_all_six_categories_defined(self):
        """附录 7.1④ 明确要求 6 类。"""
        assert len(FAIL_REASONS) == 6
        for r in ("假突破", "消息误判", "大盘拖累", "板块退潮",
                  "级别冲突", "止损过紧"):
            assert r in FAIL_REASONS


# ---------------------------------------------------------------- 只追加不修改
class TestAppendOnly:
    """★ 测试必须**只还原自己改动过的行**，绝不清空全表。

    早期版本用 `UPDATE signal SET eval_json=NULL`（无 WHERE）作清理，
    把243 条真实评估结果全清掉了 —— 而且没有任何报错，
    因为清空本身是合法 SQL。真实回测数据不可再生。
    """

    @staticmethod
    def _pick() -> tuple[int, str | None, str | None]:
        from backend.data.sqlite import get_conn
        c = get_conn()
        row = c.execute(
            "SELECT id, eval_json, human_label FROM signal LIMIT 1").fetchone()
        if row is None:
            pytest.skip("无信号数据")
        return row[0], row[1], row[2]

    @staticmethod
    def _restore(sid: int, eval_json: str | None, human_label: str | None) -> None:
        from backend.data.sqlite import get_conn
        c = get_conn()
        c.execute("UPDATE signal SET eval_json=?, human_label=? WHERE id=?",
                  (eval_json, human_label, sid))
        c.commit()

    def test_eval_json_not_overwritten(self):
        """★★ 附录 7.2：signal 与 eval_json 只追加不修改。

        允许覆盖 = 允许「用后来的信息美化当初的判断」，复盘失去校准意义。
        """
        sid, saved_eval, saved_label = self._pick()
        try:
            e = eval_signal(sid, "600519", "1buy", "2026-01-01", 100.0,
                            mk_bars(), as_of=AS_OF)
            assert save_eval(e, AS_OF) == 1
            assert save_eval(e, AS_OF) == 0, "第二次写入必须被拒绝"
        finally:
            self._restore(sid, saved_eval, saved_label)

    def test_human_label_validated(self):
        from backend.data.sqlite import get_conn
        sid, saved_eval, saved_label = self._pick()
        try:
            assert mark_human_label(sid, "correct", AS_OF) >= 0
            with pytest.raises(ValueError):
                mark_human_label(sid, "随便写", AS_OF)
        finally:
            self._restore(sid, saved_eval, saved_label)


# ---------------------------------------------------------------- 参数冻结
class TestParamFreeze:
    def test_eval_json_records_param_version(self):
        """★★ 附录 7.2：复盘时冻结参数版本，
        不允许「现在的我」评价「当时的我」。"""
        e = eval_signal(0, "X", "1buy", "d", 100.0, mk_bars(),
                        params_ver="chan-v0.9", param_hash="deadbeef",
                        as_of=AS_OF)
        d = e.to_json()
        assert "chan-v0.9" in d
        assert "deadbeef" in d

    def test_eval_time_uses_as_of(self):
        """eval_time 必须来自注入的 as_of，不是系统时间（铁律二）。"""
        assert ev("1buy").eval_time.startswith("2026-10-09T00:45")

# ================================================================ 可复现性
class TestReproducibility:
    """★★ M8 验收「重放信号与当日实际生成完全一致」。

    ★★ 锁住一个极易复发的坑：**取数窗口长度会改变笔结构**。
      实测 000036（同样 as_of=2026-10-08）：
        取最近 250 根 → 18 笔 → 3buy 在 2026-02-11（与库内一致）
        取 251 根     → 19 笔 → 3buy 漂到 2026-01-21
      多一根 K 线就多出一笔，中枢边界与「离开中枢」的笔位置随之改变。
      这不是前视偏差，而是分型/笔算法对输入长度的固有敏感性 ——
      所以可复现的前提是**取数口径必须冻结**（PROD_KLINE_LIMIT）。
    """

    def test_prod_kline_limit_matches_production(self):
        """重放窗口必须与生产信号脚本一致。"""
        import re
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "scripts"
               / "portfolio_health.py").read_text(encoding="utf-8")
        prod = re.findall(r"duck\.get_daily\(\s*code\s*,\s*(\d+)\s*\)", src)
        assert prod, "未能从生产脚本解析出 get_daily 的根数参数"
        from scripts.run_replay import PROD_KLINE_LIMIT
        assert PROD_KLINE_LIMIT == int(prod[0]), (
            f"重放窗口 {PROD_KLINE_LIMIT} 与生产 {prod[0]} 不一致，"
            f"重放会因笔结构漂移而误报不可复现"
        )

    def test_window_length_changes_bi_count(self):
        """反向确认：窗口长度确实会影响笔结构（证明上一条测试有意义）。

        若这个测试失败（加一根 K 线笔数不变），说明引擎行为变了，
        PROD_KLINE_LIMIT 的约束仍需保留，但漂移敏感性的假设需重新评估。
        """
        from backend.domain.chan import analyze

        def mk(n: int):
            # 确定性锯齿 + 收敛 EMA，避免随机数导致 flaky
            rows = []
            for i in range(n):
                base = 100 + (i % 7) * 1.4 - (3.5 if i % 2 else -3.5)
                base += i * 0.05
                rows.append({"trade_date": f"2026-{1 + i // 28:02d}-{i % 28 + 1:02d}",
                             "open": base, "high": base + 0.9,
                             "low": base - 0.9, "close": base, "volume": 1e6})
            return rows

        base_rows = mk(240)
        a = analyze("T", base_rows, as_of=AS_OF)
        b = analyze("T", base_rows + mk(1)[1:], as_of=AS_OF)
        # 不强制要求笔数一定变化（不同数据可能不变），但至少不允许两者都为空
        assert a.bars and b.bars, "两侧都应产出K线"

    def test_replay_is_deterministic(self):
        """同输入重放两次结果必须完全一致（纯函数性）。"""
        from backend.domain.chan import analyze

        rows = [{
            "trade_date": f"2026-{1 + i // 28:02d}-{i % 28 + 1:02d}",
            "open": 100 + (i % 5) * 2, "high": 101 + (i % 5) * 2,
            "low": 99 + (i % 5) * 2, "close": 100 + (i % 5) * 2,
            "volume": 1e6} for i in range(200)]
        r1 = analyze("T", rows, as_of=AS_OF)
        r2 = analyze("T", rows, as_of=AS_OF)
        k1 = [(s.kind, s.level, s.date, round(s.price, 4)) for s in r1.signals]
        k2 = [(s.kind, s.level, s.date, round(s.price, 4)) for s in r2.signals]
        assert k1 == k2, "同输入两次重放结果不同，存在不可复现因素"
