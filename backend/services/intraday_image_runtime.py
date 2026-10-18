"""Versioned screenshot observation journal, separate from original Chan observations."""
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from backend.domain import intraday_images as rules


def setup(con):
    con.executescript('''
      CREATE TABLE IF NOT EXISTS image_observations(code TEXT,period TEXT,policy TEXT,key TEXT,payload TEXT,
        PRIMARY KEY(code,period,policy,key));
      CREATE TABLE IF NOT EXISTS image_runs(code TEXT,period TEXT,policy TEXT,latest TEXT,
        PRIMARY KEY(code,period,policy));
      CREATE TABLE IF NOT EXISTS image_context(code TEXT,ts TEXT,observed_at TEXT,payload TEXT,
        PRIMARY KEY(code,ts,observed_at));
      CREATE TABLE IF NOT EXISTS image_revisions(id INTEGER PRIMARY KEY,code TEXT,period TEXT,
        policy TEXT,key TEXT,observed_at TEXT,reason TEXT);
    ''')
    if 'source_hash' not in {r[1] for r in con.execute('PRAGMA table_info(image_runs)')}:
        con.execute('ALTER TABLE image_runs ADD COLUMN source_hash TEXT')
        con.commit()


def capture_context(con, code, now):
    """Never backfill a prior bar's environment with a snapshot observed later."""
    from backend.data import duck
    try:
        market=duck.get_ro_conn().execute('''SELECT CAST(ts AS VARCHAR),score,limit_down_cnt
          FROM market_regime WHERE ts<=? ORDER BY ts DESC LIMIT 1''',[now]).fetchone()
        if not market or not 0 <= (datetime.fromisoformat(now)-datetime.fromisoformat(market[0])).total_seconds() <= 180:
            return
        ctx=dict(ts=market[0],observed_at=now,market_good=market[1] is not None and market[1]>=60,
                 limit_down_count=market[2],sector_strong=None)
        boards=duck.get_ro_conn().execute('''SELECT b.strength,b.factor_coverage,b.source_complete,
            CAST(b.ts AS VARCHAR),b.board_code FROM board_strength b
            WHERE b.ts<=? AND b.ts>=CAST(? AS TIMESTAMP)-INTERVAL '180 seconds'
              AND b.board_code IN (SELECT board_code FROM board_member_snapshot WHERE code=?
                AND snap_date=(SELECT max(snap_date) FROM board_member_snapshot WHERE code=? AND snap_date<=CAST(? AS DATE)))
            QUALIFY row_number() OVER(PARTITION BY b.board_code ORDER BY b.ts DESC)=1''',
            [now,now,code,code,now]).fetchall()
        complete=[b for b in boards if b[2] and b[1] is not None and b[1]>=.8 and b[0] is not None]
        if complete:
            ctx['sector_strong']=any(b[0]>=60 for b in complete)
            ctx['board_evidence']=[dict(code=b[4],strength=b[0],ts=b[3]) for b in complete]
        with con:
            con.execute('INSERT OR IGNORE INTO image_context VALUES (?,?,?,?)',
                        (code,market[0],now,json.dumps(ctx)))
    except Exception:
        return  # Missing context fails closed; never invent a passing score.


