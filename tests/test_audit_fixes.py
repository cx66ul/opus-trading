"""针对本轮审计发现的缺陷所加的回归测试。

每个用例对应一个已确认的 bug，注释里写明「原来错在哪」，
防止后续重构时被无声改回。
"""

from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.common import mailer
from backend.common.mailer import MailSender, in_quiet_hours, render_html_mail
from backend.domain import chan
from backend.domain.dsl import _p_ma_relation
from backend.domain.mail_templates import (
    MailContext,
    PositionRow,
    render_alert_html,
    render_midday_html,
    render_premarket_html,
)
from backend.scheduler import REGISTRY, TASK_TABLE, _in_cron_window, catch_up
from scripts.daily_recommend import _one_word_limits

CST = timezone(timedelta(hours=8))


# ================================================================ 缠论索引坐标
def _zigzag(n: int = 150, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    rows, px = [], 100.0
    for i in range(n):
        px *= 1 + rnd.uniform(-0.03, 0.03)
        rows.append({"trade_date": f"D{i + 1:03d}", "open": px,
                     "high": px * 1.01, "low": px * 0.99, "close": px,
                     "volume": 1000, "pct_chg": 0.0})
    return rows


def test_clean_bars_i0_is_list_index():
    """clean_bars 的 i0 必须是 bars 列表下标，不是原始 rows 下标。

    原来写成原始行下标，而 bars 已剔除停牌/脏数据行；两者一旦不等，
    下游 bars[a.i0 : b.i1+1] 整体错位。
    """
    rows = [{"trade_date": f"D{i}", "open": 10, "high": 11, "low": 9,
             "close": 10, "volume": 1} for i in range(10)]
    rows.insert(5, {"trade_date": "SUS", "open": 0, "high": 0, "low": 0,
                    "close": 0, "volume": 0})
    bars = chan.clean_bars(rows)
    assert len(bars) == 10
    for k, b in enumerate(bars):
        assert b.i0 == k, f"bars[{k}].i0={b.i0} 应与列表位置一致"
    # 原始行下标另存，未丢失
    assert bars[5].raw_idx == 6


def test_inserting_suspended_bar_does_not_change_structure():
    """插入一根停牌 bar，笔端点 / 背驰比值 / 信号必须完全不变。

    这是上面那个坐标 bug 的核心回归 —— 修好之前，仅插入一根停牌 bar
    就会让 14 根笔的端点日期与全部背驰比值漂移。
    """
    rows = _zigzag()
    as_of = date(2026, 10, 9)
    sus = dict(rows[60])
    sus.update({"high": 0, "low": 0, "close": 0, "open": 0})

    a = chan.analyze("T", [dict(r) for r in rows], as_of)
    b = chan.analyze("T", [dict(r) for r in rows[:60]] + [sus]
                     + [dict(r) for r in rows[60:]], as_of)

    assert len(a.bars) == len(b.bars) == 150
    bi_a = [(x.direction, a.bars[x.start_raw].d, a.bars[x.end_raw].d) for x in a.bis]
    bi_b = [(x.direction, b.bars[x.start_raw].d, b.bars[x.end_raw].d) for x in b.bis]
    assert bi_a == bi_b, "插入停牌 bar 不应改变任何笔的端点"

    assert [round(x.ratio, 6) for x in a.beichis] == \
           [round(x.ratio, 6) for x in b.beichis], "背驰比值不应漂移"
    assert [(s.kind, s.date) for s in a.signals] == \
           [(s.kind, s.date) for s in b.signals], "信号集合不应改变"


# ================================================================ DSL 谓词
class _MaCtx:
    def __init__(self, closes):
        self.closes = closes


def test_ma_relation_detects_cross_above():
    """ma_relation 曾经恒返回 None（守卫写成 i-slow<0，而 i 恒为负下标）。

    后果：任何 cross_above 规则永不命中，且只在 warning 里留一句「数据缺失」。
    """
    up = [10.0] * 13 + [22.0]        # 当日金叉
    dn = [20.0] * 13 + [8.0]         # 当日死叉
    assert _p_ma_relation(_MaCtx(up), {"fast": 5, "slow": 10,
                                       "op": "cross_above"})[0] is True
    assert _p_ma_relation(_MaCtx(dn), {"fast": 5, "slow": 10,
                                       "op": "cross_above"})[0] is False


def test_ma_relation_op_is_not_ignored():
    """op 曾经被忽略：cross_below 与 cross_above 返回同一个布尔量。"""
    up = [10.0] * 13 + [22.0]
    dn = [20.0] * 13 + [8.0]
    assert _p_ma_relation(_MaCtx(up), {"fast": 5, "slow": 10,
                                       "op": "cross_below"})[0] is False
    assert _p_ma_relation(_MaCtx(dn), {"fast": 5, "slow": 10,
                                       "op": "cross_below"})[0] is True


def test_ma_relation_missing_data_and_bad_params():
    """数据不足 / 参数非法时必须返回 None（判「数据缺失」），不得猜。"""
    assert _p_ma_relation(_MaCtx([10.0, 11.0]),
                          {"fast": 5, "slow": 10, "op": "cross_above"})[0] is None
    assert _p_ma_relation(_MaCtx([10.0] * 20),
                          {"fast": 10, "slow": 5, "op": "cross_above"})[0] is None


# ================================================================ 邮件渲染
def test_html_mail_keeps_last_cell():
    """render_html_mail 曾把「末格带色类」当行级标记而 cells[:-1] 丢弃。

    实测 (代码, 名称, 涨跌幅) 这种最常见形态会**丢掉涨跌幅**。
    """
    secs = [("持仓应对", [
        (("600519", "flat"), ("贵州茅台", "flat"),
         ("1255.79", "flat"), ("-0.22%", "down"), ("中枢上方", "flat")),
    ])]
    out = render_html_mail("t", secs, datetime(2026, 10, 9, 9, 0, tzinfo=CST))
    row = re.search(r"<tr[^>]*>(.*?)</tr>", out, re.S).group(1)
    assert row.count("<td") == 5, "五个单元格一个都不能少"
    assert "-0.22%" in row
    assert "中枢上方" in row


def test_html_mail_string_row_is_one_cell():
    """整行字符串不得被当可迭代对象逐字符拆开。"""
    out = render_html_mail("t", [("头", [("美股：纳指 +0.82%",)])],
                           datetime(2026, 10, 9, 9, 0, tzinfo=CST))
    row = re.search(r"<tr[^>]*>(.*?)</tr>", out, re.S).group(1)
    assert row.count("<td") == 1
    assert "美股：纳指 +0.82%" in row


# ================================================================ 邮件模板 None
def test_mail_templates_survive_none_fields():
    """market_score / cost / price 为 None 时不得抛格式化异常。

    数据缺失的时刻恰恰是最需要收到邮件的时候 —— 原实现会整封渲染失败。
    """
    ctx = MailContext(as_of=datetime(2026, 10, 9, 9, 0, tzinfo=CST),
                      kind="premarket")
    ctx.positions = [PositionRow(code="600519", name="贵州茅台", cost=None,
                                 price=None, pnl_pct=None, health=None,
                                 structure="", advice="")]
    assert len(render_premarket_html(ctx)) > 0
    ctx.kind = "midday"
    assert len(render_midday_html(ctx)) > 0


@pytest.mark.parametrize("kind,fn", [
    ("premarket", render_premarket_html),
    ("midday", render_midday_html),
    ("alert", render_alert_html),
])
def test_mail_templates_survive_empty_context(kind, fn):
    """完全空的上下文也必须能渲染出邮件（全占位符形态）。"""
    out = fn(MailContext(as_of=datetime(2026, 10, 9, 12, 0, tzinfo=CST),
                         kind=kind))
    assert "Hawk" in out


# ================================================================ 免打扰 / 重试
def test_quiet_hours_boundaries():
    for hhmm, want in [("22:59", False), ("23:00", True), ("00:30", True),
                       ("07:29", True), ("07:30", False), ("09:00", False)]:
        h, m = map(int, hhmm.split(":"))
        ts = datetime(2026, 10, 9, h, m, tzinfo=CST)
        assert in_quiet_hours(ts) is want, hhmm


def test_retry_delays_match_spec():
    """规格 §5.3 要求 30s/120s/300s，原为 2**attempt（2s/4s）。"""
    assert mailer.RETRY_DELAYS == (30, 120, 300)


def test_send_skips_in_quiet_hours(monkeypatch, tmp_path):
    """免打扰跳过要留痕，且不得真的连 SMTP。"""
    logged: list = []
    monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn(logged))
    monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: True)

    m = MailSender.__new__(MailSender)     # 跳过读密钥库
    m.cfg = type("C", (), {"to": ["a@b.c"], "sender": "a@b.c",
                           "user": "a@b.c", "auth_code": "x",
                           "host": "h", "port": 465, "use_ssl": True,
                           "timeout": 1.0})()
    res = m.send("s", "<p>h</p>", "t",
                 datetime(2026, 10, 9, 23, 30, tzinfo=CST), kind="premarket")
    assert res["ok"] is False and res["skipped"] is True
    assert any("skipped" in str(x) for x in logged), "跳过也要写 mail_log"


