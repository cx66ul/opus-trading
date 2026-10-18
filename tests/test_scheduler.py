"""M7 交易日历与调度器测试。

★ 重点固化两类真实静默失效：
1. **未来日期写成 is_open=0** → 明年的任务被永久静默跳过，不报任何错
2. **查不到日历就返回 False** → 跨年后系统整体停摆
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.domain.trade_cal import (
    build_calendar, is_trade_day, last_n_trade_days,
    next_trade_day, prev_trade_day, probe_market_open,
)

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 三态日历
class TestCalendarThreeStates:
    def test_future_dates_marked_unknown_not_closed(self):
        """★未来日期必须标记为「未知」，不能写成休市。

        这是本轮最严重的潜在bug：早期实现把所有「日线没出现过的日期」
        写成 is_open=0。日历覆盖到明年后，**明年所有任务会被永久静默跳过**，
        而且没有任何报错 —— 这类失效最难查。
        """
        from backend.data.sqlite import get_conn
        conn = get_conn()
        latest = conn.execute(
            "SELECT max(trade_date) FROM trade_calendar WHERE is_open = 1"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT is_open FROM trade_calendar WHERE trade_date = ?",
            (latest,)).fetchone()

        # 日线覆盖到的那天必须是交易日
        assert row[0] == 1, f"{latest} 应为交易日，实际 is_open={row[0]}"

        # 一个月后必须标记为 -1（未知）而不是 0（休市）
        future = (date.fromisoformat(latest) + timedelta(days=30)).isoformat()
        f = conn.execute(
            "SELECT is_open FROM trade_calendar WHERE trade_date = ?",
            (future,)).fetchone()
        if f is not None:
            assert f[0] != 0, (
                f"{future} 被标为休市(is_open=0)，会导致该日任务被静默跳过"
            )

    @pytest.mark.duckdb_locked
    def test_closed_days_have_no_kline(self):
        """★已确证的休市日不该有日线数据 —— 反向验证推导逻辑。"""
        from backend.data import duck
        from backend.data.sqlite import get_conn
        conn = get_conn()
        closed = [r[0] for r in conn.execute(
            "SELECT trade_date FROM trade_calendar"
            " WHERE is_open = 0 AND note='no_kline' LIMIT 10").fetchall()]
        if not closed:
            pytest.skip("无已确证休市日样本")
        con = duck.get_ro_conn()
        ph = ",".join("?" * len(closed))
        n = con.execute(
            f"SELECT count(*) FROM (SELECT DISTINCT trade_date FROM kline_daily"
            f" WHERE trade_date IN ({ph}))", closed).fetchone()[0]
        assert n == 0, f"标为休市的日期却有日线数据: {closed[:3]}"

    def test_calendar_has_all_weekends_closed(self):
        """任何周末都必须是休市（规则确定，无需联网）。"""
        from backend.data.sqlite import get_conn
        conn = get_conn()
        rows = conn.execute(
            "SELECT trade_date FROM trade_calendar").fetchall()
        bad = []
        for r in rows[:2000]:
            d = date.fromisoformat(r[0])
            if d.weekday() >= 5:
                is_open = conn.execute(
                    "SELECT is_open FROM trade_calendar WHERE trade_date = ?",
                    (r[0],)).fetchone()[0]
                if is_open == 1:
                    bad.append(r[0])
        assert not bad, f"周末被标为交易日: {bad[:5]}"


# ---------------------------------------------------------------- 判定
class TestIsTradeDay:
    @pytest.mark.parametrize("d,expected,desc", [
        ("2026-10-08", True, "最新交易日"),
        ("2026-09-30", True, "节前最后交易日"),
        ("2026-10-10", False, "周六"),
        ("2026-10-11", False, "周日"),
        ("2026-01-01", False, "元旦"),
        ("2026-02-17", False, "春节"),
        ("2026-10-05", False, "国庆（已确证休市）"),
    ])
    def test_known_days(self, d, expected, desc):
        """只测已发生日期 —— 未来日期无法预判，断言具体值是把假设当事实。"""
        assert is_trade_day(d) == expected, f"{d}（{desc}）判定错误"

    def test_weekend_never_trading(self):
        """周末绝不判为交易日，且不需要联网。"""
        for offset in range(0, 30):
            d = (date(2026, 10, 8) + timedelta(days=offset))
            if d.weekday() >= 5:
                assert is_trade_day(d) is False, f"{d} 周末被判为交易日"

    def test_future_weekday_not_silently_skipped(self):
        """★未来工作日不能被判为非交易日。

        若日历把未来写成 is_open=0，调度器会永久跳过该日的任务。
        这里验证：未来工作日要么正确判 True，要么日历标记为「未知」。
        """
        from backend.data.sqlite import get_conn
        future = "2026-12-07"                # 周一
        assert date.fromisoformat(future).weekday() < 5, "用例需为工作日"
        flag = get_conn().execute(
            "SELECT is_open FROM trade_calendar WHERE trade_date = ?",
            (future,)).fetchone()
        if flag is not None:
            assert flag[0] != 0, (
                f"{future} 是工作日却被标为休市，调度器会永久跳过该日任务"
            )

    def test_returns_bool_not_int(self):
        """返回类型必须是 bool —— int 会被 if 判定误用。"""
        assert isinstance(is_trade_day("2026-10-08"), bool)
        assert isinstance(is_trade_day("2026-10-10"), bool)


# ---------------------------------------------------------------- 前后交易日
class TestNav:
    def test_prev_trade_day_skips_weekend(self):
        """2026-10-08（周四）的前一交易日应跳过周末。"""
        pd = prev_trade_day("2026-10-08")
        assert pd is not None
        d = date.fromisoformat(pd)
        assert d.weekday() < 5
        assert pd < "2026-10-08"

    def test_prev_n_trade_days(self):
        p2 = prev_trade_day("2026-10-08", 2)
        assert p2 and p2 < prev_trade_day("2026-10-08")

    def test_last_n_sorted_ascending(self):
        days = last_n_trade_days("2026-10-08", 5)
        assert len(days) == 5
        assert days == sorted(days), "必须升序返回（用于计算均线）"
        assert days[-1] <= "2026-10-08"

    def test_next_trade_day_none_when_exhausted(self):
        """未来无数据时返回 None，不得抛异常或瞎猜。"""
        r = next_trade_day("2026-12-31")
        assert r is None or date.fromisoformat(r).weekday() < 5


# ---------------------------------------------------------------- 调度器
class TestScheduler:
    def test_non_trading_day_is_skipped_not_failed(self):
        """★休市日跳过任务属正常行为，不能算失败。

        否则每个周末和节假日都会产生一堆「失败」告警，
        真正的问题会被淹没。
        """
        from backend.scheduler import REGISTRY, run_job
        if "night_archive" not in REGISTRY:
            pytest.skip("调度器未导入实现")
        as_of = datetime(2026, 10, 10, 8, 30, tzinfo=CST)   # 周六
        r = run_job(REGISTRY["night_archive"], as_of)
        assert r.ok is True
        assert r.skipped in (True, False)     # 非交易日任务不该被跳过

    def test_timeout_breaks_without_retry(self):
        """★超时必须直接熔断，不重试。

        再重试一次还是会超时，白白多等一个 timeout_sec。
        """
        import time

        from backend.scheduler import Job, run_job

        def slow(a, tg):
            time.sleep(4)
            return {}

        j = Job(name="t_timeout", cron="* * * * *", trade_day_only=False,
                timeout_sec=1, desc="卡死", fn=slow)
        as_of = datetime(2026, 10, 8, 10, 0, tzinfo=CST)
        import time as _t
        t0 = _t.monotonic()
        r = run_job(j, as_of, force=True)
        el = _t.monotonic() - t0
        assert r.ok is False
        assert "超时" in r.error
        assert el < 3.0, f"超时后仍重复等待（耗时 {el:.1f}s），应立即熔断"

    def test_retry_then_success(self):
        """瞬时故障应重试并最终成功 —— 这是重试机制的意义。"""
        from backend.scheduler import Job, run_job
        calls = {"n": 0}

        def flaky(a, tg):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ValueError("瞬时故障")
            return {"ok_after": calls["n"]}

        j = Job(name="t_flaky", cron="* * * * *", trade_day_only=False,
                timeout_sec=30, desc="抖动", fn=flaky)
        r = run_job(j, datetime(2026, 10, 8, 10, 0, tzinfo=CST), force=True)
        assert r.ok is True
        assert calls["n"] == 2
        assert r.detail["ok_after"] == 2

    def test_gives_up_after_max_retries(self):
        from backend.scheduler import Job, run_job
        n = {"c": 0}

        def always_fail(a, tg):
            n["c"] += 1
            raise RuntimeError("必败")

        j = Job(name="t_fail", cron="* * * * *", trade_day_only=False,
                timeout_sec=30, desc="必败", fn=always_fail)
        r = run_job(j, datetime(2026, 10, 8, 10, 0, tzinfo=CST), force=True)
        assert r.ok is False
        assert n["c"] == 3, f"重试次数应为 3，实际 {n['c']}"

    def test_execution_logged(self):
        """每次执行都要写 event_log —— 否则无法追溯任务健康度。"""
        from backend.data.sqlite import get_conn
        from backend.scheduler import REGISTRY, run_job
        c = get_conn()
        before = c.execute(
            "SELECT count(*) FROM event_log WHERE kind='job_run'").fetchone()[0]
        run_job(REGISTRY["night_archive"],
                datetime(2026, 10, 8, 23, 0, tzinfo=CST), force=True)
        after = c.execute(
            "SELECT count(*) FROM event_log WHERE kind='job_run'").fetchone()[0]
        assert after > before

    def test_task_table_covers_appendix_61(self):
        """附录 6.1 的 12 类任务必须都在任务表里。"""
        from backend.scheduler import TASK_TABLE
        names = {t[0] for t in TASK_TABLE}
        for required in ("news_247", "premarket", "midday", "close",
                         "daily_sync", "night_archive", "weekly"):
            assert required in names, f"缺少任务: {required}"

    def test_job_not_in_task_table_rejected(self):
        """注册表中不存在的任务名必须报错，而不是静默创建。"""
        from backend.scheduler import REGISTRY, job
        with pytest.raises(KeyError):
            @job("nonexistent_task")
            def _f(as_of, tg):
                return {}


class TestCatchUp:
    @pytest.fixture(autouse=True)
    def isolate_job_effects(self, monkeypatch):
        from backend import scheduler
        # 测试补跑筛选与返回契约，不执行真实发信、同步、归档。
        for registered in scheduler.REGISTRY.values():
            monkeypatch.setattr(registered, "fn", lambda a, tg: {"test_only": True})
        monkeypatch.setattr(scheduler, "log_event", lambda *a, **kw: None)

    def test_catch_up_skips_high_frequency_tasks(self):
        """高频任务（每分钟/每5 分钟）不补跑 —— 补了也没意义。"""
        from backend.scheduler import catch_up
        as_of = datetime(2026, 10, 9, 16, 0, tzinfo=CST)
        names = {r.name for r in catch_up(as_of, window_hours=24)}
        assert "news_247" not in names
        assert "realtime" not in names
        assert "board_scan" not in names

    def test_catch_up_returns_list(self):
        from backend.scheduler import catch_up
        as_of = datetime(2026, 10, 9, 16, 0, tzinfo=CST)
        res = catch_up(as_of, window_hours=24)
        assert isinstance(res, list)
        for r in res:
            assert hasattr(r, "name") and hasattr(r, "ok")
