"""邮件模板→发送器契约，全部离线，不读取真实凭据。"""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone, timedelta
import smtplib

import pytest

from backend.common.mailer import MailConfig
from backend.domain import mail_templates as mt


@pytest.fixture
def sender(monkeypatch):
    cfg = MailConfig('smtp.example.com', 465, 'from@example.com', 'fake',
                     'from@example.com', ['original@example.com'])

    class FakeSender:
        def __init__(self):
            self.cfg = cfg
            self.calls = []

        def send(self, *args, **kwargs):
            self.calls.append((args, kwargs, self.cfg))
            return dict(ok=True)

    instance = FakeSender()
    monkeypatch.setattr(mt, 'MailSender', lambda: instance)
    return instance, cfg


def context(urgent=False):
    return mt.MailContext(as_of=datetime(2026,10,11,23,30,
        tzinfo=timezone(timedelta(hours=8))),kind='alert',urgent=urgent,alert_text='告警内容')


def test_urgent_flag_reaches_sender(sender):
    instance, _ = sender
    mt.send(context(True),retry=1)
    args, kwargs, _ = instance.calls[0]
    assert kwargs['urgent'] is True
    assert kwargs['retry'] == 1
    assert '【紧急】' in args[0]


def test_normal_alert_does_not_bypass_quiet_hours(sender):
    instance, _ = sender
    mt.send(context(False))
    assert instance.calls[0][1]['urgent'] is False
    assert 'force' not in instance.calls[0][1]


def test_recipient_override_preserves_immutable_original(sender):
    instance, original = sender
    assert mt.send(context(), to=['override@example.com'])['ok'] is True
    assert instance.calls[0][2].to == ['override@example.com']
    assert original.to == ['original@example.com']
    with pytest.raises(FrozenInstanceError):
        original.to = []


@pytest.mark.parametrize('transport', [smtplib.SMTP, smtplib.SMTP_SSL])
def test_suite_prevents_real_smtp_connections(transport):
    # autouse 防线必须在 DNS/TLS/认证之前失败。
    with pytest.raises(pytest.fail.Exception, match='真实 SMTP'):
        transport('smtp.example.com', timeout=.1)
