"""Frozen, paired replay. Keeps full source/parameter/signal/fill audit; no tuning."""
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.data.adapters.tencent import TencentAdapter
from backend.services.intraday import normalize,wallclock,stamp
from backend.domain import intraday_images as rule
from backend.domain.intraday_t_backtest import simulate,aggregate

OUT=ROOT/'docs/intraday-distillation-20261011'
CODES=('600152','603232','603248')


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def complete_days(rows,step):
    by={}
    for b in rows:by.setdefault(b['ts'][:10],[]).append(b)
    wanted=[f'{n//60:02}:{n%60:02}' for a,z in ((570,690),(780,900)) for n in range(a+step,z+1,step)]
    kept=[];excluded=[]
    for day,group in sorted(by.items()):
        continuous=[b for b in group if b['ts'][11:16]!='09:30']
        if [b['ts'][11:16] for b in continuous]==wanted:kept+=group
        else:excluded.append(dict(day=day,observed=len(continuous),required=len(wanted)))
    return kept,excluded


def main():
    OUT.mkdir(exist_ok=True)
    run=OUT/'backtest-v1'
    if (run/'results.json').exists():raise SystemExit('Existing result preserved; do not overwrite a completed run')
    run.mkdir(exist_ok=True)
    policy=rule.load_policy()
    files=['config/intraday_image_strategy.json','backend/domain/intraday_images.py',
           'backend/domain/intraday_t_backtest.py','scripts/backtest_intraday_images.py',
           'backend/domain/chan.py','docs/intraday-distillation-20261011/蒸馏规则与预注册回测口径.md']
    hashes={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}
    dump(run/'frozen-before-results.json',dict(at=stamp(wallclock()),files=hashes,policy=policy,
         universe=CODES,slippages=[.002,.001,.0005],no_parameter_search=True))
    sources={};coverage=[]
    adapter=TencentAdapter()
    try:
        for code in CODES:
            for period in ('m1','m5'):
                path=run/f'{code}-{period}-source.json'
                if path.exists():raw=json.loads(path.read_text(encoding='utf-8'))
                else:
                    raw=adapter.fetch_minute(code,period,320)
                    dump(path,raw)
                rows=normalize(raw,period,wallclock())
                full,excluded=complete_days(rows,int(period[1:]))
                sources[(code,period)]=full
                coverage.append(dict(code=code,period=period,raw_bars=len(rows),complete_bars=len(full),
                    excluded=excluded,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    days=sorted({r['ts'][:10] for r in full})))
    finally:adapter.close()
    dump(run/'coverage.json',coverage)
    all_results={};summaries={}
    for period in ('m1','m5'):
        for variant in ('baseline_chan','fused_full','price_only_ablation'):
            params=dict(policy)
            if variant=='baseline_chan':params['enabled']=False
            datasets={}
            for code in CODES:
                rows=sources[(code,period)]
                events=rule.stream(code,rows,int(period[1:]),rows[-1]['ts'],params,
                    price_only=variant=='price_only_ablation')
                dump(run/f'{code}-{period}-{variant}-signals.json',events)
                datasets[code]=(rows,events)
            for slippage in (.002,.001,.0005):
                for split in ('all','early_half','late_half'):
                    group=[]
                    for code,(rows,events) in datasets.items():
                        days=sorted({b['ts'][:10] for b in rows})
                        selected=set(days if split=='all' else days[:len(days)//2] if split=='early_half' else days[len(days)//2:])
                        # Signals must have arisen in the reported subwindow; warmup never executes trades.
                        chosen=[b for b in rows if b['ts'][:10] in selected]
                        ev=[e for e in events if e['available_at'][:10] in selected]
                        result=simulate(code,chosen,ev,slippage,policy['max_t_fraction'])
                        result['signal_status_counts']=dict(Counter(e['status'] for e in ev))
                        group.append(result)
                    key=f'{period}|{variant}|slippage={slippage}|{split}'
                    all_results[key]=group;summaries[key]=aggregate(group)
                    if split=='all':print(key,json.dumps(summaries[key],ensure_ascii=False),flush=True)
    for f,h in hashes.items():assert hashlib.sha256((ROOT/f).read_bytes()).hexdigest()==h,'Code changed while running'
    dump(run/'results.json',all_results)
    dump(run/'summary.json',summaries)
    dump(run/'integrity.json',dict(source_hashes=coverage,code_hashes=hashes,
        completed_at=stamp(wallclock()),not_true_out_of_sample=True,market_context='absent_no_imputation',
        result_sha256=hashlib.sha256((run/'results.json').read_bytes()).hexdigest()))


if __name__=='__main__':main()
