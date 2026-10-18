# -*- coding: utf-8 -*-
"""邮件去重（2026-10-10 修复的真 bug）

★ 起因是实测 `mail_log` 里的**真实数据**：
  **2026-10-09 11:35 的 midday 邮件被发了 35 次，全部 status=sent。**
  根因：`MailSender.send` 只**写** mail_log、**从不查**是否已发过；
  再叠加 `scheduler.catch_up()` 在补跑时重新触发 ⇒ 重复放大。

★ 附带修掉：`body_hash` 原本是 `f"{len(html_body)}b"` ——一个**长度字符串**
  而不是哈希。同长度不同正文会撞车、不同长度同一正文判不出⇒
  即便下游补查重逻辑，用它当键也必然出错。

这些用例全部用**临时 sqlite**（monkeypatch 打桩），不碰真实库。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))


class _MailDb:
    """临时库句柄。`.conn` 是**与被测代码同一个**连接对象，
    否则断言读到的是另一个连接的未提交视图。"""

    def __init__(self, path, conn):
        self.path = path
        self.conn = conn

    def count_sent(self) -> int:
        return self.conn.execute(
            "select count(*) from mail_log where status='sent'").fetchone()[0]


@pytest.fixture
def mail_db(tmp_path, monkeypatch):
    """把 mailer 的 sqlite 连接打桩到临时库，并预建mail_log 表。

    ⚠ 必须返回**同一个连接对象**：若写成 `lambda: sqlite3.connect(...)`，
      每次调用都是**新连接** ⇒ send() 里写了数据、测试断言时另开连接读，
      看不到未提交的事务 ⇒ 表现为「发送成功但一条记录都没有」。
      （这正是「测出来的和真跑的不一样」的典型来源。）
    """
    from backend.common import mailer

    p = tmp_path / "mail.sqlite"
    db = sqlite3.connect(str(p))
    db.execute("""
        CREATE TABLE mail_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, kind TEXT, subject TEXT, body_hash TEXT,
            status TEXT, err TEXT, retry INTEGER DEFAULT 0)
    """)
    db.commit()

    import backend.data.sqlite as sqlite_mod
    monkeypatch.setattr(mailer, "get_conn", lambda: db)
    yield _MailDb(p, db)
    db.close()


def _sender(monkeypatch, mail_db):
    """造一个不真正连 SMTP 的 MailSender（只测去重逻辑）。

    ⚠ 不能只 `__new__` 绕过 `__init__` —— 那样 `self.cfg` 不存在，
      `send()` 里访问 `self.cfg.sender` 会抛 AttributeError，
      表现为「ok=False」而**看不出是测试自身的问题**（本轮真踩过）。
      正确做法：显式装一个假的 cfg 对象。
    """
    from backend.common import mailer

    m = mailer.MailSender.__new__(mailer.MailSender)

    class _Cfg:
        sender = "hawk@example.com"
        to = ["me@example.com"]
        host = "smtp.example.com"
        port = 465
        user = "hawk@example.com"
        auth_code = "stub"
        use_ssl = True
        timeout = 5

    m.cfg = _Cfg()
    sent: list[str] = []

    def fake_connect(self):
        class S:
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False
            def login(self_inner, *a):
                return None
            def sendmail(self_inner, f, t, s):
                sent.append(s)
        return S()

    monkeypatch.setattr(mailer.MailSender, "_connect", fake_connect)
    return m, sent


class TestContentHash:
    def test_same_body_same_hash(self):
        from backend.common.mailer import content_hash
        assert content_hash("<html>abc</html>") == content_hash("<html>abc</html>")

    def test_different_body_different_hash(self):
        from backend.common.mailer import content_hash
        assert content_hash("<html>abc</html>") != content_hash("<html>abd</html>")

    def test_old_length_based_hash_would_collide(self):
        """★ 反证旧实现为什么不能用：同长度不同正文在旧判据下无法区分。"""
        a, b = "<html>abc</html>", "<html>abd</html>"
        assert len(a) == len(b)                      # 旧判据：撞车
        from backend.common.mailer import content_hash
        assert content_hash(a) != content_hash(b)    # 新判据：可区分

    def test_hash_is_pure_no_random_salt(self):
        """★ 纯函数（铁律二），且**不含盐** —— 加盐会让跨进程判重永久失效。"""
        from backend.common.mailer import content_hash
        assert content_hash("x") == content_hash("x")

    def test_hash_is_hex_and_short(self):
        from backend.common.mailer import content_hash
        h = content_hash("hello")
        assert len(h) == 16
        assert all(ch in "0123456789abcdef" for ch in h)


class TestMailDedup:
    def test_second_identical_send_is_deduped(self, mail_db, monkeypatch):
        """★★ 核心行为：同日同内容第二次必须被判重跳过。"""
        from backend.common.mailer import MailSender
        m, sent = _sender(monkeypatch, mail_db)
        as_of = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        kw = dict(kind="midday", retry=1)

        r1 = m.send("Hawk 午间策略", "<html>A</html>", "A", as_of, **kw)
        assert r1["ok"] is True
        r2 = m.send("Hawk 午间策略", "<html>A</html>", "A", as_of, **kw)

        assert r2["deduped"] is True
        assert r2["skipped"] is True
        assert len(sent) == 1, "SMTP 只应真正发送一次"
        assert r2["id"] is None, "判重时不应写入新记录"

    def test_force_bypasses_dedup(self, mail_db, monkeypatch):
        """★ force=True（用户主动重发）必须能穿透去重。"""
        m, sent = _sender(monkeypatch, mail_db)
        as_of = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        kw = dict(kind="midday", retry=1, force=True)
        m.send("Hawk 午间策略", "<html>A</html>", "A", as_of, **kw)
        r2 = m.send("Hawk 午间策略", "<html>A</html>", "A", as_of, **kw)
        assert r2["deduped"] is False
        assert len(sent) == 2

    def test_different_content_is_not_deduped(self, mail_db, monkeypatch):
        """★ 内容变了就应真发 —— 否则内容更新会被误吞。"""
        m, sent = _sender(monkeypatch, mail_db)
        as_of = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        kw = dict(kind="midday", retry=1)
        m.send("Hawk 午间策略", "<html>A</html>", "A", as_of, **kw)
        r2 = m.send("Hawk 午间策略", "<html>B</html>", "B", as_of, **kw)
        assert r2["deduped"] is False
        assert len(sent) == 2

    def test_different_kind_not_deduped(self, mail_db, monkeypatch):
        """★ 同内容但不同 kind（盘前 vs 午间）应各发一次。"""
        m, sent = _sender(monkeypatch, mail_db)
        as_of = datetime(2026, 10, 9, 8, 30, tzinfo=CST)
        m.send("Hawk 盘前策略", "<html>A</html>", "A", as_of,
               kind="premarket", retry=1)
        r = m.send("Hawk 午间策略", "<html>A</html>", "A", as_of,
                   kind="midday", retry=1)
        assert r["deduped"] is False
        assert len(sent) == 2

    def test_different_day_is_not_deduped(self, mail_db, monkeypatch):
        """★ 次日同一内容应重新发送（判重按交易日，不是永久抑制）。"""
        m, sent = _sender(monkeypatch, mail_db)
        kw = dict(kind="midday", retry=1)
        m.send("Hawk 午间策略", "<html>A</html>", "A",
               datetime(2026, 10, 9, 11, 35, tzinfo=CST), **kw)
        r = m.send("Hawk 午间策略", "<html>A</html>", "A",
                   datetime(2026, 10, 10, 11, 35, tzinfo=CST), **kw)
        assert r["deduped"] is False
        assert len(sent) == 2

    def test_catch_up_later_timestamp_still_deduped(self, mail_db, monkeypatch):
        """★★ 补跑场景（本bug 的真实触发路径）。

        `scheduler.catch_up()` 会在重启后重跑同一批任务，钟点与首次不同
        （例如首次 11:35、补跑 16:20）。若用**完整时间戳**做等值比较
        就会漏判 ⇒ 仍会重复发送。判据必须归一到**交易日**。
        """
        m, sent = _sender(monkeypatch, mail_db)
        kw = dict(kind="midday", retry=1)
        m.send("Hawk 午间策略", "<html>A</html>", "A",
               datetime(2026, 10, 9, 11, 35, tzinfo=CST), **kw)
        r = m.send("Hawk 午间策略", "<html>A</html>", "A",
                   datetime(2026, 10, 9, 16, 20, tzinfo=CST), **kw)
        assert r["deduped"] is True, "同日不同时钟的补跑必须被拦住"
        assert len(sent) == 1

    def test_failed_send_is_not_recorded_as_sent(self, mail_db, monkeypatch):
        """★ 判重只看 status='sent' —— 失败记录不能挡住重试。

        ⚠ 这条测试第一版是**假阳性**（本轮实测）：
          它只断言「失败后再次发送能发出去」，而 `retry=1` 时
          判重查询根本没命中（因为**从没有 failed 行被写进库**——
          写的是 `_log_fail`，与 sent 不同的行）。
          把判重 SQL 里的 `status='sent'` 删掉，测试**仍全绿** ⇒ 判据不可区分。
        ⇒ 改为直接断言库内状态分布 + 直接调用 `_already_sent` 验证它不看 failed。
        """
        from backend.common import mailer
        m, _ = _sender(monkeypatch, mail_db)

        def boom(self_inner):
            raise mailer.smtplib.SMTPException("临时故障")
        monkeypatch.setattr(mailer.MailSender, "_connect", boom)
        monkeypatch.setattr(mailer.time, "sleep", lambda *_: None)

        as_of = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        r = m.send("Hawk 午间策略", "<html>A</html>", "A", as_of,
                   kind="midday", retry=1)
        assert r["ok"] is False

        # ★ 确认失败确实落了库（否则下面那条断言就是自欺）
        n_failed = mail_db.conn.execute(
            "select count(*) from mail_log where status='failed'").fetchone()[0]
        assert n_failed == 1, "失败必须留痕，否则无法区分「没发」与「发失败」"

        # ★ 直接问判重函数：只有 failed 行时**必须**返回 False
        h = mailer.content_hash("<html>A</html>")
        assert mailer.MailSender._already_sent(
            mail_db.conn, "midday", h, as_of) is False, (
            "failed 记录绝不能被当成'已发送'——否则一次SMTP 故障会让这封邮件当天永久丢失")

        # 修好连接后应能正常发出
        m2, sent = _sender(monkeypatch, mail_db)
        r2 = m2.send("Hawk 午间策略", "<html>A</html>", "A", as_of,
                     kind="midday", retry=1)
        assert r2["deduped"] is False
        assert len(sent) == 1

    def test_skip_does_not_create_sent_row(self, mail_db, monkeypatch):
        """★ 判重跳过**不写** mail_log（否则日志会被跳过记录污染）。"""
        m, _ = _sender(monkeypatch, mail_db)
        as_of = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        kw = dict(kind="midday", retry=1)
        m.send("Hawk 午间策略", "<html>A</html>", "A", as_of, **kw)
        m.send("Hawk 午间策略", "<html>A</html>", "A", as_of, **kw)
        n = mail_db.conn.execute(
            "select count(*) from mail_log where status='sent'").fetchone()[0]
        assert n == 1


class TestDedupContract:
    def test_send_signature_has_force(self):
        """★ 契约：send 必须支持 force，否则用户无法主动重发。"""
        import inspect
        from backend.common.mailer import MailSender
        assert "force" in inspect.signature(MailSender.send).parameters

    def test_return_dict_has_deduped_key(self):
        """★ 返回结构含 deduped，调用方才能区分「跳过」的原因。"""
        import inspect
        from backend.common.mailer import MailSender
        doc = inspect.getdoc(MailSender.send) or ""
        assert "deduped" in doc

    def test_docstring_records_the_real_incident(self):
        """★ 真实事故要写进 docstring（下一个改这里的人看不到本次会话）。"""
        import inspect
        from backend.common.mailer import MailSender
        doc = inspect.getdoc(MailSender.send) or ""
        assert "35" in doc, "必须在 docstring 记录「发了 35 次」这个真实事故"

    def test_templates_send_forwards_force(self):
        """★ mail_templates.send 必须透传 force，否则封装层把能力截断了。"""
        import inspect
        from backend.domain.mail_templates import send
        assert "force" in inspect.signature(send).parameters

    def test_scheduler_catchup_does_not_pass_force(self, mail_db, monkeypatch):
        """★★ 守卫不能被自动补跑穿透。

        若 catch_up 路径传了 force=True，去重就形同虚设 ——
        那正是「每次重启都重发一遍」的根源。
        """
        import inspect
        from backend.scheduler import run_job
        src = inspect.getsource(run_job)
        assert "force=True" not in src, "自动补跑路径不得传 force=True"