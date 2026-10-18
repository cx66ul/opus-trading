"""Independent replay of exported fills; validates ledgers and frozen input hashes."""
import hashlib
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/intraday-distillation-20261011/backtest-v1'


def main():
    frozen=json.loads((OUT/'frozen-before-results.json').read_text(encoding='utf-8'))
    for file,digest in frozen['files'].items():
        assert hashlib.sha256((ROOT/file).read_bytes()).hexdigest()==digest,file
    coverage=json.loads((OUT/'coverage.json').read_text(encoding='utf-8'))
    for item in coverage:
        assert hashlib.sha256((OUT/f"{item['code']}-{item['period']}-source.json").read_bytes()).hexdigest()==item['sha256']
    results=json.loads((OUT/'results.json').read_text(encoding='utf-8'))
    fills_checked=0;accounts=0
    for key,group in results.items():
        period,variant,_,_=key.split('|')
        for account in group:
            if 'error' in account:continue
            accounts+=1
            initial=account['initial_capital'];base=account['base_shares']
            raw=json.loads((OUT/f"{account['code']}-{period}-source.json").read_text(encoding='utf-8'))
            prices={b['ts']:b for b in raw}
            start=prices[account['start']]['open']
            cash=initial-base*start;shares=base;available=base;day=None
            curve=account['equity'];indices={p['ts']:i for i,p in enumerate(curve)}
            fills={f['filled_at']:f for f in account['fills']}
            assert len(fills)==len(account['fills'])
            events=json.loads((OUT/f"{account['code']}-{period}-{variant}-signals.json").read_text(encoding='utf-8'))
            for point in curve:
                ts=point['ts'];b=prices[ts]
                if ts[:10]!=day:day=ts[:10];available=shares
                f=fills.get(ts)
                if f:
                    fills_checked+=1
                    assert indices[ts]-indices[f['signal_at']]==2
                    assert f['signal_at'][:10]==day
                    assert any(e['available_at']==f['signal_at'] and e['status']=='confirmed' and e['active']
                        and f['side'] in e['kind'] for e in events)
                    q=f['shares'];px=f['price'];value=q*px
                    fee=max(5,value*.00025)+value*.00001+(value*.0005 if f['side']=='sell' else 0)
                    assert abs(fee-f['fee'])<1e-7
                    assert q%100==0 and q<=b['volume']*.01
                    assert b['low']<=px<=b['high'] and b['low']!=b['high']
                    assert f['available_before']==available
                    if f['side']=='buy':cash-=value+fee;shares+=q
                    else:
                        assert available>=q;available-=q;shares-=q;cash+=value-fee
                    assert shares>=0 and cash>=0 and available==f['available_after']
                assert abs(point['cash']-cash)<1e-6 and point['shares']==shares and point['available']==available
                assert abs(point['equity']-(cash+shares*b['close']))<1e-6
                assert abs(point['buy_hold_equity']-(initial+base*(b['close']-start)))<1e-6
            assert abs(account['net_return_pct']-((curve[-1]['equity']/initial-1)*100))<1e-7
            for t in account['trades']:
                entry,exit=t['entry'],t['exit'];sign=1 if entry['side']=='buy' else -1
                assert entry['side']!=exit['side']
                pnl=sign*(exit['price']-entry['price'])*entry['shares']-entry['fee']-exit['fee']
                assert abs(pnl-t['net_pnl'])<1e-7
    report=dict(accounts_checked=accounts,fills_checked=fills_checked,hashes_unchanged=True,
        ledger_reconciled=True,T1_checked=True,two_bar_latency_checked=True,
        no_actual_exchange_fill_claim=True)
    (OUT/'independent-audit.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