def test_send_urgent_pierces_quiet_hours(monkeypatch):
    """urgent=True 必须穿透免打扰（重大利空不能等到早上）。"""
    monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn([]))

    class S:
        def __init__(self, *a, **k):
            raise RuntimeError("stop-here")

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", S)

    m = MailSender.__new__(MailSender)
    m.cfg = type("C", (), {"to": ["a@b.c"], "sender": "a@b.c", "user": "a@b.c",
                           "auth_code": "x", "host": "h", "port": 465,
                           "use_ssl": True, "timeout": 1.0})()
    res = m.send("s", "<p>h</p>", "t",
                 datetime(2026, 10, 9, 23, 30, tzinfo=CST),
                 kind="alert", retry=1, urgent=True)
    # 走到连接阶段说明没有被免打扰挡下
    assert res["ok"] is False and not res.get("skipped")


class _FakeConn:
    """★ 2026-10-10：判重查询上线后，桩必须**按 SQL 类型分派**。

    原桩对所有语句都返回同一个「成功游标」，判重的 `SELECT` 因此
    被当成「查到了」⇒ `fetchone()` 缺失直接 AttributeError（更糟的是
    若桩返回真值，首发就会被判重跳过）。⇒ SELECT 一律返回「查不到」。
    """

    def __init__(self, sink):
        self.sink = sink
        self._n = 0

    def execute(self, sql, args=()):
        self.sink.append((sql, args))
        cur = type("R", (), {})()
        cur.fetchone = lambda: None
        cur.fetchall = lambda: []
        if sql.lstrip().upper().startswith("SELECT"):
            cur.lastrowid = 0
            cur.rowcount = 0
            return cur
        self._n += 1
        cur.lastrowid = self._n
        cur.rowcount = 1
        return cur

    def commit(self):
        pass


