# -*- coding: utf-8 -*-
"""市场状态门控的样本外验证（M9 · 把 D16 结论变成可执行规则）。

★ 验证什么
----------
D16 分行情统计发现「震荡市超额 -1.28%」，但那是**归因**（事后段标签）。
本脚本问的是**决策**：如果「只在上涨市出买点」，样本外表现会不会变好？

★★★ 三条必须守住的口径
--------------------------
1. **门控只用因果信息**（`backend.domain.regime_gate`，point-in-time）。
   段标签用了整段平均（含未来 64 天），**实测 16% 的交易日不一致**
   ⇒ 用段标签做门控就是前视偏差，会得到虚高的结果。这里绝不使用。

2. **全样本连续跑，只按入场日统计样本外**。
   不能按段切片重跑（会断 `seen` 连续性，信号归零，同 `run_oos` 的坑）。

3. **样本量稳定性检验**。
   D14 实测 60 只通过、200 只不通过、400 只又通过 ⇒ 只报一个样本量的
   结果等于在 7 个数字里挑好看的那个。`run_oos.stability_check` 已封装。

★ 必须有的对照
--------------
- `none`：不做门控（基线）
- `up`：只在上涨市出信号（D16 建议）
- `no_flat`：避开震荡市（up + down 都做）
- ★ `lookahead`：故意用段标签做门控 —— **用来量化前视偏差值多少钱**，
  必须明显好于因果门控，否则说明因果门控实现错了。

运行：
    python scripts/run_regime_gate.py --limit 200 --scan 800 --stability
"""
from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb                                   # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "rb", _ROOT / "scripts" / "run_backtest.py")
rb = importlib.util.module_from_spec(_spec)
sys.modules["rb"] = rb
_spec.loader.exec_module(rb)

_spec2 = importlib.util.spec_from_file_location(
    "oo", _ROOT / "scripts" / "run_oos.py")
oo = importlib.util.module_from_spec(_spec2)
sys.modules["oo"] = oo
_spec2.loader.exec_module(oo)

from backend.domain.backtest import CostModel, backtest    # noqa: E402
from backend.domain import regime_gate as rg                # noqa: E402

CST = timezone(timedelta(hours=8))

# 门控方案 → 允许的市场状态
GATES = {
    "none": None,                    # 不过滤（基线）
    "up": ("up",),                   # 只在上涨市出信号（D16 建议）
    "no_flat": ("up", "down"),       # 只避开震荡市
    "all": ("up", "flat", "down"),   # 显式不过滤（等同 none，做一致性校验）
}

# ★ `leaky` 不在 GATES 里 —— 它走 `_make_identical_gate` 的同口径对照分支。
# ★★ 口径教训：第一版直接把「段标签门控」与「因果门控」比超额，
#   得出「前视反而更差」的荒谬结论。原因：两者选的日期集合不同
#   （实测 Jaccard 仅 84.9%），差值混了门控口径差异，不是前视的代价。
#   正确做法是**锁定同一批日期**，只改「判定依据」是否含未来。


def build_gates(con, window: int = 60, up_th: float = 8.0,
                smooth: int = 10) -> dict:
    """构造因果门控序列 + 段标签（前向偏差对照用）。"""
    rows = con.execute(
        f"""SELECT trade_date, avg(close) AS c FROM
            read_parquet('{rb.PARQUET.as_posix()}')
            GROUP BY trade_date ORDER BY trade_date"""
    ).fetchall()
    series = rg.build_regime_series(rows, window=window,
                                    up_th=up_th, down_th=-up_th)

    # ★ 段标签仅用于「量化前视偏差」，绝不用于正式门控
    _s = importlib.util.spec_from_file_location(
        "rsx", _ROOT / "scripts" / "run_regime_split.py")
    rsx = importlib.util.module_from_spec(_s)
    sys.modules["rsx"] = rsx
    _s.loader.exec_module(rsx)
    segs = rsx.market_segments(con, window)

    def seg_regime(d: str) -> str:
        for s in segs:
            if s["start"] <= d <= s["end"]:
                return s["regime"]
        return "unknown"

    up_dates = {d for d in series.dates
                if series.smooth_regime(d, smooth) == "up"}
    leaky_up = {d for d in series.dates if seg_regime(d) == "up"}
    jac = len(up_dates & leaky_up) / max(1, len(up_dates | leaky_up)) * 100

    return {"series": series, "seg_of": seg_regime, "smooth": smooth,
            "segs": segs, "up_dates": up_dates, "leaky_up_dates": leaky_up,
            "jaccard": jac}


