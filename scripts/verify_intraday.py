"""Real public feed + causal prefix acceptance. Run before starting the new worker."""
import json
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.services import intraday as m
from backend.data.adapters.tencent import TencentAdapter


def main():
    out=ROOT/'docs'/'intraday-implementation-20261011'
    out.mkdir(exist_ok=True)
    service=m.IntradayService(ROOT/'data'/'hawk.sqlite')
    adapter=TencentAdapter()
    results=[]
    began=time.monotonic()
    try:
        with closing(m.connect()) as con:
            for stock in service.pool():
                for period in m.PERIODS:
                    rows=adapter.fetch_minute(stock['code'],period,320)
                    result=m.ingest(con,stock['code'],period,rows,m.wallclock())
                    states=[json.loads(r[0]) for r in con.execute(
                        'SELECT payload FROM signals WHERE code=? AND period=?',(stock['code'],period))]
                    result.update(active=sum(s['active'] for s in states),
                        candidates=sum(s['status']=='candidate' for s in states),
                        replay=sum(s['mode']=='replay' for s in states))
                    if stock['code']=='600152' and period=='m1':
                        (out/'minute-source-sample.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
                        (out/'signal-journal-sample.json').write_text(json.dumps(states,ensure_ascii=False,indent=2),encoding='utf-8')
                    results.append(result)
                    print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        adapter.close()
    (out/'real-feed-acceptance.json').write_text(json.dumps(dict(at=m.stamp(m.wallclock()),
        seconds=round(time.monotonic()-began,2),results=results),ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
