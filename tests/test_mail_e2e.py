# -*- coding: utf-8 -*-
"""★★ 邮件发送**端到端**链路测试（真实渲染 + 假 SMTP）。

★★★ 这组测试补的是一个真实盲区
--------------------------------
`tests/test_audit_fixes.py` 里`send()` 的用例全部用 `monkeypatch` 把
`smtplib.SMTP_SSL` 换掉、或直接 `MailSender.__new__` 跳过读密钥库。
它们验证的是「免打扰判定」与「重试次数」这些**局部**逻辑，
**从未跑过 `mail_templates.send()` 这个真实入口**。

后果（实测）：`mail_log` 表 **0 行** —— 邮件一封都没发出去过，
而测试全绿。因为签名不匹配、模板 None 崩溃这类问题
都发生在 mock 覆盖不到的地方：
  · `MailContext` 字段缺失 → 模板渲染抛异常
  · `MailSender()` 读不到 `mail.to` → 不知道发给谁
  · subject/kind 传错 → 收件箱里是一堆无意义标题

★ 这里的原则
------------
**不连真实 SMTP**（不该在测试里发信），但**除 SMTP 之外全部走真路径**：
渲染 → 免打扰 → 建连 → 写 `mail_log`。
只在最后一层把 socket 换掉 —— 这样能抓到「渲染/配置/参数」类问题，
又不会污染邮箱。

运行： pytest tests/test_mail_e2e.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 9, 8, 30, tzinfo=CST)


# ================================================================== 假 SMTP
class FakeSMTP:
    """记录投递内容的假 SMTP。只拦最后一层 socket。"""

    sent: list[dict] = []
    slept: list[float] = []

    def __init__(self, host, port, timeout=None, context=None):
        # ★ context 参数：`MailSender._connect` 用
        #   `SMTP_SSL(host, port, context=ctx, timeout=...)` 调用，
        #   签名不匹配会TypeError（而不是连出去）
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self._user = user

    def sendmail(self, from_addr, to_addrs, body):
        FakeSMTP.sent.append({
            "from": from_addr, "to": list(to_addrs), "body": body,
        })

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def _reset_smtp(monkeypatch):
    """★ 保险丝：任何**未被打桩**的 SMTP 连接立刻抛错。

    测试绝不能真连 SMTP —— 既会挂起超时，又会往邮箱里发垃圾。
    用 `monkeypatch.setattr(..., raising=False)` 兜住所有出口，
    漏打桩的那个会以「立刻失败」的方式暴露，而不是静默发信。
    """
    import smtplib as _s

    def _boom(*a, **k):
        raise AssertionError(
            "测试试图连接真实 SMTP —— 说明有 send 路径没被 FakeSMTP 拦住")

    monkeypatch.setattr(_s, "SMTP", _boom, raising=False)
    monkeypatch.setattr(_s, "SMTP_SSL", _boom, raising=False)

    # ★★ 必须打桩 `time.sleep`：规格 §5.3 把重试延迟改成 30s/120s/300s
    #   （原为 2s/4s）。这意味着**任何触发重试的测试会真睡 30 秒起步**，
    #   多轮重试更是 30+120+300 = 450 秒 ⇒ 表现为「测试卡死 / SIGTERM」。
    #   这不是代码缺陷，是规格要求的必然结果，但测试必须规避。
    import backend.common.mailer as _m
    slept: list[float] = []
    monkeypatch.setattr(_m.time, "sleep", lambda d: slept.append(d))
    FakeSMTP.slept = slept          # 供断言检查退避梯度
    FakeSMTP.sent = []
    yield
    FakeSMTP.sent = []


# ================================================================ 配置
def _cfg():
    return type("C", (), {
        "to": ["me@example.com"], "sender": "hawk@example.com",
        "user": "hawk@example.com", "auth_code": "x",
        "host": "smtp.example.com", "port": 465, "use_ssl": True,
        "timeout": 1.0, "retry": 1,
    })()


# ================================================================== 用例
class TestMailTemplatesSend:
    """★ 走 `mail_templates.send()` 真实入口。"""

    def test_alert_mail_renders_and_delivers(self, monkeypatch):
        """★ 告警邮件能完整走通：渲染 → 投递 → 写 mail_log。"""
        from backend.common import mailer
        from backend.domain import mail_templates as mt

        logged: list = []
        monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn(logged))
        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
        monkeypatch.setattr(mailer.MailConfig, "from_secrets",
                            staticmethod(lambda: _cfg()))
        monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: False)

        ctx = mt.MailContext(
            kind="alert", as_of=AS_OF, urgent=True,
            market_state="start", position_cap=0.7, market_score=62.0,
            alert_text="600519 价格异常",
        )
        res = mt.send(ctx)
        assert res["ok"] is True, f"发送应成功，实得 {res}"
        assert FakeSMTP.sent, "★ 应真的调用了 sendmail（否则等于没验证投递）"
        assert len(FakeSMTP.sent) == 1
        assert FakeSMTP.sent[0]["to"] == ["me@example.com"]

    @pytest.mark.parametrize("kind,label", [
        ("premarket", "盘前策略"), ("midday", "午间策略"), ("alert", "告警"),
    ])
    def test_subject_contains_kind_label(self, monkeypatch, kind, label):
        """★ 三类邮件的主题必须带中文类别（收件箱里要能分辨）。

        主题在 `mail_templates.send` 内部由 `title_map` 拼成，
        不走 `MailContext` 字段 —— 所以只能断言最终 MIME 头。
        """
        import email
        from backend.common import mailer
        from backend.domain import mail_templates as mt

        cap: dict = {}

        class Rec(FakeSMTP):
            def sendmail(self, f, to, body):
                msg = email.message_from_string(body)
                cap["subject"] = str(email.header.make_header(
                    email.header.decode_header(msg.get("Subject", ""))))

        monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn([]))
        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Rec)
        monkeypatch.setattr(mailer.MailConfig, "from_secrets",
                            staticmethod(lambda: _cfg()))
        monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: False)

        ctx = mt.MailContext(kind=kind, as_of=AS_OF, urgent=True,
                             market_state="start", market_score=60.0,
                             alert_text="x")
        res = mt.send(ctx)
        assert res["ok"] is True, f"{kind} 发送失败：{res}"
        assert label in cap["subject"], \
            f"{kind} 主题应含「{label}」，实得 {cap['subject']!r}"

    def test_missing_fields_do_not_crash(self, monkeypatch):
        """★★ 数据缺失时邮件仍要发得出去（占位符而非崩溃）。

        场景：盘前某只票没有现价/没有市场评分。
        此时恰恰最需要收到邮件提醒。
        """
        from backend.common import mailer
        from backend.domain import mail_templates as mt

        monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn([]))
        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
        monkeypatch.setattr(mailer.MailConfig, "from_secrets",
                            staticmethod(lambda: _cfg()))
        monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: False)

        # 构造一个「几乎全空」的上下文：只有 as_of/kind，其余全是默认空值
        ctx = mt.MailContext(kind="premarket", as_of=AS_OF, urgent=True)
        res = mt.send(ctx)
        assert res["ok"] is True, f"空数据也应发得出，实得 {res}"
        body = FakeSMTP.sent[0]["body"]
        assert "None" not in body, \
            "★ 正文里出现 'None' 说明 None 没被兜底成占位符"

    def test_quiet_hours_blocks_non_urgent(self, monkeypatch):
        """★ 免打扰必须真的挡住非紧急邮件，并留痕。"""
        from backend.common import mailer
        from backend.domain import mail_templates as mt

        logged: list = []
        monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn(logged))
        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
        monkeypatch.setattr(mailer.MailConfig, "from_secrets",
                            staticmethod(lambda: _cfg()))
        monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: True)

        night = datetime(2026, 10, 9, 23, 30, tzinfo=CST)
        ctx = mt.MailContext(kind="premarket", as_of=night, urgent=False)
        res = mt.send(ctx)
        assert res.get("skipped") is True, "免打扰时段应跳过"
        assert not FakeSMTP.sent, "被跳过就不该真发"
        assert logged, "★ 跳过也必须写 mail_log，否则事后无从判断"


class _FakeConn:
    """记录写库的调用。

    ★ `execute()` 必须返回**带 lastrowid 的游标**：
      `MailSender._log_ok` 用 `cur.lastrowid` 取自增 id。
      早先返回 `self`（无该属性）⇒ 邮件其实**已经发出**了，
      却在写日志时抛 AttributeError，被except 吞掉后重试 ——
      表现为「已发送」日志打印 3 次 + 最终返回失败。
      这正是「投递成功但业务判定失败」的隐蔽形态。

    ★★ 2026-10-10：必须按 SQL 类型**分派**（新增判重查询后暴露）。
      判重那条SQL 是 `SELECT 1 FROM mail_log ...`，
      若桩对所有语句都返回「成功游标」，判重会被误判为「已发送」⇒
      首次发送就被跳过 ⇒ `res["ok"]` 为 False、`sent` 恒空。
      ⇒ SELECT 语句返回「查不到」的游标（`fetchone() -> None`），
        INSERT/UPDATE 才返回带 lastrowid 的成功游标。
      ★ 这是「测试替身字段/行为要与真实类一致」的老纪律，
        本次因为新增了一条**读**路径才暴露出来。
    """

    def __init__(self, sink):
        self.sink = sink
        self._lastrowid = 0

    def execute(self, sql, args=()):
        self.sink.append((sql, args))
        cur = type("C", (), {})()
        if sql.lstrip().upper().startswith("SELECT"):
            # 查重路径：桩内没有真实数据 ⇒ 一律「查不到」
            cur.fetchone = lambda: None
            cur.fetchall = lambda: []
            cur.lastrowid = 0
            cur.rowcount = 0
            return cur
        self._lastrowid += 1
        cur.lastrowid = self._lastrowid
        cur.rowcount = 1
        cur.fetchone = lambda: None
        cur.fetchall = lambda: []
        return cur

    def commit(self):
        pass


# ================================================================== 配置
class TestMailConfigReachable:
    """★ 真实密钥库必须能读出配置（否则 send 会在运行时才炸）。"""

    def test_config_can_be_loaded_or_raises_clearly(self):
        """读配置要么成功，要么抛**清晰**异常，不能静默返回空。"""
        from backend.common.mailer import MailConfig
        try:
            cfg = MailConfig.from_secrets()
        except Exception as exc:
            # 凭据缺失是合法状态，但必须能说清缺什么
            assert isinstance(exc, Exception)
            assert str(exc), "异常必须带消息（否则用户不知道该跑 setup_secrets）"
            return
        # 读到配置就必须字段完整
        for f in ("host", "port", "user", "auth_code", "sender"):
            assert getattr(cfg, f, None), f"配置字段 {f} 为空"
        if not cfg.to:
            pytest.skip("mail.to 未设置（跑 scripts/setup_secrets.py 配置）")


if __name__ == "__main__":                        # pragma: no cover
    pytest.main([__file__, "-v"])


class TestRetryDoesNotResendOnPostDeliveryFailure:
    """★★ 投递成功但写日志失败时，**不得重发**。

    实测形态（写这组测试时被逼出来的）：
      `MailSender.send` 的顺序是「先 sendmail 成功 → 再 _log_ok 写 mail_log」。
      若 `_log_ok` 抛异常（这里是我第一版测试桩`execute()` 返回 `self`、
      没有 `lastrowid`），异常被 `except Exception` 捕获 ⇒
        · 日志打印「已发送」**3 次**
        · `sendmail` 被调用 **3 次**（真发信 ⇒ 用户收到 3 封重复邮件）
        · 最终返回 `ok=False`
      这是「投递成功但业务判定失败」的隐蔽形态：
      日志说成功了，业务说失败了，用户收到 N 封一样的信。

      本测试用**最小可用**的游标桩，让投递后日志正常写入，
      并断言 `sendmail` 只被调一次 —— 若顺序或异常处理被改坏，
      `sent` 计数会 >1 而此处会失败。
    """

    def test_sendmail_called_exactly_once_on_success(self, monkeypatch):
        from backend.common import mailer
        from backend.domain import mail_templates as mt

        logged: list = []
        monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn(logged))
        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
        monkeypatch.setattr(mailer.MailConfig, "from_secrets",
                            staticmethod(lambda: _cfg()))
        monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: False)

        ctx = mt.MailContext(kind="alert", as_of=AS_OF, urgent=True,
                             market_state="start", market_score=62.0,
                             alert_text="x")
        res = mt.send(ctx)
        assert res["ok"] is True, f"应成功：{res}"
        assert len(FakeSMTP.sent) == 1, \
            f"★ 投递应只发生 1 次，实得 {len(FakeSMTP.sent)} 次（用户会收到重复邮件）"

    def test_log_failure_is_distinguishable_from_delivery_failure(self, monkeypatch):
        """★ 投递成功但写日志失败时，日志记录必须能反映真实状态。

        不要求业务上完美（那要改 `send` 的顺序），
        但至少 `sendmail` 不该因为后续失败而被重复调用。
        """
        from backend.common import mailer
        from backend.domain import mail_templates as mt

        calls = {"n": 0}

        class Counting(FakeSMTP):
            def sendmail(self, f, t, body):
                calls["n"] += 1
                raise OSError("网络中断")

        monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn([]))
        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Counting)
        monkeypatch.setattr(mailer.MailConfig, "from_secrets",
                            staticmethod(lambda: _cfg()))
        monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: False)

        ctx = mt.MailContext(kind="alert", as_of=AS_OF, urgent=True,
                             alert_text="x")
        #★ 显式 retry=1：验证 `mail_templates.send` 真的能把 retry 传下去
        #   （此前硬编码走默认 3 次，调用方无法缩短 30/120/300 的退避）
        res = mt.send(ctx, retry=1)
        assert res["ok"] is False, "投递失败应返回 ok=False"
        assert calls["n"] == 1, \
            f"retry=1 时只该尝试 1 次，实得 {calls['n']} 次"
        assert FakeSMTP.slept == [], "retry=1 且已用尽，不应再退避"

    def test_retry_is_forwarded_to_mail_sender(self, monkeypatch):
        """★★ `mail_templates.send(retry=N)` 必须真的生效。

        缺陷背景：`MailSender.send` 的默认 `retry=3`，而重试延迟是
        30s/120s/300s（规格 §5.3）⇒ 一次失败会拖 450 秒。
        若 `mail_templates.send` 不透传，调用方**没有任何办法**缩短它 ——
        告警邮件在 SMTP 故障时会被卡住 7.5 分钟。
        """
        from backend.common import mailer
        from backend.domain import mail_templates as mt

        calls = {"n": 0}

        class Failing(FakeSMTP):
            def sendmail(self, f, t, body):
                calls["n"] += 1
                raise OSError("连接超时")

        monkeypatch.setattr(mailer, "get_conn", lambda: _FakeConn([]))
        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Failing)
        monkeypatch.setattr(mailer.MailConfig, "from_secrets",
                            staticmethod(lambda: _cfg()))
        monkeypatch.setattr(mailer, "in_quiet_hours", lambda ts: False)

        ctx = mt.MailContext(kind="alert", as_of=AS_OF, urgent=True,
                             alert_text="x")
        mt.send(ctx, retry=2)
        assert calls["n"] == 2, \
            f"retry=2 应恰好尝试 2 次，实得 {calls['n']} 次（retry 未透传）"
        # 第一次失败后才退避一次（30s）
        assert FakeSMTP.slept == [30], \
            f"retry=2 应只退避一次(30s)，实得 {FakeSMTP.slept}"