def make_gated_entry_fn(shared: dict, gate: dict, mode: str):
    """把门控包在 entry_fn 外层。

    ★ 门控放在**信号层**而非引擎层：引擎不知道「市场状态」，
      由策略决定今天该不该出信号（与 D13「系统给参考」定位一致）。
    """
    if mode in ("causal", "leaky"):
        # ★★ 同口径前视对照：两个变体选**完全相同的日期**（`up_dates`），
        #   只让「判定依据」一个含未来、一个只用历史。
        #
        # 第一版我拿「段标签门控」与「因果门控」直接比超额，得出
        # 「前视反而更差（+1.68% vs +2.77%）」的**荒谬结论**。
        # 原因：两者选的根本不是同一批交易日 ——
        # 实测 Jaccard 仅 84.9%（仅段标签 15 天、仅因果 11 天）。
        # ⇒ 差值混了「门控口径差异」，不是「前视的收益」。
        return _make_identical_gate(shared, gate, mode)

    states = GATES[mode]
    if states is None or set(states) >= set(("up", "flat", "down")):
        return rb.make_entry_fn(shared.get("bars", []), shared)

    series = gate["series"]
    smooth = gate["smooth"]
    inner = rb.make_entry_fn(shared.get("bars", []), shared)

    def gated(bar, i, allb):
        if not series.allow(bar.d, states, smooth):
            return None
        return inner(bar, i, allb)
    return gated


def _make_identical_gate(shared: dict, gate: dict, variant: str):
    """★ 同口径前视对照：两个门控选**完全相同**的日期，只改判定依据。

    `variant`:
      "causal" —— 只用截至当日的 roll（真实可用）
      "leaky"  —— 额外要求段标签也判为 up（含未来，不可用）

    这样差值才真的是「前视偏差的代价」，不混入门控口径差异。
    ★ 两个变体都以 `up_dates`（因果集合）为基准，
      所以 `leaky` 只会**更严**（交集会缩小），不会出现反向套利。
    """
    up_dates = gate["up_dates"]
    seg_of = gate["seg_of"]
    inner = rb.make_entry_fn(shared.get("bars", []), shared)

    def gated(bar, i, allb):
        if bar.d not in up_dates:
            return None
        if variant == "leaky" and seg_of(bar.d) != "up":
            return None
        return inner(bar, i, allb)
    return gated


