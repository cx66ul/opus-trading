"""三买（3buy）失效归因实验 —— 只读，不写库，不污染生产数据。

问题（M8 回测发现）：3buy 胜率 35.7%、平均收益 -4.15%，
而 1buy/2buy 均为 100%胜率 / +10% 收益。同一套缠论引擎，
为什么只有三买差这么多？

假设列表（逐个用真实数据检验，不靠猜）：
  H1 弱三买（weak，未回抽确认）混入统计 —— 它在「突破笔最高点」买入，
     那是追高，天然最差。若把 strong 单独看，胜率应显著回升。
  H2 三买的「离开中枢」判定太宽松 —— 只要笔 high > zs.high 就算离开，
     而中枢上沿本就在价格附近，容易被小幅突破触发。
  H3 三买信号时间戳取的是回抽笔的 end_date，等于「回抽已经走完才买入」，
     比 1buy/2buy（买在低点）晚了整整一段，5日窗口里套利空间被吃掉。
  H4 三买没有中枢震荡时长过滤 —— 极短中枢产生的三买是噪声。

跑法： python scripts/diag_3buy.py --limit 400
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb                                            # noqa: E402
import pandas as pd                                      # noqa: E402

from backend.domain import chan                           # noqa: E402

CST = timezone(timedelta(hours=8))
PARQUET = "data/snapshots/2026-10-08/kline_daily.parquet"


def _forward_ret(bars: list[chan.Bar], date: str, horizon: int) -> float | None:
    """从date 当日收盘到 horizon 日后的涨跌幅 %（可成交价口径）。"""
    idx = next((i for i, b in enumerate(bars) if b.d == date), None)
    if idx is None or idx + horizon >= len(bars):
        return None
    a, b = bars[idx].c, bars[idx + horizon].c
    if a <= 0:
        return None
    return round((b / a - 1) * 100, 2)


def _forward_ret_at(bars: list[chan.Bar], date: str, price: float,
                    horizon: int) -> float | None:
    """按**信号自身报价**起算的前向收益。

    与 _forward_ret 的区别：后者用当日收盘，前者用信号价。
    两者差值能暴露「信号价是否可成交」—— 若信号价远高于收盘，
    说明报价口径有问题（这就是弱三买用突破笔最高价的病灶）。
    """
    idx = next((i for i, b in enumerate(bars) if b.d == date), None)
    if idx is None or idx + horizon >= len(bars):
        return None
    if price <= 0:
        return None
    return round((bars[idx + horizon].c / price - 1) * 100, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--min-bars", type=int, default=120)
    args = ap.parse_args()

    con = duckdb.connect()
    codes = [r[0] for r in con.execute(
        f"""SELECT code FROM read_parquet('{PARQUET}')
            GROUP BY code HAVING count(*) >= {args.min_bars}
            ORDER BY code LIMIT {args.limit}""").fetchall()]
    print(f"样本 {len(codes)} 只，窗口 {args.horizon} 日\n")

    # key -> list[(ret, extra)]
    groups: dict[str, list] = defaultdict(list)
    by_price: dict[str, list] = defaultdict(list)
    zs_amp_by_key: dict[str, list] = defaultdict(list)
    delay_by_key: dict[str, list] = defaultdict(list)

    for code in codes:
        rows = con.execute(
            f"""SELECT trade_date, open, high, low, close, volume
                FROM read_parquet('{PARQUET}') WHERE code = ?
                ORDER BY trade_date""", [code]).fetchall()
        recs = [{"d": str(r[0]), "open": r[1], "high": r[2],
                 "low": r[3], "close": r[4], "volume": r[5]} for r in rows]
        bars = chan.clean_bars(recs)
        if len(bars) < args.min_bars:
            continue
        merged = chan.merge_klines(bars)
        fr = chan.find_fractals(merged)
        bis = chan.build_bi(merged, fr, bars=bars)
        if len(bis) < 3:
            continue
        zss = chan.build_zhongshu(bis, bars)
        _, _, hist = chan.macd_series([b.c for b in bars])
        bcs = chan.detect_beichi(bis, hist, bars)
        sigs = chan.scan_signals(bis, zss, bcs, bars, hist, as_of=datetime.now(CST))

        for s in sigs:
            # ★ 只统计「已走完 horizon 且尚未走完（历史信号）」的样本，
            #   否则最后几天的信号必然亏损，会污染统计。
            if bars[-1].d <= s.date:
                continue
            ret = _forward_ret(bars, s.date, args.horizon)
            if ret is None:
                continue
            groups[s.kind].append((ret, s.level))
            rp = _forward_ret_at(bars, s.date, s.price, args.horizon)
            if rp is not None:
                by_price[f"{s.kind}/{s.level}"].append(rp)
            if s.kind in ("1buy", "2buy", "3buy"):
                zs_amp_by_key[s.kind].append(
                    s.zhongshu.amplitude if s.zhongshu else None)
                # 信号日与「突破笔结束日」的间隔（交易日）
                try:
                    di = next(i for i, b in enumerate(bars) if b.d == s.date)
                    delay_by_key[s.kind].append(di)
                except StopIteration:
                    pass

    print("=" * 78)
    print(f"按信号类型（{args.horizon} 日窗口，剔除未走完的近期信号）")
    print("=" * 78)
    print(f"{'类型':>8s}{'样本':>7s}{'胜率':>9s}{'平均收益':>10s}{'中位收益':>10s}")
    for kind in sorted(groups):
        rets = [r for r, _ in groups[kind]]
        n = len(rets)
        if n == 0:
            continue
        wins = [r for r in rets if r > 0]
        print(f"{kind:>8s}{n:>7d}{len(wins)/n*100:>8.1f}%"
              f"{sum(rets)/n:>10.2f}%{sorted(rets)[n//2]:>10.2f}%")

    # ---- H1: strong vs weak
    print()
    print("=" * 78)
    print("H1 假设：三买是否被 weak（未回抽确认）拖垮？")
    print("=" * 78)
    for kind in ("1buy", "2buy", "3buy", "3sell"):
        sub = defaultdict(list)
        for r, lv in groups.get(kind, []):
            sub[lv].append(r)
        if not sub:
            continue
        print(f"  {kind}:")
        for lv in sorted(sub):
            rets = sub[lv]
            n = len(rets)
            wins = [r for r in rets if r > 0]
            print(f"    {lv:>7s} n={n:<4d} 胜率={len(wins)/n*100:>5.1f}% "
                  f"平均={sum(rets)/n:>7.2f}% 中位={sorted(rets)[n//2]:>7.2f}%")

    # ---- H3: 信号报价是否可成交
    print()
    print("=" * 78)
    print("H3 信号报价 vs 当日收盘：报价口径是否可成交")
    print("=" * 78)
    print("  （若两种口径收益差很大，说明报价偏离收盘价，现实中难成交）")
    for key in sorted(by_price):
        at = by_price[key]
        n = len(at)
        w = len([x for x in at if x > 0])
        close_side = [r for r, lv in groups.get(key.split("/")[0], [])
                      if lv == key.split("/")[1]]
        cw = (len([x for x in close_side if x > 0]) / len(close_side) * 100
              if close_side else float("nan"))
        print(f"  {key:>16s} n={n:<5d} 按信号价 胜率={w/n*100:>5.1f}% "
              f"平均={sum(at)/n:>7.2f}%  |  按收盘价 胜率={cw:>5.1f}% "
              f"平均={sum(close_side)/len(close_side):>7.2f}%"
              if close_side else f"  {key:>16s} n={n:<5d} 按信号价 {w/n*100:.1f}%")
    print()
    print("  ★★ 读这张表前必读（选择偏差警告）：")
    print("     「按信号价」普遍接近 100% 胜率，**这不是策略有多准，而是幸存者偏差**：")
    print("     买点信号价取的是回调笔的最低点，卖点取的是反弹笔的最高点 ——")
    print("     都在事后才know 的极值上。实盘按当日收盘下单，拿不到这个价。")
    print("     ⇒ **唯一可用于实盘决策的口径是「按收盘价」**，「按信号价」仅供理解信号成因。")
    print("     3buy/weak 两种口径一致（修复后它本就取收盘价），说明它的低胜率是真实的市场行为。")

    # ---- H2: 中枢振幅
    print()
    print("=" * 78)
    print("H2 假设：三买是否集中在振幅过小的中枢（噪声中枢）？")
    print("=" * 78)
    for kind in ("1buy", "2buy", "3buy"):
        amps = [a for a in zs_amp_by_key.get(kind, []) if a is not None]
        if not amps:
            print(f"  {kind}: 无中枢信息（一买/二买不绑定中枢，属预期）")
            continue
        amps_sorted = sorted(amps)
        print(f"  {kind}: n={len(amps)} 最小={amps_sorted[0]:.2f}% "
              f"中位={amps_sorted[len(amps)//2]:.2f}% 最大={amps_sorted[-1]:.2f}%")

    # ---- H4: 按中枢振幅分档看三买胜率
    print()
    print("=" * 78)
    print("H4 三买按中枢振幅分档（振幅越小=中枢越窄=信号越可疑）")
    print("=" * 78)
    buckets = defaultdict(list)
    for (r, lv), amp in zip(
            [x for k in ("3buy",) for x in groups[k]], zs_amp_by_key.get("3buy", [])):
        if amp is None:
            continue
        b = ("<5%" if amp < 5 else "5-10%" if amp < 10
             else "10-20%" if amp < 20 else ">=20%")
        buckets[b].append(r)
    for b in ("<5%", "5-10%", "10-20%", ">=20%"):
        rets = buckets.get(b, [])
        if not rets:
            print(f"  振幅 {b:>6s}: 无样本")
            continue
        wins = [r for r in rets if r > 0]
        print(f"  振幅 {b:>6s} n={len(rets):<4d} 胜率={len(wins)/len(rets)*100:>5.1f}% "
              f"平均={sum(rets)/len(rets):>7.2f}%")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())