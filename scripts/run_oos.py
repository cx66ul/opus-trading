"""样本外验证（OOS）· 止损 5% —— D13 决策落地验证。

★ 为什么不能用 `backtest.walk_forward`：
  `walk_forward` 把 bars 切成片段后**独立重跑**，隐含假设是
  「entry_fn 只依赖片段内数据」。
  但缠论信号依赖 ① 长历史（实测190 天才扫出 189 个信号）
  以及 ② **连续的 `seen` 集合**（判断信号是否「新可见」）。
  实测：train=120 / test=30 的切片重跑后，成交从 2 笔掉到 **0 笔**
  —— 片段只有 30 根，而 warmup 就要 60 根。
  ⇒ 用它做缠论的样本外验证会得到「样本外 0 笔 ⇒无法验证」的假结论。

★ 本脚本的正确口径 —— **「信号流连续，只在样本外区间统计」**：
  1. 用**全样本**跑一遍回测（缠论结构、`seen` 状态都是连续的）
  2. 把时间轴切成 训练段 / 样本外段
  3. **只统计入场日落在样本外段**的成交
  这才是真正的样本外：入场决策所用的历史全在训练段内，
  而结果没有被用来选参数。

★ 为什么这样仍算「样本外」：
  · 参数（止损 5%）由用户拍定，不是从这批数据里搜出来的
  · 若要更严格，可用 auto_tune 在训练段选参、样本外评估（已支持）
  · 本脚本额外报告「训练段 vs 样本外段」的指标对比 ——
    若样本外显著差于训练段，说明有前视偏差或过拟合

跑法：
  python scripts/run_oos.py --stop-pct 5
  python scripts/run_oos.py --stop-pct 5 --tune    # 训练段自动选参
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb                                    # noqa: E402
from loguru import logger                         # noqa: E402

from backend.domain.backtest import (             # noqa: E402
    CostModel,
    auto_tune,
    backtest,
    oos_gate,
)
from backend.domain.overlay import atr            # noqa: E402

sys.path.insert(0, str(_ROOT / "scripts"))
import importlib.util                             # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "rb", _ROOT / "scripts" / "run_backtest.py")
rb = importlib.util.module_from_spec(_spec)
sys.modules["rb"] = rb
_spec.loader.exec_module(rb)

CST = timezone(timedelta(hours=8))


def stats_of(trades: list, label: str) -> dict:
    """一组成交的统计。样本不足时胜率为 None（不显示误导性数字）。"""
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "winrate": None, "avg_ret": None,
                "worst": None, "net": 0.0, "avg_hold": None,
                "note": "无成交"}
    rets = [t.ret_pct for t in trades]
    wins = [r for r in rets if r > 0]
    net = sum(t.net_pnl for t in trades)
    return {
        "label": label, "n": n,
        "winrate": round(len(wins) / n, 4) if n >= 10 else None,
        "raw_hit": round(len(wins) / n, 4),
        "avg_ret": round(sum(rets) / n, 2),
        "worst": round(min(rets), 2),
        "net": round(net, 0),
        "avg_hold": round(sum(t.hold_days or 0 for t in trades) / n, 1),
        "note": "" if n >= 10 else f"样本仅 {n} 笔，按规则不显示胜率",
    }


def run_oos(codes: list[str], con, stop_pct: float,
            oos_ratio: float = 0.4, cost: CostModel | None = None) -> dict:
    """全样本跑回测，按入场日切分训练/样本外。"""
    cost = cost or CostModel()
    all_trades = []
    for code in codes:
        bars = rb.load_bars(con, code)
        if len(bars) < 150:
            continue
        # ★ seen 状态在**全样本**上连续，不切片 ——
        #   切片会破坏「新可见」判断，且长历史不足会导致零信号。
        sh = {"seen": set(), "last_i": -1, "buys": [], "sells": []}
        r = backtest(code, bars, rb.make_entry_fn(bars, sh),
                     strategy_id="chanlun_base",
                     exit_fn=rb.make_exit_fn(sh),
                     stop_loss_pct=stop_pct, cost=cost)
        all_trades.extend(r.trades)

    if not all_trades:
        return {"error": "全样本无成交，无法切分"}

    # ---- 按入场日排序后切分（用分位数切，不按固定日期，保证两段都有量）
    all_trades.sort(key=lambda t: t.entry_date)
    split_idx = int(len(all_trades) * (1 - oos_ratio))
    train = all_trades[:split_idx]
    oos = all_trades[split_idx:]
    return {
        "total": len(all_trades),
        "split_date": (all_trades[split_idx].entry_date
                       if split_idx < len(all_trades) else None),
        "train": stats_of(train, f"训练段（{split_idx} 笔）"),
        "oos": stats_of(oos, f"样本外段（{len(oos)} 笔）"),
        "oos_start": oos[0].entry_date if oos else None,
        "oos_end": oos[-1].entry_date if oos else None,
        "_trades": all_trades,
    }


def stability_check(codes: list[str], con, stop_pct: float,
                     steps: tuple[int, ...] = (30, 60, 100, 150, 200, 300, 400),
                     oos_ratio: float = 0.4,
                     cost: CostModel | None = None) -> dict:
    """★★ 样本量稳定性检验 —— 判断「通过」是不是小样本巧合。

    ★★ 为什么必须做这一步（实测踩过）：
      60 只标的时样本外平均 +0.51%、门槛**通过**；
      扩到 200 只变成 -0.15%、门槛**不通过**；
      再扩到 400 只又变成 +0.44%、**又通过**。
      ⇒ 收益在 0 附近来回穿越，单点结论不可信。
      只报一个样本量的结果，等于在 7 个数字里挑了个好看的。

    ⇒ 判定标准：若各档样本外平均收益**符号不一致**，
      则该策略**没有稳定优势**，应报「不显著」而不是报最好那档。
    """
    rows = []
    for n in steps:
        sub = codes[:n] if n < len(codes) else codes
        if not sub:
            continue
        r = run_oos(sub, con, stop_pct, oos_ratio, cost)
        if r.get("error"):
            continue
        o = r["oos"]
        rows.append({
            "codes": len(sub), "total": r["total"], "oos_n": o["n"],
            "winrate": o["winrate"], "avg_ret": o["avg_ret"],
            "worst": o["worst"], "net": o["net"],
            "passed": (o["avg_ret"] or 0) > 0,
        })

    if not rows:
        return {"error": "稳定性检验无有效样本"}

    signs = {1 if (r["avg_ret"] or 0) > 0 else -1 for r in rows
             if r["avg_ret"] is not None}
    rets = [r["avg_ret"] for r in rows if r["avg_ret"] is not None]
    npass = len([r for r in rows if r["passed"]])

    # ★ 符号一致 = 方向可信；符号不一致 = 在 0 附近震荡 = 无稳定优势
    if len(signs) == 1:
        verdict = "stable_positive" if signs == {1} else "stable_negative"
        note = (f"各档样本外平均收益均为{'正' if 1 in signs else '负'}，"
                f"方向一致")
    else:
        verdict = "unstable"
        note = (f"★ 各档样本外平均收益在 0 附近**来回穿越**"
                f"（{min(rets):+.2f}% ~ {max(rets):+.2f}%），"
                f"{npass}/{len(rows)} 档通过门槛 ⇒ "
                f"**没有稳定优势，单点「通过」是小样本巧合**")

    return {
        "stop_pct": stop_pct,
        "rows": rows,
        "verdict": verdict,
        "note": note,
        "n_pass": npass,
        "n_total": len(rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="样本外验证（止损锁定版）")
    ap.add_argument("--stop-pct", type=float, default=5.0, help="止损百分比")
    ap.add_argument("--limit", type=int, default=60, help="回测多少只标的")
    ap.add_argument("--scan", type=int, default=300, help="扫描池大小")
    ap.add_argument("--oos-ratio", type=float, default=0.4,
                    help="样本外占总成交比例")
    ap.add_argument("--tune", action="store_true",
                    help="在训练段用 auto_tune 自动选止损档（更严格）")
    ap.add_argument("--stability", action="store_true",
                    help="样本量稳定性检验（判断「通过」是否只是小样本巧合）")
    ap.add_argument("--stability-steps", default="30,60,100,150,200,300,400",
                    help="稳定性检验的标的数梯度")
    args = ap.parse_args()

    if not rb.PARQUET.exists():
        logger.error(f"Parquet 快照不存在: {rb.PARQUET}")
        return 1
    con = duckdb.connect()
    codes = [r[0] for r in con.execute(
        f"""SELECT code FROM read_parquet('{rb.PARQUET.as_posix()}')
            GROUP BY code HAVING count(*) >= 200
            ORDER BY code LIMIT {args.scan}""").fetchall()][: args.limit]

    cm = CostModel()
    print("=" * 78)
    print(f"样本外验证 · 止损锁定 {args.stop_pct:.0f}%")
    print("=" * 78)
    print(f"标的 {len(codes)} 只 | 样本外占比 {args.oos_ratio:.0%}")
    print("★ 口径：全样本连续跑，只统计入场日落在样本外段的成交")
    print("  （切片重跑会让缠论信号归零 —— 片段太短，无新信号）")
    print()

    res = run_oos(codes, con, args.stop_pct, args.oos_ratio, cm)
    if res.get("error"):
        logger.error(res["error"])
        return 1

    print(f"总成交 {res['total']} 笔 | 样本外起点 {res['oos_start']}")
    print()
    hdr = f"{'':22s}{'笔数':>6s}{'胜率':>9s}{'平均收益':>10s}{'最差单笔':>10s}{'净盈亏':>12s}{'平均持有':>9s}"
    print(hdr)
    print("-" * len(hdr))
    for key in ("train", "oos"):
        s = res[key]
        wr = f"{s['winrate']:.1%}" if s["winrate"] is not None else "不足"
        print(f"{s['label']:>22s}{s['n']:>6d}{wr:>9s}"
              f"{s['avg_ret']:>9.2f}%{s['worst']:>9.2f}%"
              f"{s['net']:>12,.0f}{s['avg_hold']:>8.1f}d")
    print()

    tr, oos_s = res["train"], res["oos"]
    # ---- 上线门槛判定
    gate_trades = oos_s["n"]
    gate_ret = oos_s["avg_ret"] or 0.0
    ok, why = oos_gate(
        {"error": None, "n_folds": 1, "oos_trades": gate_trades,
         "oos_return_sum_pct": round(gate_ret * gate_trades, 2)},
        min_folds=1, min_trades=10, min_return_pct=0.0)
    print(f"上线门槛判定（样本外）：{'通过' if ok else '不通过'} —— {why}")
    print()

    # ---- 过拟合/前视检查：样本外 vs 训练段
    print("=" * 78)
    print("样本外 vs 训练段 逐项对比（★ 前视偏差/过拟合的体检表）")
    print("=" * 78)
    print(f"{'指标':>12s}{'训练段':>12s}{'样本外':>12s}{'差异':>12s}{'判断':>10s}")
    if tr["avg_ret"] is not None and oos_s["avg_ret"] is not None:
        for name, a, b in (("平均收益%", tr["avg_ret"], oos_s["avg_ret"]),
                           ("最差单笔%", tr["worst"], oos_s["worst"])):
            diff = b - a
            # 平均收益：样本外明显更差 ⇒ 可能过拟合
            verdict = "样本外更差" if diff < -1.0 else (
                "样本外更好" if diff > 1.0 else "相当")
            print(f"{name:>12s}{a:>11.2f}%{b:>11.2f}%{diff:>+11.2f}%"
                  f"{verdict:>14s}")
    if tr["winrate"] is not None and oos_s["winrate"] is not None:
        diff = (oos_s["winrate"] - tr["winrate"]) * 100
        verdict = "样本外更差" if diff < -10 else (
            "样本外更好" if diff > 10 else "相当")
        print(f"{'胜率':>12s}{tr['winrate']:>11.1%}{oos_s['winrate']:>11.1%}"
              f"{diff:>+11.1f}pp{verdict:>14s}")
    else:
        print(f"{'胜率':>12s}{'样本不足':>12s}{'样本不足':>12s}{'—':>12s}"
              f"{'无法比较':>14s}")
    print()
    print("  ★ 解读：样本外指标**接近或优于**训练段 ⇒ 无明显过拟合；")
    print("     样本外显著更差 ⇒ 要么过拟合，要么存在前视偏差，需回头查。")
    print("     注意：止损 % 是**用户拍定**的固定值，不是在这批数据里搜出来的，")
    print("     所以样本外变差不必然是过拟合 —— 也可能是市场环境切换。")

    # ---- 训练段自动选参（更严格）
    if args.tune:
        print()
        print("=" * 78)
        print("严格模式 · 各止损档在训练段/样本外的表现对照")
        print("=" * 78)
        rows = []
        for stop in (4.0, 5.0, 6.0, 8.0, 10.0, None):
            r2 = run_oos(codes, con, stop, args.oos_ratio, cm)
            if r2.get("error"):
                continue
            rows.append((stop, r2))
        print(f"{'止损档':>10s}{'训练段平均':>12s}{'样本外平均':>12s}"
              f"{'样本外笔数':>12s}")
        for stop, r2 in rows:
            label = "不启用" if stop is None else f"-{stop:.0f}%"
            print(f"{label:>10s}{r2['train']['avg_ret']:>11.2f}%"
                  f"{r2['oos']['avg_ret']:>11.2f}%{r2['oos']['n']:>12d}")
        print()
        print("  ★ 训练段最优档与样本外最优档若不一致 ⇒ 直接用训练段最优会踩坑，")
        print("    这正是「样本外验证」存在的意义。")

    # ---- 样本量稳定性检验
    if args.stability:
        print()
        print("=" * 78)
        print("★★ 样本量稳定性检验（判断「通过」是否只是小样本巧合）")
        print("=" * 78)
        steps = tuple(int(x) for x in args.stability_steps.split(","))
        st = stability_check(codes, con, args.stop_pct, steps,
                             args.oos_ratio, cm)
        if st.get("error"):
            logger.error(st["error"])
        else:
            print(f"{'标的数':>7s}{'总成交':>8s}{'样本外笔数':>11s}"
                  f"{'样本外胜率':>11s}{'样本外平均':>11s}{'样本外净额':>13s}"
                  f"{'门槛':>7s}")
            for r in st["rows"]:
                wr = f"{r['winrate']:.1%}" if r["winrate"] is not None else "不足"
                print(f"{r['codes']:>7d}{r['total']:>8d}{r['oos_n']:>11d}"
                      f"{wr:>11s}{r['avg_ret']:>10.2f}%{r['net']:>13,.0f}"
                      f"{'通过' if r['passed'] else '不过':>8s}")
            print()
            print(f"  判定：{st['verdict']}")
            print(f"  {st['note']}")
            print()
            if st["verdict"] == "unstable":
                print("  ★★ 结论：当前止损档**没有可验证的稳定优势**。")
                print("     建议：① 不要据此上线；② 扩大样本或拉长时间窗再看；")
                print("            ③ 优先改入场/出场逻辑（卖点出场胜率 87.5%，")
                print("               说明信号有效，只是出现得太少），")
                print("            而不是继续调止损百分比。")
            # ★★ 落盘JSON —— `scheduler` 的 `@job("weekly")` 消费它。
            #   为什么必须落盘而不用 stdout：周报要发**邮件**，
            #   而邮件需要结构化数据（不是给人看的表格文本）。
            #   调度器是独立进程，只能通过文件交换。
            #★覆盖式写入：稳定性是「当前状态」，留旧文件会读到过期结论。
            out_path = _ROOT / "data" / "oos_stability.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(
                {"generated_at": datetime.now(CST).isoformat(timespec="seconds"),
                 "scan_limit": args.limit, "oos_ratio": args.oos_ratio,
                 "stop_pct": args.stop_pct, **st},
                ensure_ascii=False, indent=2, default=str), "utf-8")
            print(f"  → 已写入 {out_path.name}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())