import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from backend.services import tracked_news as T
from backend.services.news_collection import save_items

NOW = datetime.fromisoformat('2026-10-11T15:00:00+08:00')


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'test.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE stock(id TEXT,name TEXT,industry TEXT,concept TEXT);
            CREATE TABLE watchlist(code TEXT); CREATE TABLE position(code TEXT,status TEXT,shares INTEGER);
            INSERT INTO stock VALUES('600001','甲方能源','能源','["原油"]');
            INSERT INTO stock VALUES('600002','乙方航空','航空',NULL);
            INSERT INTO watchlist VALUES('600002');
            INSERT INTO position VALUES('600001','holding',NULL);''')
    save_items([dict(source='jin10',external_id='123',ts=NOW.isoformat(),title='',
                     content='甲方能源因油价上涨受益，乙方航空燃油成本增加。',related=[],tags=[])], NOW, path)
    return path


class Provider:
    name = 'test'
    model = 'test-model'

    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def chat(self, system, user, as_of, **kwargs):
        request = json.loads(user)
        code = request['target']['code']
        self.calls.append(code)
        return json.dumps(dict(related=True, direction='bull' if code=='600001' else 'bear',
                               confidence=.9, strength=3, reason='收入与成本传导不同',
                               evidence='油价上涨' if code=='600001' else '燃油成本增加'))

    def audit(self, **kwargs):
        pass


def test_held_first_per_stock_opposite_sentiment_and_idempotence(database):
    provider = Provider()
    result = T.process_batch(NOW, database, provider)
    assert result['processed'] == 2
    assert provider.calls == ['600001', '600002']
    out = T.read_tracked(NOW, database)
    assert [r['direction'] for r in out['rows']] == ['bull', 'bear']
    assert [r['held'] for r in out['rows']] == [1, 0]
    assert T.process_batch(NOW, database, provider)['processed'] == 0
    assert len(provider.calls) == 2


def test_match_boundaries_sector_and_missing_metadata():
    stock = dict(code='600001',name='甲方能源',industry=None,concept=None)
    assert T.match('电话160000123',stock) is None
    assert T.match('股票600001',stock) == ('direct','600001')
    assert T.match('油价上涨',stock) is None
    stock['concept'] = '["原油"]'
    assert T.match('原油涨价',stock) == ('sector_candidate','原油')
    assert T.match('简称报道',stock,[dict(market='cn',symbol='sh600001')]) == ('source_link','600001')
    assert T.match('简称报道',stock,[dict(market='us',symbol='600001')]) is None


def test_sina_local_timestamp_not_treated_as_utc(database):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE raw_news SET ts='2026-10-11 14:59:00'")
    assert T.process_batch(NOW,database,Provider())['processed'] == 2
    assert len(T.read_tracked(NOW,database)['rows']) == 2


def test_fabricated_evidence_and_uncertainty():
    result = dict(related=True,direction='bull',strength=3,confidence=.9,reason='依据',evidence='原文不存在')
    with pytest.raises(ValueError):
        T.validate_result(result,'真实原文')
    result.update(evidence='真实',confidence=.4)
    assert T.validate_result(result,'真实原文')['direction'] == 'unknown'
    result.update(confidence=float('nan'))
    with pytest.raises(ValueError):
        T.validate_result(result,'真实原文')


def test_model_unavailable_keeps_pending(database):
    provider = Provider()
    provider.available = lambda: False
    assert T.process_batch(NOW,database,provider)['state'] == 'model_unavailable'
    rows = T.read_tracked(NOW,database)['rows']
    assert len(rows) == 2
    assert all(r['state']=='pending' and r['direction']=='unknown' for r in rows)


def test_error_retry_and_removed_stock(database):
    provider = Provider()
    provider.chat = lambda *a, **kw: 'invalid json'
    assert T.process_batch(NOW,database,provider)['processed'] == 2
    rows = T.read_tracked(NOW,database)['rows']
    assert all(r['state']=='error' and r['direction']=='unknown' for r in rows)
    assert T.process_batch(NOW,database,provider)['processed'] == 0
    assert T.process_batch(NOW+timedelta(minutes=6),database,Provider())['processed'] == 2
    with sqlite3.connect(database) as db:
        db.execute('DELETE FROM watchlist')
    assert len(T.read_tracked(NOW,database)['rows']) == 1


def test_stale_claim_recovered_and_historical_news_excluded(database):
    con = T.connect(database)
    T.enqueue(con,NOW)
    with con:
        con.execute("UPDATE tracked_news_tag SET state='processing',updated_at=?",((NOW-timedelta(minutes=6)).isoformat(),))
    con.close()
    assert T.process_batch(NOW,database,Provider())['processed'] == 2
    assert T.read_tracked(NOW+timedelta(days=8),database)['rows'] == []


def test_indirect_rejection_not_presented_as_stock_benefit(database):
    with sqlite3.connect(database) as con:
        con.execute("UPDATE raw_news SET payload=?",(json.dumps(dict(content='原油供应增加')),))
    provider=Provider()
    provider.chat=lambda *a,**kw: '{"related":false}'
    T.process_batch(NOW,database,provider)
    assert T.read_tracked(NOW,database)['rows']==[]
