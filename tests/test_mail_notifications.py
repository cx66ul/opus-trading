import json
import smtplib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from email import message_from_string

import pytest

from backend.services import mail_notifications as M
from backend.common.mailer import MailConfig

NOW=datetime.fromisoformat('2026-10-12T10:00:00+08:00')
CFG=MailConfig('example.com',465,'from@example.com','stub','from@example.com',['to@example.com'])


class SMTP:
    def __init__(self,error=None,partial=False,quit_error=False):
        self.error=error;self.partial=partial;self.quit_error=quit_error;self.messages=[]
    def login(self,*args):
        pass
    def sendmail(self,sender,targets,message):
        if self.error:raise self.error
        self.messages.append(message)
        return {'other@example.com':(550,b'rejected')} if self.partial else {}
    def quit(self):
        if self.quit_error:raise OSError('QUIT disconnected')


@pytest.fixture
def db(tmp_path):
    path=tmp_path/'mail.sqlite'
    con=M.connect(path)
    con.executescript('''CREATE TABLE position(code,status,shares);CREATE TABLE stock(id,name,industry,concept);
        CREATE TABLE watchlist(code,priority,id);
        INSERT INTO position VALUES('600001','holding',NULL);''')
    yield path,con
    con.close()


def queue(con,key='report:midday:2026-10-12',kind='midday',urgent=False,expiry=None):
    return M.enqueue(con,key,kind,M.notification_payload('测试',[('内容',['原文'])],NOW,urgent=urgent,code='600001'),
                     NOW,expiry or NOW+timedelta(days=3))


def test_schedule_no_backfill_and_weekend():
    start=NOW-timedelta(days=1)
    at=NOW.replace(hour=9)
    assert M.report_slots(at,start,True)==[('premarket',at)]
    assert M.report_slots(at,start,False)==[]
    assert M.report_slots(at,at+timedelta(seconds=1),True)==[]
    assert M.report_slots(at+timedelta(minutes=10),start,True)==[]
    sat=datetime.fromisoformat('2026-10-17T10:00:00+08:00')
    assert M.report_slots(sat,start,False)==[('weekly',sat)]
    assert M.report_slots(sat+timedelta(days=1),start,False)==[]


def test_one_report_per_slot_even_content_changes(db):
    _,con=db
    assert queue(con)==1
    assert queue(con)==0
    smtp=SMTP()
    assert M.deliver_one(con,NOW,True,CFG,lambda:smtp)=='sent'
    assert M.deliver_one(con,NOW,True,CFG,lambda:smtp)=='idle'
    assert len(smtp.messages)==1
    msg=message_from_string(smtp.messages[0])
    assert msg['Message-ID'].startswith('<opus.')
    assert msg['To']=='to@example.com'
    assert con.execute('SELECT count(*) FROM mail_log').fetchone()[0]==1


def test_cross_process_style_claim_only_one_sender(db):
    path,con=db;queue(con)
    smtp=SMTP()
    def worker(_):
        with closing(M.connect(path)) as connection:
            return M.deliver_one(connection,NOW,True,CFG,lambda:smtp)
    with ThreadPoolExecutor(max_workers=4) as pool:
        states=list(pool.map(worker,range(4)))
    assert states.count('sent')==1
    assert len(smtp.messages)==1


def test_expired_report_is_not_sent(db):
    _,con=db;queue(con,expiry=NOW-timedelta(seconds=1))
    smtp=SMTP()
    assert M.deliver_one(con,NOW,True,CFG,lambda:smtp)=='idle'
    assert con.execute('SELECT state FROM notification_outbox').fetchone()[0]=='expired'
    assert not smtp.messages


def test_connection_failure_retries_but_data_timeout_does_not(db):
    _,con=db;queue(con)
    def broken():raise OSError('offline')
    assert M.deliver_one(con,NOW,True,CFG,broken)=='retry'
    assert M.deliver_one(con,NOW,True,CFG,broken)=='idle'
    smtp=SMTP(error=TimeoutError('no final DATA response'))
    assert M.deliver_one(con,NOW+timedelta(minutes=1),True,CFG,lambda:smtp)=='uncertain'
    assert M.deliver_one(con,NOW+timedelta(minutes=10),True,CFG,lambda:SMTP())=='idle'