def update(con, code, period, now, fresh):
    if period not in ('m1','m5'):return
    setup(con)
    p=rules.load_policy()
    if not p['enabled']:return
    rows=[json.loads(r[0]) for r in con.execute('SELECT payload FROM bars WHERE code=? AND period=? ORDER BY ts',(code,period))]
    if not rows:return
    fingerprint=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
    run=con.execute('SELECT latest,source_hash FROM image_runs WHERE code=? AND period=? AND policy=?',(code,period,p['hash'])).fetchone()
    if run and run[0]==rows[-1]['ts'] and run[1]==fingerprint:return
    capture_context(con,code,now)
    contexts=[json.loads(r[0]) for r in con.execute('SELECT payload FROM image_context WHERE code=? ORDER BY observed_at',(code,))]
    def context_at(at):
        candidates=[c for c in contexts if c['ts']<=at and c['observed_at']<=at]
        return candidates[-1] if candidates else None
    events=rules.stream(code,rows,int(period[1:]),rows[-1]['ts'],p,context_at)
    old={r['key']:json.loads(r['payload']) for r in con.execute(
        'SELECT key,payload FROM image_observations WHERE code=? AND period=? AND policy=?',(code,period,p['hash']))}
    with con:
        keys={f"{e['kind']}|{e['structure_at']}|{e['available_at']}" for e in events}
        for key,old_event in old.items():
            if key not in keys and not old_event.get('source_revised'):
                old_event.update(source_revised=True,active=False)
                old_event['reason']+='；源历史数据修订后本触发不再成立，首次记录保留'
                con.execute('UPDATE image_observations SET payload=? WHERE code=? AND period=? AND policy=? AND key=?',
                    (json.dumps(old_event,ensure_ascii=False),code,period,p['hash'],key))
                con.execute('INSERT INTO image_revisions(code,period,policy,key,observed_at,reason) VALUES (?,?,?,?,?,?)',
                    (code,period,p['hash'],key,now,'source_revised'))
        for e in events:
            key=f"{e['kind']}|{e['structure_at']}|{e['available_at']}"
            if key in old:continue
            contiguous=run and (datetime.fromisoformat(rows[-1]['ts'])-datetime.fromisoformat(run[0])).total_seconds()<=int(period[1:])*60+90
            e.update(observed_at=now,mode='live' if fresh and contiguous and e['available_at']==rows[-1]['ts'] else 'replay')
            # A point observation is historical evidence, not a claim its setup remains valid forever.
            e['reason']+='；'+e['reason_detail'] if e['reason_detail'] else ''
            con.execute('INSERT OR IGNORE INTO image_observations VALUES (?,?,?,?,?)',
                (code,period,p['hash'],key,json.dumps(e,ensure_ascii=False)))
        con.execute('INSERT OR REPLACE INTO image_runs VALUES (?,?,?,?,?)',(code,period,p['hash'],rows[-1]['ts'],fingerprint))


def read(con, code, period):
    setup(con)
    p=rules.load_policy()
    if not p['enabled'] or period not in ('m1','m5'):return None,p
    rows=[json.loads(r[0]) for r in con.execute(
        'SELECT payload FROM image_observations WHERE code=? AND period=? AND policy=?',(code,period,p['hash']))]
    ready=con.execute('SELECT 1 FROM image_runs WHERE code=? AND period=? AND policy=?',(code,period,p['hash'])).fetchone()
    return rows if ready else [],p


def validation(p):
    root=Path(__file__).resolve().parents[2]
    folder=root/'docs/intraday-distillation-20261011/backtest-v1'
    try:
        frozen=json.loads((folder/'frozen-before-results.json').read_text(encoding='utf-8'))
        checked=('config/intraday_image_strategy.json','backend/domain/intraday_images.py',
                 'backend/domain/intraday_t_backtest.py','backend/domain/chan.py')
        if any(hashlib.sha256((root/f).read_bytes()).hexdigest()!=frozen['files'][f] for f in checked):
            return dict(state='version_changed',note='当前规则版本与回测版本不同，不能沿用旧结果。')
        summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
        variants={k:summary[f'm5|{k}|slippage=0.002|all'] for k in ('baseline_chan','fused_full','price_only_ablation')}
        return dict(state='no_improvement_demonstrated',variants=variants,
            note='三股10个完整交易日的5分钟历史回放：基线账户收益3.23%，完整约束融合版2.41%；量价实验版0.48%、23笔闭合T中7盈16亏。未证明提升。完整版缺少历史环境证据，闭合T为0，胜率不可估；账户收益含底仓和未闭合持仓。')
    except (OSError,KeyError,ValueError):
        return dict(state='unvalidated',note='尚无与当前版本一致的回测证据。')
