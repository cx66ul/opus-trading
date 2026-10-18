"""桌面邮件通知：持久队列、原子认领、按事件去重及有界重试。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, format_datetime
from pathlib import Path

from backend.common.mailer import MailConfig, MailSender, render_html_mail, render_text_mail
from backend.data.sqlite import DB_PATH
from backend.domain.news.base import CST
from backend.services.news_collection import wallclock

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'config' / 'mail_notifications.json'
SCHEDULE = {'premarket': (9, 0), 'midday': (12, 0), 'close': (15, 5), 'weekly': (10, 0)}
TITLES = {'premarket': '盘前报告', 'midday': '午间报告', 'close': '收盘报告', 'weekly': '每周汇总'}
LOG = logging.getLogger(__name__)


def connect(path):
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript('''CREATE TABLE IF NOT EXISTS notification_outbox (
        event_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
        attempted_at TEXT, next_attempt TEXT, sent_at TEXT, attempts INTEGER DEFAULT 0,
        error TEXT, message_id TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notification_state (key TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS mail_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,kind TEXT,subject TEXT,body_hash TEXT,status TEXT,err TEXT,retry INTEGER DEFAULT 0);''')
    return con


def load_config(path=CONFIG):
    if os.environ.get('OPUS_MAIL_DISABLE') == '1' or not Path(path).exists():
        return {'enabled': False}
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('enabled'):
        activation = datetime.fromisoformat(config['activated_at'])
        if activation.tzinfo is None:
            raise ValueError('邮件启用时间必须带时区')
    return config


def notification_payload(title, sections, now, **extra):
    html = render_html_mail(title, sections, now).replace('max-width:720px', 'max-width:600px')
    text = render_text_mail(title, sections, now)
    # 原始通用模板的固定来源列表不适合金十通知。
    old = '数据来源：腾讯行情 / 新浪财经 / 东方财富（免费接口，可能存在延迟）。'
    return dict(subject='Opus · ' + title, html=html.replace(old, '数据来源以正文逐条标注为准。'),
                text=text.replace(old, '数据来源以正文逐条标注为准。'), **extra)


def enqueue(con, key, kind, payload, now, expiry):
    mid = '<opus.' + hashlib.sha256(key.encode()).hexdigest()[:32] + '@notifications.local>'
    with con:
        return con.execute('''INSERT OR IGNORE INTO notification_outbox
            (event_key,kind,payload,created_at,message_id,expires_at) VALUES (?,?,?,?,?,?)''',
            (key,kind,json.dumps(payload,ensure_ascii=False),now.isoformat(),mid,expiry.isoformat())).rowcount


def report_slots(now, activated_at, trading):
    """只接纳当天十分钟窗口，不补发启用前或上次关机期间的旧报告。"""
    now = now.astimezone(CST)
    slots = []
    for kind, (hour, minute) in SCHEDULE.items():
        if (kind == 'weekly' and now.weekday() != 5) or (kind != 'weekly' and not trading):
            continue
        slot = now.replace(hour=hour,minute=minute,second=0,microsecond=0)
        if slot >= activated_at and timedelta(0) <= now-slot < timedelta(minutes=10):
            slots.append((kind,slot))
    return slots


def alert_window(now, trading, urgent=False):
    if urgent:
        return True
    now = now.astimezone(CST)
    minutes = now.hour*60 + now.minute
    return trading and (540 <= minutes <= 690 or 780 <= minutes <= 900)


def build_report(kind, now, db_path=DB_PATH):
    """只报告已入库数据，逐处注明数据时点；未采集不能伪装实时行情。"""
    from backend.services.tracked_news import read_tracked
    with closing(sqlite3.connect(str(db_path))) as con:
        con.row_factory = sqlite3.Row
        positions = con.execute('''SELECT p.code,coalesce(p.name,s.name,p.code) name,p.shares,p.cost
            FROM position p LEFT JOIN stock s ON s.id=p.code
            WHERE p.status IN ('holding','open') AND (p.shares IS NULL OR p.shares>0)''').fetchall()
        watches = con.execute('''SELECT w.code,coalesce(s.name,w.code) name FROM watchlist w
            LEFT JOIN stock s ON s.id=w.code ORDER BY w.priority DESC,w.id LIMIT 20''').fetchall()
        sections = [('报告范围', [f'生成于 {now:%Y-%m-%d %H:%M}（北京时间）。使用本机已保存数据；无实时行情时不推断当日涨跌。'])]
        sections.append(('当前持仓', [[r['code'],r['name'],f"数量 {r['shares'] if r['shares'] is not None else '未录入'}",
                                       f"成本 {r['cost'] if r['cost'] is not None else '未录入'}"] for r in positions] or ['未录入持仓']))
        sections.append(('自选观察', [[r['code'],r['name']] for r in watches] or ['暂无自选']))
    tracked = read_tracked(now,db_path,limit=50)
    rows = []
    for item in tracked['rows']:
        if item['state'] != 'done':
            continue
        label = {'bull':'利好','bear':'利空','neutral':'中性','unknown':'待核实'}[item['direction']]
        rows.append([f"{item['code']} {item['name']}",label,f"{item['source']} {item['ts']}",item['reason'] or ''])
    sections.append(('近七日关联新闻（模型判断）',rows[:20] or ['当前已采集内容中暂无完成核实的关联新闻；不代表市场没有相关新闻。']))
    # 同一查询按发送时点截止，不把历史日线伪装为盘中报价。
    try:
        from backend.data import duck
        cutoff = now.date() - timedelta(days=1) if now.hour < 15 else now.date()
        codes = list(dict.fromkeys([r['code'] for r in positions] + [r['code'] for r in watches]))
        if codes:
            values = duck.get_ro_conn().execute('''SELECT code,trade_date,close FROM kline_daily
                WHERE trade_date<=? AND code IN (''' + ','.join('?' for _ in codes) + ''')
                QUALIFY row_number() OVER(PARTITION BY code ORDER BY trade_date DESC)=1''', [cutoff,*codes]).fetchall()
            sections.append(('最近已入库日线收盘价（非实时报价）',[[c,str(d),str(v)] for c,d,v in values] or ['暂无行情']))
    except Exception:
        sections.append(('行情状态',['行情库暂不可用，本报告未包含价格，不以零代替缺失。']))
    sections.append(('说明',['利好利空为本地模型的逐股判断，原文与理由可在软件自选页核对。',
                             '本报告为持仓、自选和关联资讯汇总；不代表实时完整行情或策略回测结果。']))
    return notification_payload(TITLES[kind],sections,now,urgent=False)


def queue_alerts(con, now, activated_at, db_path):
    from backend.services.tracked_news import read_tracked
    data = read_tracked(now,db_path,limit=10000)
    for row in data['rows']:
        ts = datetime.fromisoformat(row['ts'])
        ts = ts.replace(tzinfo=CST) if ts.tzinfo is None else ts
        if not row['held'] or row['state'] != 'done' or ts < activated_at:
            continue
        if row['direction'] not in ('bull','bear') or (row['strength'] or 0)<3 or (row['confidence'] or 0)<.7:
            continue
        urgent = row['direction']=='bear' and row['strength']==5 and row['confidence']>=.8
        content = row.get('content') or row.get('title') or ''
        fingerprint = hashlib.sha256(''.join(content.split()).encode()).hexdigest()
        key = f"news:{row['code']}:{row['direction']}:{fingerprint}"
        label = '利好' if row['direction']=='bull' else '利空'
        title = ('【紧急】' if urgent else '') + f"持仓新闻 · {row['name']} {label}"
        sections = [('关联标的',[[row['code'],row['name'],label,f"强度{row['strength']}/5，置信度{row['confidence']:.0%}"]]),
                    ('原文',[f"{row['source']} · {row['ts']}",content]),
                    ('判断依据',[row['reason'] or '',row['evidence'] or '', '模型判断，未经人工验证。'])]
        enqueue(con,key,'holding_news',notification_payload(title,sections,now,urgent=urgent,code=row['code']),
                now,ts+timedelta(days=3))


def deliver_one(con, now, trading, cfg=None, transport=None):
    """DATA 后异常标记 uncertain；只对明确未接受的失败重试，避免重复投递。"""
    with con:
        con.execute("UPDATE notification_outbox SET state='uncertain',error='上次发送中断，需核对收件箱' "
                    "WHERE state='sending' AND julianday(attempted_at)<julianday(?)",
                    ((now-timedelta(minutes=5)).isoformat(),))
        con.execute("UPDATE notification_outbox SET state='expired',error='超过通知有效期' "
                    "WHERE state IN ('pending','retry') AND julianday(expires_at)<julianday(?)",(now.isoformat(),))
    rows = con.execute("SELECT * FROM notification_outbox WHERE state IN ('pending','retry') "
                       "AND (next_attempt IS NULL OR julianday(next_attempt)<=julianday(?)) ORDER BY created_at",(now.isoformat(),)).fetchall()
    for row in rows:
        payload = json.loads(row['payload'])
        if row['kind']=='holding_news':
            holding = con.execute("SELECT 1 FROM position WHERE code=? AND status IN ('holding','open') "
                                  "AND (shares IS NULL OR shares>0)",(payload['code'],)).fetchone()
            if not holding:
                with con:
                    con.execute("UPDATE notification_outbox SET state='cancelled',error='已不在持仓中' WHERE event_key=?",(row['event_key'],))
                continue
            if not alert_window(now,trading,payload.get('urgent',False)):
                continue
        elif not payload.get('urgent') and (now.astimezone(CST).hour>=23 or now.astimezone(CST).hour*60+now.astimezone(CST).minute<450):
            continue
        with con:
            if not con.execute("UPDATE notification_outbox SET state='sending',attempted_at=?,attempts=attempts+1 "
                               "WHERE event_key=? AND state IN ('pending','retry')",(now.isoformat(),row['event_key'])).rowcount:
                continue
        phase = 'connect'
        state,error = 'failed',''
        try:
            config = cfg or MailConfig.from_secrets()
            if not config.to:
                raise ValueError('未配置有效收件人')
            msg=MIMEMultipart('alternative')
            msg['Subject']=payload['subject']; msg['From']=formataddr(('Opus 辅助交易',config.sender))
            msg['To']=', '.join(config.to); msg['Date']=format_datetime(now)
            msg['Message-ID']=row['message_id']
            msg.attach(MIMEText(payload['text'],'plain','utf-8'));msg.attach(MIMEText(payload['html'],'html','utf-8'))
            smtp=(transport or MailSender(config)._connect)()
            try:
                smtp.login(config.user,config.auth_code)
                phase='data'
                rejected=smtp.sendmail(config.sender,config.to,msg.as_string())
                phase='accepted'
                state='partial' if rejected else 'sent'
                error=f'部分收件人拒收：{len(rejected)} 个；未自动重发' if rejected else ''
            finally:
                # QUIT 失败不能让已接受的邮件重新入队。
                try:
                    smtp.quit()
                except Exception:
                    pass
        except Exception as exc:
            safe_failure = phase=='connect' or isinstance(exc,(smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,smtplib.SMTPDataError))
            state = ('retry' if row['attempts']<2 else 'failed') if safe_failure else 'uncertain'
            error=f"{type(exc).__name__}（SMTP {getattr(exc,'smtp_code','未知')}）"
        delay = 60 if row['attempts']==0 else 300
        with con:
            con.execute('''UPDATE notification_outbox SET state=?,error=?,sent_at=?,next_attempt=? WHERE event_key=?''',
                        (state,error,now.isoformat() if state in ('sent','partial') else None,
                         (now+timedelta(seconds=delay)).isoformat() if state=='retry' else None,row['event_key']))
            con.execute('''INSERT INTO mail_log(ts,kind,subject,body_hash,status,err,retry) VALUES (?,?,?,?,?,?,?)''',
                        (now.isoformat(),row['kind'],payload['subject'],hashlib.sha256(payload['html'].encode()).hexdigest()[:16],
                         state,error,row['attempts']+1))
        return state
    return 'idle'


class MailNotifications:
    def __init__(self,db_path=DB_PATH,config_path=CONFIG):
        self.db_path=db_path;self.config_path=config_path
        self.stop_event=threading.Event();self.thread=None
        self.last_error='';self.last_tick=None
        self.calendar_day=None;self.trading=False

    def start(self):
        self.thread=threading.Thread(target=self._run,name='mail-notifications',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=1)

    def tick(self,now,config,trading):
        activation=datetime.fromisoformat(config['activated_at'])
        with closing(connect(self.db_path)) as con:
            for kind,slot in report_slots(now,activation,trading):
                key=f'report:{kind}:{slot.date()}'
                if con.execute('SELECT 1 FROM notification_outbox WHERE event_key=?',(key,)).fetchone():continue
                enqueue(con,key,kind,build_report(kind,now,self.db_path),now,slot+timedelta(minutes=30))
            queue_alerts(con,now,activation,self.db_path)
            result=deliver_one(con,now,trading)
            with con:
                con.execute('INSERT OR REPLACE INTO notification_state VALUES (?,?)',
                            ('heartbeat',json.dumps(dict(at=now.isoformat(),result=result,trading=trading))))
        self.last_tick=now.isoformat();self.last_error=''

    def _run(self):
        if self.stop_event.wait(3):return
        while not self.stop_event.is_set():
            try:
                config=load_config(self.config_path)
                if config.get('enabled'):
                    now=wallclock()
                    if self.calendar_day!=now.date():
                        from backend.domain.trade_cal import is_trade_day
                        self.trading=is_trade_day(now.date())
                        self.calendar_day=now.date()
                    self.tick(now,config,self.trading)
            except Exception as exc:
                self.last_error=f'{type(exc).__name__}: {exc}'[:300]
                LOG.exception('邮件通知轮询失败')
            if self.stop_event.wait(20):break

    def status(self):
        return notification_status(self.db_path,self.config_path, bool(self.thread and self.thread.is_alive()
                                  and not self.stop_event.is_set()),self.last_error)


def notification_status(db_path=DB_PATH,config_path=CONFIG,running=False,error=''):
    config=load_config(config_path)
    result=dict(enabled=bool(config.get('enabled')),running=running,activated_at=config.get('activated_at'),
                schedule=SCHEDULE,heartbeat=None,counts={},recent=[],error=error)
    with closing(sqlite3.connect(str(db_path))) as con:
        con.row_factory=sqlite3.Row
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='notification_outbox'").fetchone():return result
        beat=con.execute("SELECT payload FROM notification_state WHERE key='heartbeat'").fetchone()
        result['heartbeat']=json.loads(beat[0]) if beat else None
        result['counts']={r[0]:r[1] for r in con.execute('SELECT state,count(*) FROM notification_outbox GROUP BY state')}
        result['recent']=[dict(r) for r in con.execute('''SELECT kind,state,created_at,attempted_at,sent_at,attempts,error
            FROM notification_outbox ORDER BY created_at DESC LIMIT 10''')]
    return result