def test_explicit_smtp_rejection_has_bounded_retries(db):
    _,con=db;queue(con)
    smtp=SMTP(error=smtplib.SMTPDataError(451,b'try later'))
    for delta,expected in [(0,'retry'),(60,'retry'),(360,'failed')]:
        assert M.deliver_one(con,NOW+timedelta(seconds=delta),True,CFG,lambda:smtp)==expected
    assert M.deliver_one(con,NOW+timedelta(hours=1),True,CFG,lambda:smtp)=='idle'
    assert con.execute('SELECT attempts FROM notification_outbox').fetchone()[0]==3


@pytest.mark.parametrize('partial,quit_error,expected',[(True,False,'partial'),(False,True,'sent')])
def test_partial_acceptance_and_quit_failure_never_resend(db,partial,quit_error,expected):
    _,con=db;queue(con)
    smtp=SMTP(partial=partial,quit_error=quit_error)
    assert M.deliver_one(con,NOW,True,CFG,lambda:smtp)==expected
    assert M.deliver_one(con,NOW,True,CFG,lambda:smtp)=='idle'
    assert len(smtp.messages)==1


def test_interrupted_send_is_uncertain_not_retried(db):
    _,con=db;queue(con)
    with con:con.execute("UPDATE notification_outbox SET state='sending',attempted_at=?",((NOW-timedelta(minutes=6)).isoformat(),))
    assert M.deliver_one(con,NOW,True,CFG,lambda:SMTP())=='idle'
    assert con.execute('SELECT state FROM notification_outbox').fetchone()[0]=='uncertain'


def test_regular_alert_waits_and_removed_holding_cancelled(db):
    _,con=db;queue(con,'alert','holding_news')
    after=NOW.replace(hour=20)
    assert M.deliver_one(con,after,True,CFG,lambda:SMTP())=='idle'
    with con:con.execute('DELETE FROM position')
    assert M.deliver_one(con,NOW,True,CFG,lambda:SMTP())=='idle'
    assert con.execute('SELECT state FROM notification_outbox').fetchone()[0]=='cancelled'


def test_urgent_alert_outside_market_and_quiet_hours(db):
    _,con=db;queue(con,'urgent','holding_news',urgent=True)
    assert M.deliver_one(con,NOW.replace(hour=23),False,CFG,lambda:SMTP())=='sent'


def test_queue_filters_old_news_watchlist_and_uncertain_labels(db,monkeypatch):
    _,con=db
    base=dict(ts=NOW.isoformat(),held=1,state='done',strength=3,confidence=.8,direction='bull',
              code='600001',name='测试公司',content='公司获得新订单',source='jin10',reason='订单',evidence='新订单')
    rows=[base,{**base,'source':'sina_7x24'}, {**base,'held':0,'content':'仅自选'},
          {**base,'ts':(NOW-timedelta(days=1)).isoformat(),'content':'历史'},
          {**base,'confidence':.4,'content':'低置信'}, {**base,'direction':'unknown','content':'未知'}]
    monkeypatch.setattr('backend.services.tracked_news.read_tracked',lambda *a,**kw:dict(rows=rows))
    M.queue_alerts(con,NOW,NOW-timedelta(hours=1),None)
    assert con.execute('SELECT count(*) FROM notification_outbox').fetchone()[0]==1


def test_disabled_config_and_lifecycle(tmp_path,monkeypatch):
    config=tmp_path/'config.json'
    assert M.load_config(config)=={'enabled':False}
    config.write_text(json.dumps(dict(enabled=True,activated_at=NOW.isoformat())))
    assert M.load_config(config)=={'enabled':False} # pytest fixture forces off
    monkeypatch.delenv('OPUS_MAIL_DISABLE')
    assert M.load_config(config)['enabled'] is True
    service=M.MailNotifications(tmp_path/'test.sqlite',config)
    service.start();service.stop()
    assert not service.thread.is_alive()