def stats_with_benchmark(trades, bench: list, label: str) -> dict:
    """统计 + 大盘超额（`stats_of` 不算超额，这里补上）。

    ★ 不能只调 `oo.stats_of` —— 它不返回超额，
      而 D15/D16 的结论**全部**建立在超额之上。
      绝对收益会把「跟跌」误判成「亏」。
    """
    base = oo.stats_of(trades, label)
    ex = [(t.ret_pct, b) for t, b in zip(trades, bench) if b is not None]
    rets = [e[0] for e in ex]
    if not rets:
        base.update({"excess_avg": None, "excess_winrate": None,
                     "bench_avg": None, "excess_median": None,
                     "excess_note": "无基准数据"})
        return base
    n = len(rets)
    diffs = sorted(r - b for r, b in ex)
    base.update({
        "excess_avg": round(sum(diffs) / n, 2),
        "excess_median": diffs[n // 2],
        "excess_winrate": (round(len([x for x in diffs if x > 0]) / n, 4)
                           if n >= 10 else None),
        "bench_avg": round(sum(b for _, b in ex) / n, 2),
        "excess_note": "" if n >= 10 else f"超额样本仅 {n} 条，不显示胜率",
    })
    return base


def run_gated(codes: list[str], con, gate: dict, mode: str,
              stop_pct: float = 5.0, exit_mode: str = "chan",
              oos_ratio: float = 0.4,
              cost: CostModel | None = None) -> dict:
    """全样本连续跑一次门控回测，返回样本内/外统计。"""
    cost = cost or CostModel()
    trades = []
    for code in codes:
        bars = rb.load_bars(con, code)
        if len(bars) < 150:
            continue
        shared = {"seen": set(), "last_i": -1, "buys": [], "sells": [],
                  "zs": None, "sell_levels": ("strong",), "bars": bars}
        entry = make_gated_entry_fn(shared, gate, mode)
        r = backtest(code, bars, entry, strategy_id="chanlun_base",
                     exit_fn=rb.make_exit_fn(shared, mode=exit_mode),
                     stop_loss_pct=stop_pct, cost=cost)
        trades.extend(r.trades)

    if not trades:
        return {"error": "无成交"}

    ts = sorted(trades, key=lambda t: t.entry_date)
    bm = rb.build_benchmark(con, ts)
    bench = bm.rets if bm else []
    split = int(len(ts) * (1 - oos_ratio))
    split_date = ts[split - 1].entry_date if split else ts[0].entry_date

    return {
        "total": len(ts),
        "split_date": split_date,
        "train": stats_with_benchmark(ts[:split], bench[:split], "训练段"),
        "oos": stats_with_benchmark(ts[split:], bench[split:], "样本外"),
    }


def print_compare(rows: dict, oos_ratio: float) -> None:
    print()
    print("=" * 96)
    print("市场状态门控对比（门控用 point-in-time 因果信息）")
    print("=" * 96)
    print(f"{'门控':>10s}{'笔数':>7s}{'样本外笔数':>11s}{'样本外胜率':>11s}"
          f"{'样本外平均':>11s}{'样本外超额':>11s}{'超额胜率':>10s}")
    for name, r in rows.items():
        if r.get("error"):
            print(f"{name:>10s}  {r['error']}")
            continue
        o = r["oos"]
        if o.get("n", 0) < 10:
            print(f"{name:>10s}{r['total']:>7d}{o.get('n', 0):>11d}"
                  f"{'不足(样本<10)':>11s}{'—':>11s}{'—':>11s}{'—':>10s}")
            continue
        wr = f"{o['winrate']:.1%}" if o.get("winrate") is not None else "不足"
        ew = (f"{o['excess_winrate']:.1%}"
              if o.get("excess_winrate") is not None else "不足")
        ex = (f"{o['excess_avg']:+.2f}%" if o.get("excess_avg") is not None
              else "—")
        print(f"{name:>10s}{r['total']:>7d}{o['n']:>11d}{wr:>11s}"
              f"{o['avg_ret']:>10.2f}%{ex:>11s}{ew:>10s}")
    print()
    print(f"  训练段 = 前 {(1 - oos_ratio) * 100:.0f}% 成交，样本外 = 后 "
          f"{oos_ratio * 100:.0f}%（按入场日切）")
    print("  ★ 样本外笔数 <10 时不显示胜率（样本太少，数字会误导）")


def main() -> int:
    ap = argparse.ArgumentParser(description="市场状态门控验证")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--scan", type=int, default=300)
    ap.add_argument("--stop-pct", type=float, default=5.0)
    ap.add_argument("--exit-mode", default="chan")
    ap.add_argument("--oos-ratio", type=float, default=0.4)
    ap.add_argument("--up-th", type=float, default=8.0)
    ap.add_argument("--smooth", type=int, default=10)
    ap.add_argument("--stability", action="store_true",
                    help="样本量稳定性检验（D14 已证明小样本会翻转）")
    args = ap.parse_args()

    if not rb.PARQUET.exists():
        print(f"Parquet 不存在: {rb.PARQUET}")
        return 1

    con = duckdb.connect()
    codes = [r[0] for r in con.execute(
        f"""SELECT code FROM read_parquet('{rb.PARQUET.as_posix()}')
            GROUP BY code HAVING count(*) >= 200
            ORDER BY code LIMIT {args.scan}""").fetchall()][: args.limit]

    gate = build_gates(con, up_th=args.up_th, smooth=args.smooth)
    series = gate["series"]
    cov_all = series.coverage(("up",), args.smooth)
    cov_nf = series.coverage(("up", "down"), args.smooth)
    print("=" * 96)
    print("市场状态门控验证 · D16 结论落地")
    print("=" * 96)
    print(f"标的 {len(codes)} 只 | 止损 {args.stop_pct}% | 离场 {args.exit_mode}"
          f" | 阈值 ±{args.up_th}% | 平滑 {args.smooth} 日")
    print(f"门控覆盖率（因果）：只在 up = {cov_all['allowed']:.1f}% 的交易日"
          f" | 避开 flat = {cov_nf['allowed']:.1f}%")
    print(f"★ 状态分布 up {cov_all['up']:.1f}% / flat {cov_all['flat']:.1f}%"
          f" / down {cov_all['down']:.1f}%")

    cm = CostModel()
    rows = {}
    for mode in ("none", "up", "no_flat", "all"):
        rows[mode] = run_gated(codes, con, gate, mode,
                               stop_pct=args.stop_pct,
                               exit_mode=args.exit_mode,
                               oos_ratio=args.oos_ratio, cost=cm)
    print_compare(rows, args.oos_ratio)

    # 一致性校验：all 必须与 none 完全一致（都是不过滤）
    n, a = rows.get("none", {}), rows.get("all", {})
    if not n.get("error") and not a.get("error"):
        same = n["total"] == a["total"]
        print(f"  一致性校验：all 与 none 成交数{'一致' if same else '★不一致'} "
              f"({n['total']} vs {a['total']}) —— 两者都应不过滤")

    # ★ 前视偏差的代价：★同口径★（锁定同一批日期，只改判定依据）
    print()
    print("=" * 96)
    print("前视偏差的代价（同口径对照）")
    print("=" * 96)
    print(f"  因果 up 日 {len(gate['up_dates'])} 天 | 段标签 up 日 "
          f"{len(gate['leaky_up_dates'])} 天 | Jaccard {gate['jaccard']:.1f}%")
    print(f"  ★ Jaccard <100% ⇒ 两个口径选的**不是同一批日子**，")
    print(f"    直接比超额会混入门控口径差异（前版踩过，得到「前视反而更好」的假结论）")
    print()
    cmp_rows = {}
    for variant, label in (("causal", "因果(可用)"), ("leaky", "段标签(含未来)")):
        r = run_gated(codes, con, gate, variant,
                      stop_pct=args.stop_pct, exit_mode=args.exit_mode,
                      oos_ratio=args.oos_ratio, cost=cm)
        cmp_rows[label] = r
    print(f"{'口径':>14s}{'笔数':>7s}{'样本外笔数':>11s}{'样本外超额':>11s}")
    for label, r in cmp_rows.items():
        if r.get("error"):
            print(f"{label:>14s}  {r['error']}")
            continue
        o = r["oos"]
        ex = (f"{o['excess_avg']:+.2f}%" if o.get("excess_avg") is not None
              else "—")
        print(f"{label:>14s}{r['total']:>7d}{o['n']:>11d}{ex:>11s}")
    cc, ll = cmp_rows.get("因果(可用)", {}), cmp_rows.get("段标签(含未来)", {})
    if not cc.get("error") and not ll.get("error"):
        a_, b_ = cc["oos"].get("excess_avg"), ll["oos"].get("excess_avg")
        if a_ is not None and b_ is not None:
            print()
            print(f"  前视偏差的代价 = {a_ - b_:+.2f} 个百分点"
                  f"（用未来信息看起来{'多赚' if a_ - b_ > 0 else '少赚'} "
                  f"{abs(a_ - b_):.2f}%）")

    if args.stability:
        print()
        print("=" * 96)
        print("样本量稳定性检验（up 门控）—— 判断结果是不是小样本巧合")
        print("=" * 96)
        print(f"{'标的数':>7s}{'笔数':>8s}{'样本外笔数':>11s}"
              f"{'样本外平均':>11s}{'样本外超额':>11s}{'门槛':>8s}")
        for n in (30, 60, 100, 150, 200, 300):
            sub = [r[0] for r in con.execute(
                f"""SELECT code FROM read_parquet('{rb.PARQUET.as_posix()}')
                    GROUP BY code HAVING count(*) >= 200
                    ORDER BY code LIMIT {n}""").fetchall()]
            r = run_gated(sub, con, gate, "up", stop_pct=args.stop_pct,
                          exit_mode=args.exit_mode,
                          oos_ratio=args.oos_ratio, cost=cm)
            if r.get("error") or r["oos"].get("n", 0) < 10:
                print(f"{n:>7d}  样本不足")
                continue
            o = r["oos"]
            ok = (o.get("excess_avg") or 0) > 0
            print(f"{n:>7d}{r['total']:>8d}{o['n']:>11d}"
                  f"{o['avg_ret']:>10.2f}%{o['excess_avg']:>10.2f}%"
                  f"{'通过' if ok else '不过':>8s}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
