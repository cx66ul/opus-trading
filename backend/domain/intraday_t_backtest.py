"""Paired overnight-inventory T replay. No same-day resale of newly bought shares."""
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean


def fees(side, value):
    return max(5.,value*.00025)+value*.00001+(value*.0005 if side=='sell' else 0)


def simulate(code, rows, events, slippage=.002, fraction=1/3):
    if not rows:return dict(error='no_complete_days')
    initial=100000.;start=rows[0]['open']
    base=int(50000/start/100)*100
    lot=int(base*fraction/100)*100
    cash=initial-base*start;shares=base;available=base
    active=None;trades=[];fills=[];equity=[];skips=Counter();queue={};day=None;prior=None
    events_by={}
    for s in events:
        if s['status']=='confirmed' and s['active']:
            events_by.setdefault(s['available_at'],[]).append(s)
    for i,b in enumerate(rows):
        current_day=b['ts'][:10]
        if current_day!=day:
            prior=rows[i-1]['close'] if i else None
            available=shares;day=current_day
        orders=queue.pop(i,[])
        for order in orders:
            side=order['side']
            if order['day']!=day:skips['signal_expired_next_day']+=1;continue
            if active and side==active['side']:continue
            if not lot:skips['insufficient_lot']+=1;continue
            if prior is None:skips['missing_previous_unadjusted_close']+=1;continue
            # Fixed universe: all three are non-ST Shanghai main-board stocks.
            upper=float(Decimal(str(prior*1.1)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
            lower=float(Decimal(str(prior*.9)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
            if b['volume']<=0 or b['high']==b['low'] or b['open']>=upper or b['open']<=lower:
                skips['unverified_limit_or_flat_bar_fill']+=1;continue
            if lot>b['volume']*.01:skips['participation_above_1pct']+=1;continue
            px=b['open']*(1+slippage if side=='buy' else 1-slippage)
            if not b['low']<=px<=b['high']:skips['slipped_price_outside_bar']+=1;continue
            cost=fees(side,px*lot)
            if side=='buy' and cash<lot*px+cost:skips['insufficient_cash']+=1;continue
            if side=='sell' and min(shares,available)<lot:skips['T1_unavailable_shares']+=1;continue
            before_available=available
            if side=='buy':cash-=px*lot+cost;shares+=lot
            else:cash+=px*lot-cost;shares-=lot;available-=lot
            fill=dict(code=code,side=side,signal_at=order['signal_at'],filled_at=b['ts'],price=px,
                raw_open=b['open'],shares=lot,fee=cost,available_before=before_available,
                available_after=available,rule_ids=order['rules'])
            fills.append(fill)
            if active:
                sign=1 if active['side']=='buy' else -1
                net=sign*(px-active['price'])*lot-active['fee']-cost
                trades.append(dict(entry=active,exit=fill,net_pnl=net,return_pct=net/(active['price']*lot)*100))
                active=None
            else:active=fill
            break  # one execution per candle, no instantaneous same-bar round trips
        eq=cash+shares*b['close'];benchmark=initial+base*(b['close']-start)
        equity.append(dict(ts=b['ts'],equity=eq,buy_hold_equity=benchmark,shares=shares,cash=cash,available=available))
        sigs=events_by.get(b['ts'],[])
        if sigs:
            buy=[s for s in sigs if 'buy' in s['kind']]
            sell=[s for s in sigs if 'sell' in s['kind']]
            # Risk observations take precedence when flat. Existing T closes only on opposite signal.
            target=('sell' if active['side']=='buy' else 'buy') if active else ('sell' if sell else 'buy')
            chosen=sell if target=='sell' else buy
            if chosen and i+2<len(rows):
                queue[i+2]=[dict(side=target,day=day,signal_at=b['ts'],rules=[s['rule_id'] for s in chosen])]
    peak=initial;dd=0
    for point in equity:
        peak=max(peak,point['equity']);dd=max(dd,(peak-point['equity'])/peak*100)
    wins=sum(t['net_pnl']>0 for t in trades)
    return dict(code=code,initial_capital=initial,base_shares=base,t_shares=lot,days=len({r['ts'][:10] for r in rows}),
        start=rows[0]['ts'],end=rows[-1]['ts'],net_return_pct=(equity[-1]['equity']/initial-1)*100,
        buy_hold_return_pct=(equity[-1]['buy_hold_equity']/initial-1)*100,
        excess_vs_hold_pp=(equity[-1]['equity']-equity[-1]['buy_hold_equity'])/initial*100,
        max_drawdown_pct=dd,closed_trades=len(trades),wins=wins,losses=len(trades)-wins,
        win_rate_pct=100*wins/len(trades) if len(trades)>=10 else None,
        unclosed_t=active,fees=sum(f['fee'] for f in fills),skips=dict(skips),fills=fills,trades=trades,equity=equity)


def aggregate(results):
    valid=[r for r in results if 'error' not in r]
    trades=sum(r['closed_trades'] for r in valid);wins=sum(r['wins'] for r in valid)
    return dict(accounts=len(valid),closed_trades=trades,wins=wins,losses=trades-wins,
        win_rate_pct=100*wins/trades if trades>=10 else None,
        net_return_pct=mean(r['net_return_pct'] for r in valid) if valid else None,
        buy_hold_return_pct=mean(r['buy_hold_return_pct'] for r in valid) if valid else None,
        excess_vs_hold_pp=mean(r['excess_vs_hold_pp'] for r in valid) if valid else None,
        worst_account_drawdown_pct=max((r['max_drawdown_pct'] for r in valid),default=None),
        unclosed_t_count=sum(r['unclosed_t'] is not None for r in valid),
        fees=sum(r['fees'] for r in valid))