# ================================================================ 调度器 cron
def test_every_task_has_implementation_or_is_known_gap():
    """TASK_TABLE 与 REGISTRY 的差集必须是已知缺口，且 catch_up 不得因此崩。

    原来 catch_up 直接 REGISTRY[name]，五个未实现任务会 KeyError 中断整轮补齐。

    ★ 更新记录（2026-10-10）：`midday` 与 `close` 已补上@job 实现
      （配套 `backend/domain/digest.py` 装配层 + `mail_templates.send`
      的 retry 透传）。剩余缺口缩小为 3 个。
      这条断言的价值正在于此：缺口集合一旦**静默变化**
      （有人加了任务却没实现、或忘了更新清单）就会失败。
    """
    missing = {t[0] for t in TASK_TABLE} - set(REGISTRY)
    # ★ 2026-10-10 更新：`auction`/`ref_sync`/`weekly` 已补齐，缺口归零。
    assert missing == set(), (
        f"未实现任务集合变了：{sorted(missing)}；"
        f"新增任务请同时补 @job 实现并更新此处"
    )


def test_catch_up_does_not_raise_for_missing_impl(monkeypatch, tmp_path):
    monkeypatch.setenv("HAWK_DATA_DIR", str(tmp_path))
    for t in ("11:05", "15:05", "20:30", "10:30"):
        h, m = map(int, t.split(":"))
        res = catch_up(datetime(2026, 10, 9, h, m, tzinfo=CST))
        assert isinstance(res, list)


def test_cron_window_respects_dow_and_hours():
    """catch_up 的时间窗曾退化为 hh == 当前小时，且完全忽略星期字段。"""
    wk = datetime(2026, 10, 9, 10, 30, tzinfo=CST)     # 周五
    sa = datetime(2026, 10, 10, 10, 30, tzinfo=CST)    # 周六
    assert _in_cron_window("0 10 * * 6", wk - timedelta(hours=24), wk) is False
    assert _in_cron_window("0 10 * * 6", sa - timedelta(hours=24), sa) is True
    # 跨夜：周六 07:00 应能补到周五 08:30 的盘前任务
    sat_early = datetime(2026, 10, 10, 7, 0, tzinfo=CST)
    assert _in_cron_window("30 8 * * 1-5",
                           sat_early - timedelta(hours=24), sat_early) is True


def test_auction_cron_is_not_day_of_month():
    """auction 的 cron 曾写成 `*/1 9 9 * 1-5` —— 第 3 段是 day-of-month=9，
    语义变成「每月 9 号」，而非「每个交易日」。"""
    cron = dict((t[0], t[1]) for t in TASK_TABLE)["auction"]
    assert cron.split()[2] == "*", f"day-of-month 应为 *，实为 {cron!r}"


# ================================================================ 一字板
def test_one_word_board_detection():
    """一字板 = O==H==L==C 且收盘价 == 涨停价（按板块限幅）。

    该字段曾被硬编码 False，使「一字板买不进」这条硬性过滤永久失效。
    """
    d0, d1 = date(2026, 10, 8), date(2026, 10, 9)

    def bar(d, o, h, low, c):
        return {"trade_date": d, "open": o, "high": h, "low": low, "close": c}

    # 主板：前收 10.00 -> 涨停 11.00
    sec = {d0: bar(d0, 10.0, 10.2, 9.9, 10.0)}
    assert _one_word_limits(
        {"600001": [sec[d0], bar(d1, 11.0, 11.0, 11.0, 11.0)]}, d1
    ) == {"600001": True}
    # 普通涨停（盘中开过板）不是一字板
    assert _one_word_limits(
        {"600002": [sec[d0], bar(d1, 10.3, 11.0, 10.1, 11.0)]}, d1
    ) == {}
    # 创业板限幅 20%：收 11.9 未到 12.00，不算涨停
    assert _one_word_limits(
        {"300001": [sec[d0], bar(d1, 10.5, 11.9, 10.5, 11.9)]}, d1
    ) == {}
    # 创业板一字板：前收 10.00 -> 涨停 12.00
    assert _one_word_limits(
        {"300002": [sec[d0], bar(d1, 12.0, 12.0, 12.0, 12.0)]}, d1
    ) == {"300002": True}
