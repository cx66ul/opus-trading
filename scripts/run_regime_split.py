"""分市场状态回测（M9 · 回答「策略是不是只在特定行情下有效」）

★ 为什么必须做这个（D15 的结论直接催生本脚本）：
  全样本回测显示买点「超额收益仅 +0.18%」，但那**掩盖了行情差异**。
  实测全市场等权指数的 60 日滚动涨跌（2025-06~2026-10）：
    2025-07~ 2025-11  +122%（强上涨）
    2025-12~ 2026-06+1~ +22%（震荡偏强）
    2026-07 ~ 2026-09  -14%（下跌）
  ⇒ 合并统计会把「上涨市赚得多」和「下跌市亏得多」互相抵消，
    得出「没有 alpha」——但真实情况可能是**只在上涨市有效**。

★ 与已有脚本的分工：
  run_oos.py       样本内/外切分（时间维度）
  run_backtest.py  参数对比（止损/离场维度）
  **本脚本**        市场状态分组（行情维度）—— 三者互补

★ 口径说明（重要）：
  分段**不改变信号计算**。缠论信号仍用截至当日的长历史算，
  只是**统计**时把成交按入场日归到对应行情段。
  若按段切片重跑，`seen` 会断裂、信号归零（同 run_oos 的坑）。

跑法：
  python scripts/run_regime_split.py
  python scripts/run_regime_split.py --limit 200
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

import duckdb                                    # noqa: E402
from loguru import logger                         # noqa: E402

import importlib.util                             # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "rb", _ROOT / "scripts" / "run_backtest.py")
rb = importlib.util.module_from_spec(_spec)
sys.modules["rb"] = rb
_spec.loader.exec_module(rb)

from backend.domain.backtest import (             # noqa: E402
    CostModel,    backtest,
)
from backend.domain.data_gate import (           # noqa: E402
    coverage_from_parquet, equal_weight_index,
)

CST = timezone(timedelta(hours=8))


# ================================================================== 行情分段
def segment_by_roll(index_rows, win: int = 60,
                    up_th: float = 8.0, down_th: float = -8.0,
                    min_len: int = 10) -> list[dict]:
    """★ 由**已构造好的指数序列**划行情段（纯函数，不碰数据库）。

    取数与分段刻意分开：`market_segments` 负责从 Parquet 取数并过完备度闸门，
    本函数只管「序列 → 分段」。
    这样分段逻辑可以脱离数据库单测（原先它内联SQL，
    测试桩得伪造 Parquet 结果，还得跟着 SQL 列序变）。

    index_rows : [(date_like, close), ...] 升序

    ★ 用**滚动窗口涨跌**而不是逐日涨跌：
      逐日涨跌噪声太大（±1.5%），无法判定「趋势市」。
      60 日滚动能刻画中期趋势方向。

    ★★★ 合并过短的段（min_len）：
      60 日滚动在阈值附近会反复翻转，实测产生过「1 天段」
      （2026-08-26、2026-08-14），把连续行情切碎后归类毫无意义。

    ★★ 判据用双阈值（±8%）而非 0：
      避免把几乎没动的震荡市（±1%）也算成上涨/下跌，
      那会让「上涨市表现好」变成同义反复。

    ★★★ 分类必须用**段内平均的 60 日涨跌**，不能用段末那一天的快照
      （实测踩过，结论完全失真）：
        按段末快照分段时，`down` 段 2026-07-17~08-13 的**段内实际是 +7.17%**
        （下跌趋势的后期反弹），却因段末那天 60 日为 -11.91% 被标成「下跌市」
        ⇒ 后续统计把反弹期归进下跌市，得出「下跌市平均 +1.04%」这种矛盾结论。
      段内平均才代表「这段整体处于什么状态」。
    """
    dates = [str(r[0]) for r in index_rows]
    vals = [float(r[1]) for r in index_rows]
    if len(vals) < win + 1:
        return []

    # 每交易日的滚动涨跌
    roll: dict[str, float] = {}
    for i in range(win, len(vals)):
        roll[dates[i]] = (vals[i] / vals[i - win] - 1) * 100

    # ① 先按单日状态切成临时段
    raw: list[dict] = []
    for d in dates[win:]:
        r = roll[d]
        st = ("up" if r > up_th else "down" if r < down_th else "flat")
        if raw and raw[-1]["_s"] == st:
            raw[-1]["end"] = d
            raw[-1]["rolls"].append(r)
        else:
            raw.append({"_s": st, "start": d, "end": d, "rolls": [r]})

    # ② ★ 用**段内平均的 60 日涨跌**重新定性
    out: list[dict] = []
    for s in raw:
        avg_roll = sum(s["rolls"]) / len(s["rolls"])
        st = ("up" if avg_roll > up_th
              else "down" if avg_roll < down_th else "flat")
        n = len(s["rolls"])
        if out and out[-1]["regime"] == st:
            # 合并相邻同类段（重定性后可能出现相邻同类）
            out[-1]["end"] = s["end"]
            out[-1]["n"] += n
            out[-1]["_rolls"].extend(s["rolls"])
        else:
            out.append({"regime": st, "start": s["start"], "end": s["end"],
                        "n": n, "_rolls": list(s["rolls"])})

    # ③★ 合并过短的段（min_len）—— 60 日滚动在阈值附近会反复翻转，
    #   产生大量「1 天段」，把本该连续的行情切碎。
    #   实测修正前有 2026-08-26（1天）、2026-08-14（1天）这种段，
    #   归类后统计毫无意义。
    if min_len > 1:
        merged: list[dict] = []
        for s in out:
            if merged and s["n"] < min_len:
                # 短段并入前一段（行情切换期归给趋势更明确的那侧）
                prev = merged[-1]
                prev["end"] = s["end"]
                prev["n"] += s["n"]
                prev["_rolls"].extend(s["_rolls"])
            else:
                merged.append(dict(s))
        # 首段若过短并入次段
        while len(merged) > 1 and merged[0]["n"] < min_len:
            first, nxt = merged[0], merged[1]
            nxt["start"] = first["start"]
            nxt["n"] += first["n"]
            nxt["_rolls"] = first["_rolls"] + nxt["_rolls"]
            merged.pop(0)
        out = merged

    for s in out:
        s["avg_roll"] = round(sum(s["_rolls"]) / max(len(s["_rolls"]), 1), 2)
        s.pop("_rolls", None)
    return out


def market_segments(con, win: int = 60,
                    up_th: float = 8.0, down_th: float = -8.0,
                    min_len: int = 10) -> list[dict]:
    """从 Parquet 取等权指数（过完备度闸门）后划行情段。

    ★★ 横截面完备度闸门（2026-10-09 新增，见 backend/domain/data_gate.py）
      M1 渐进同步导致 2025-06-19~2025-09-04 每天只有 1~93 只股票。
      不过滤时 `avg(close)` 会从 14.81（1 只）"涨"到 24.64（5000 只），
      伪造出一段 +66% 的"上涨行情" —— 首个「上涨」段 +69.78% 就是这么来的。
    """
    return segment_by_roll(equal_weight_index(con, rb.PARQUET), win, up_th,
                           down_th, min_len)


def print_segments(segs: list[dict], con) -> None:
    rows = equal_weight_index(con, rb.PARQUET)
    m = {str(r[0]): float(r[1]) for r in rows}
    label = {"up": "上涨", "down": "下跌", "flat": "震荡"}
    print("=" * 88)
    print("行情分段（等权指数 60 日滚动涨跌的**段内平均**，阈值 ±8%）")
    print("=" * 88)
    cov = coverage_from_parquet(con, rb.PARQUET)
    print(f"★ 横截面完备度闸门：剔除 {cov.n_unusable} 天（标的数 < "
          f"{cov.threshold}），有效起点 {cov.first_usable}")
    print(f"{'类型':>6s}{'起':>12s}{'止':>12s}{'交易日':>7s}"
          f"{'段内涨跌':>10s}{'段内60日均值':>13s}")
    bad = 0
    for s in segs:
        a, b = m.get(s["start"]), m.get(s["end"])
        r = (b / a - 1) * 100 if a and b else 0.0
        ar = s.get("avg_roll")
        ars = f"{ar:+.2f}%" if ar is not None else "—"
        print(f"{label[s['regime']]:>6s}{s['start']:>12s}{s['end']:>12s}"
              f"{s['n']:>7d}{r:>9.2f}%{ars:>13s}")
        # ★ 自检：「下跌段」的段内平均必须真的为负，否则分段又失真了
        if s["regime"] == "down" and ar is not None and ar >= 0:
            bad += 1
    if bad:
        print()
        print(f"⚠️有 {bad} 个「下跌段」的段内 60 日均值 ≥ 0 ⇒ 分段口径仍有问题，")
        print("  下方按行情归类的统计不可信。")
    print()


# ================================================================== 分段回测
def run_split(codes: list[str], con, segs: list[dict],
              stop_pct: float = 5.0, exit_mode: str = "chan",
              oos_ratio: float = 0.4,
              cost: CostModel | None = None) -> dict:
    """全样本连续跑，按入场日归到各行情段。"""
    cost = cost or CostModel()
    # 日期 → 行情段
    def regime_of(d: str) -> str:
        for s in segs:
            if s["start"] <= d <= s["end"]:
                return s["regime"]
        return "unknown"

    all_trades = []
    for code in codes:
        bars = rb.load_bars(con, code)
        if len(bars) < 150:
            continue
        sh = {"seen": set(), "last_i": -1, "buys": [], "sells": [],
              "zs": None, "sell_levels": ("strong",)}
        r = backtest(code, bars, rb.make_entry_fn(bars, sh),
                     strategy_id="chanlun_base",
                     exit_fn=rb.make_exit_fn(sh, mode=exit_mode),
                     stop_loss_pct=stop_pct, cost=cost)
        all_trades.extend(r.trades)

    if not all_trades:
        return {"error": "全样本无成交"}

    # ---- 大盘基准（按序配对）
    bm = rb.build_benchmark(con, all_trades)
    rets = [t.ret_pct for t in all_trades]
    bms = bm.rets if bm else []

    by_reg: dict[str, list] = defaultdict(list)
    for i, t in enumerate(all_trades):
        b = bms[i] if i < len(bms) else None
        by_reg[regime_of(t.entry_date)].append((t.ret_pct, b))

    def stat(items):
        if not items:
            return None
        rs = [x[0] for x in items]
        bs = [x[1] for x in items if x[1] is not None]
        ex = [r - b for r, b in items if b is not None]
        n = len(rs)
        wr = (len([x for x in rs if x > 0]) / n) if n >= 10 else None
        exw = (len([x for x in ex if x > 0]) / len(ex)) if len(ex) >= 10 else None
        return {
            "n": n,
            "winrate": wr,
            "avg_ret": round(sum(rs) / n, 2),
            "excess_avg": round(sum(ex) / len(ex), 2) if ex else None,
            "excess_winrate": exw,
            "bench_avg": round(sum(bs) / len(bs), 2) if bs else None,
        }

    out = {k: stat(v) for k, v in by_reg.items()}
    out["all"] = stat([(r, bms[i] if i < len(bms) else None)
                       for i, r in enumerate(rets)])
    return {"by_regime": out, "total": len(all_trades),
            "segs": segs}


def main() -> int:
    ap = argparse.ArgumentParser(description="分市场状态回测")
    ap.add_argument("--limit", type=int, default=120, help="回测多少只标的")
    ap.add_argument("--scan", type=int, default=500, help="扫描池大小")
    ap.add_argument("--stop-pct", type=float, default=5.0)
    ap.add_argument("--exit-mode", default="chan",
                    choices=["chan", "chan_all", "zs", "zs_ma", "none"])
    ap.add_argument("--win", type=int, default=60, help="滚动窗口交易日数")
    args = ap.parse_args()

    if not rb.PARQUET.exists():
        logger.error(f"Parquet 快照不存在: {rb.PARQUET}")
        return 1
    con = duckdb.connect()
    segs = market_segments(con, args.win)
    if not segs:
        logger.error("行情分段失败（数据不足）")
        return 1
    print_segments(segs, con)

    codes = [r[0] for r in con.execute(
        f"""SELECT code FROM read_parquet('{rb.PARQUET.as_posix()}')
            GROUP BY code HAVING count(*) >= 200
            ORDER BY code LIMIT {args.scan}""").fetchall()][: args.limit]

    print()
    print("=" * 82)
    print(f"分行情回测（{len(codes)} 只，止损 {args.stop_pct:.0f}%，"
          f"离场 {args.exit_mode}）")
    print("=" * 82)

    res = run_split(codes, con, segs, args.stop_pct, args.exit_mode)
    if res.get("error"):
        logger.error(res["error"])
        return 1

    label = {"up": "上涨市", "down": "下跌市", "flat": "震荡市",
             "unknown": "未知", "all": "全样本"}
    order = ["up", "flat", "down", "unknown", "all"]
    print(f"{'行情':>8s}{'笔数':>6s}{'胜率':>9s}{'平均收益':>10s}"
          f"{'大盘基准':>10s}{'超额收益':>10s}{'超额胜率':>10s}")
    print("-" * 82)
    for k in order:
        s = res["by_regime"].get(k)
        if not s:
            continue
        wr = f"{s['winrate']:.1%}" if s["winrate"] is not None else "不足"
        ex = f"{s['excess_avg']:+.2f}%" if s["excess_avg"] is not None else "—"
        exw = (f"{s['excess_winrate']:.1%}"
               if s["excess_winrate"] is not None else "不足")
        bn = f"{s['bench_avg']:+.2f}%" if s["bench_avg"] is not None else "—"
        print(f"{label.get(k, k):>8s}{s['n']:>6d}{wr:>9s}"
              f"{s['avg_ret']:>9.2f}%{bn:>10s}{ex:>10s}{exw:>10s}")
    print()

    # ---- 结论
    up = res["by_regime"].get("up")
    down = res["by_regime"].get("down")
    print("=" * 82)
    print("结论")
    print("=" * 82)
    if up and down and up.get("excess_avg") is not None \
            and down.get("excess_avg") is not None:
        print(f"  上涨市超额 {up['excess_avg']:+.2f}%"
              f"  vs  下跌市超额 {down['excess_avg']:+.2f}%")
        if up["excess_avg"] > down["excess_avg"] + 0.5:
            print("  ⇒★ 策略**只在上涨市有正超额**，下跌市跑输大盘。")
            print("     这是「择时不足」而非「选股无效」——")
            print("     正确用法是**在大盘状态允许时才启用**（M5 六态状态机已有）。")
        elif up["excess_avg"] > 0 and down["excess_avg"] > 0:
            print("  ⇒ 两市都有正超额，但全样本被摊薄 ⇒ **有 alpha，只是幅度小**。")
        else:
            print("  ⇒ 上涨市也没有正超额 ⇒ 不是时段问题，**买点本身无效**。")
    else:
        print("  样本不足，无法给出结论")
    print()
    print("  ★ 注意：这里的超额是**按入场日归段**的，段内还有波动，")
    print("     样本量小的时候超额也会震荡（D14已验证过：30~400 只结果会翻转）。")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())