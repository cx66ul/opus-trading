"""真实数据回测（M9 验证脚本）。

★ 为什么单独写脚本而不是直接跑引擎：
  回测引擎是纯函数（输入 bars，输出结果），但真实回测还需要
  **把缠论信号翻译成 Entry 信号**这一层胶水。这层胶水最容易出错：
    · 用当日收盘当入场价 → 前视偏差（M8 已踩过）
    · 忘了跳过未走完的信号 → 用「未来才知道会触发」的历史信号
    · 中枢/笔结构随取数窗口漂移（M8 实测250 vs 251根差一笔）
  所以它值得被脚本化并留下可复核的输出。

数据源：**只读 Parquet 快照**（data/snapshots/2026-10-08/kline_daily.parquet），
★ 不用 DuckDB —— 避免与后台同步进程抢文件锁。

跑法： python scripts/run_backtest.py --codes 300 --limit 50
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

from backend.domain.backtest import (             # noqa: E402
    Bar,
    CostModel,
    auto_tune,
    backtest,
    oos_gate,
    walk_forward,
)
from backend.domain.backtest import Benchmark     # noqa: E402
from backend.domain.chan import analyze          # noqa: E402

CST = timezone(timedelta(hours=8))
PARQUET = _ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"

# ★ 与生产信号脚本一致（M8 实测：取数根数不同会改变笔结构）
PROD_KLINE_LIMIT = 250


def build_benchmark(con, trades: list) -> Benchmark | None:
    """构造与成交**按序配对**的大盘基准。

    ★★ 为什么必须有（实测踩过，本项目最贵的一次误判）：
      买点后 10 天，个股平均 -2.11%、同期大盘 -2.30%
      只看绝对收益 ⇒ 「策略亏了 2%、买点无效」
      算超额 ⇒ **+0.18%** ⇒ 信号只是**跟跌**，没有 alpha
      这两个结论天差地别，而只看绝对收益就会得出错误的那一个。

    ★ 基准口径：全市场等权平均的**区间涨跌幅**（pct_chg 按日累加）。
      不用指数是因为本地没有可靠的指数序列，
      而「全市场等权」正是我们要回答的问题本身
      ——「我选的票比全体平均好多少」。

    ★ 按序配对而非按日期匹配：每笔成交的基准区间
      必须与该笔的「入场日→出场日」严格一致。
    """
    if not trades:
        return None
    # 建日期 → 当日全市场平均涨跌
    rows = con.execute(
        f"""SELECT trade_date, avg(pct_chg) FROM
            read_parquet('{PARQUET.as_posix()}')
            WHERE pct_chg IS NOT NULL GROUP BY trade_date"""
    ).fetchall()
    mkt = {str(r[0]): float(r[1]) for r in rows if r[1] is not None}
    if not mkt:
        return None
    dates = sorted(mkt)

    rets: list[float] = []
    for t in trades:
        try:
            i0 = dates.index(t.entry_date)
            i1 = dates.index(t.exit_date) if t.exit_date else i0
        except ValueError:
            # 该笔的日期不在基准序列里（数据边界），跳过而不是猜
            continue
        if i1 < i0:
            continue
        seg = dates[i0 + 1: i1 + 1]      # ★ 不含入场日：入场当日已含在买入价里
        rets.append(sum(mkt[d] for d in seg))
    return Benchmark(rets=rets) if rets else None


def load_bars(con, code: str, limit: int = PROD_KLINE_LIMIT) -> list[Bar]:
    rows = con.execute(
        f"""SELECT trade_date, open, high, low, close, volume
            FROM read_parquet('{PARQUET.as_posix()}')
            WHERE code = ? ORDER BY trade_date""", [code]).fetchall()
    out: list[Bar] = []
    for r in rows[-limit:]:
        try:
            o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        except (TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0 or h < l:
            continue            # 脏数据/停牌
        out.append(Bar(d=str(r[0]), o=o, h=h, l=l, c=c,
                       v=float(r[5] or 0)))
    return out


def _scan_new(bars: list[Bar], i: int, warmup: int,
               seen: set, cache: dict | None = None,
               sell_levels: tuple | None = None
               ) -> tuple[list, list, dict | None]:
    """扫描第 i 根 bar，返回「本日新可见」的 (买点, 卖点, 最后中枢)，并更新 seen。

    ★★★ 必须**由同一个函数同时产出买卖点并更新 seen**：
      早期版本让 entry_fn 和 exit_fn 各自扫一遍，
      结果两者对「什么是新信号」的判断不一致 ——
      出现「09-22 有新买点、09-23 入场当天就被新卖点平掉」，
      一天进出、胜率被成本吃掉。
      单一扫描点 = 单一事实来源：engine 在同一根 bar 上
      无论先问entry 还是先问 exit，得到的都是同一组信号。

    ★★ `sell_levels`：卖点纳入的档位。
      实测 strong 档「本日新可见」仅 122 次 / 4750 根 bar（2.2%），
      纳入 normal 档可提升覆盖（1sell/2sell 都是 normal）。
      ★ 优先从 `cache["sell_levels"]`（即 shared）读，
        传参只作为覆盖手段 —— 靠调用方手动传极易漏传后静默退回默认值
        （实测踩过：漏传时 2sell/normal 被静默丢弃，看着像「没信号」）。

    ★★ 同时返回**最后中枢** `{low, high}`，供出场判「跌破中枢下沿」——
      中枢破位是结构化离场条件，与缠论卖点互补。
      中枢信息不随 seen 变化（它是结构状态，不是事件），所以每次都返回。

    ★ `cache` 记忆本根的扫描结果，让 exit 能复用 entry 的扫描。
    """
    if cache is not None and cache.get("last_i") == i:
        return (cache.get("buys", []), cache.get("sells", []),
                cache.get("zs"))
    if i < warmup or i + 1 >= len(bars):
        return [], [], None
    # ★ sell_levels 以 shared 里的为准（单一事实来源）
    levels = (cache.get("sell_levels") if cache else None) or \
        sell_levels or ("strong",)
    upto = bars[max(0, i - PROD_KLINE_LIMIT + 1): i + 1]
    recs = [{"d": b.d, "open": b.o, "high": b.h, "low": b.l,
             "close": b.c, "volume": b.v} for b in upto]
    res = analyze("X", recs, as_of=bars[i].d)

    buys, sells = [], []
    for s in res.signals:
        key = (s.kind, s.level, s.date)
        if key in seen:
            continue                    # 之前已可见，不算新信号
        if s.kind in ("1buy", "2buy", "3buy") and s.level == "strong":
            buys.append(s)
        elif (s.kind in ("1sell", "2sell", "3sell")
              and s.level in levels):
            sells.append(s)
    seen.update((s.kind, s.level, s.date) for s in res.signals)

    zs = None
    if res.zhongshus:
        last = res.zhongshus[-1]
        zs = {"low": last.low, "high": last.high}
    if cache is not None:
        cache.update({"last_i": i, "buys": buys, "sells": sells, "zs": zs})
    return buys, sells, zs


def make_entry_fn(bars: list[Bar], shared: dict, warmup: int = 60):
    """入场：本日新可见 strong 买点 → T+1 开盘买入。

    ★ 三个关键口径（每个都真实踩过）：
      1. **入场用 T+1 开盘价**，不是 T 日收盘（否则是前视偏差）
      2. **只认本日新可见的信号** ——
         同一个信号会连续几十天被重复报出（实测 000001 的 3buy
         标记 2025-12-31，却在之后每根 bar 都「出现」）。
         不判新鲜度就会：① 在同一信号上反复建仓、每次都付成本
         （实测成本拖累飙到 156%）；② 因同时存在历史 strong 卖点，
         入场次日即被平仓（实测平均持有 0.7 天、胜率 3.3%）。
         ★★ 也不能用 `s.date == 当日` 判新鲜 —— 缠论信号标记的是
         **笔的结束日**，笔要等下一根才确认，天然滞后：
         实测 190 天里 `s.date == 当日` 命中 0 次。
      3. **买点与卖点互斥**：本日两者都新可见时（结构转折）一律不交易。
    """
    def entry_fn(bar: Bar, i: int, allb: list[Bar]):
        buys, sells, _zs = _scan_new(allb, i, warmup,
                                      shared["seen"], shared)
        if not buys or sells:
            return None
        nxt = allb[i + 1]
        # ★ T+1 必须**能成交**。现实中一字板挂单成交不了，
        #   回测若照样成交会系统性高估「一字板抄底」类策略。
        #   判据：开盘 == 最高 == 最低（封死的一字板）。
        #
        #   ★ 注意「开盘涨停但收盘跌停」**不是**买不到的情况，
        #   不该被过滤：实测 000007 在 2026-07-07 开盘 13.50（涨停价，
        #   前收 12.88），盘中一路跌到 -10% 收 11.59。
        #   开盘价 13.50 是真实可成交的 ⇒ 回测按开盘买入 -31% 是**如实反映**，
        #   这是市场风险不是 bug。所以只挡「封死」的一字板。
        if abs(nxt.h - nxt.l) < 1e-9 and nxt.o == nxt.h:
            return None                # 一字板，买不到
        # 开盘即跌停（开盘=最低 且 跌幅已达幅度）同样无法成交
        prev_close = bar.c
        if prev_close > 0 and abs(nxt.o - nxt.h) < 1e-9 \
                and (prev_close - nxt.o) / prev_close >= 0.095:
            return None
        s = buys[-1]
        return (nxt.d, nxt.o, [f"{s.kind}"], [s.reason[:60]],
                0.9, [0.08, 0.15], 10)
    return entry_fn


def make_exit_fn(shared: dict, warmup: int = 60, mode: str = "chan",
                 zs_buffer: float = 0.0, ma: int = 0):
    """出场判定。

    ★★ mode 决定「离场触发源」，用于提高卖点覆盖率（D15）：

      "chan"    仅 strong 缠论卖点 —— 基线，覆盖率最低
      "chan_all" 缠论卖点（含 normal 档1sell/2sell/3sell）
      "zs"      跌破最近中枢下沿 —— 覆盖率最高（实测 46.4% 的天数在下方）
      "zs_ma"   中枢下沿 或 跌破 N 日均线
      "none"    只靠 max_hold / 止损（对照组）

    ★★ 为什么加「跌破中枢下沿」而不是放宽缠论卖点：
      实测 25 只 × 4750 根 bar：
        缠论卖点累计出现 5553 次，但**本日新可见仅 122 次（2.2%）**
        而「收盘跌破最近中枢下沿」占 **46.4%** 的天数
      ⇒ 缠论卖点天然稀疏（结构确认需要时间），靠放宽 level 提升有限；
        中枢破位是**每天都可能发生**的状态，天然高覆盖。

    ★ zs_buffer：中枢下沿 × (1 - buffer)，留一点缓冲避免假破位。
       0 = 严格跌破下沿；0.01 = 允许下探 1% 才算破。
    """
    def exit_fn(bar: Bar, i: int, allb: list[Bar]):
        _buys, sells, zs = _scan_new(allb, i, warmup,
                                      shared["seen"], shared)
        # ---- 1) 缠论卖点
        if mode in ("chan", "chan_all", "zs", "zs_ma") and sells:
            return "chan_sell"
        if mode == "chan":
            return None
        # ---- 2) 结构破位
        if mode in ("zs", "zs_ma") and zs is not None:
            low = zs["low"] * (1 - zs_buffer)
            if bar.c < low:
                return "zs_break"
        # ---- 3) 均线破位
        if mode == "zs_ma" and ma > 0 and len(allb) > i - ma + 1 >= 0:
            line = sum(b.c for b in allb[i - ma + 1: i + 1]) / ma
            if bar.c < line:
                return "ma_break"
        return None
    return exit_fn


def main() -> int:
    ap = argparse.ArgumentParser(description="真实数据回测（A+C 方案口径）")
    ap.add_argument("--limit", type=int, default=30, help="回测多少只标的")
    ap.add_argument("--scan", type=int, default=400, help="扫描池大小")
    ap.add_argument("--stop-pct", type=float, default=8.0,
                    help="A方案风控止损百分比（0=不启用）")
    ap.add_argument("--compare-exits", action="store_true",
                    help="对比不同止损位下的效果（A 方案对照实验）")
    ap.add_argument("--exit-mode", default="chan",
                    choices=["chan", "chan_all", "zs", "zs_ma", "none"],
                    help="离场触发源（D15 提高卖点覆盖率）")
    ap.add_argument("--sell-levels", default="strong",
                    help="纳入的卖点档位，逗号分隔（strong/normal/weak）")
    ap.add_argument("--compare-exit-modes", action="store_true",
                    help="对比不同离场触发源的覆盖率与效果（D15）")
    args = ap.parse_args()

    if not PARQUET.exists():
        logger.error(f"Parquet 快照不存在: {PARQUET}")
        return 1
    con = duckdb.connect()
    codes = [r[0] for r in con.execute(
        f"""SELECT code FROM read_parquet('{PARQUET.as_posix()}')
            GROUP BY code HAVING count(*) >= 200
            ORDER BY code LIMIT {args.scan}""").fetchall()][: args.limit]
    print("=" * 78)
    print(f"真实数据回测 · 缠论基线策略（{len(codes)} 只）")
    print("=" * 78)
    print("数据源: Parquet 快照（只读，不与同步任务抢锁）")
    print(f"取数窗口: {PROD_KLINE_LIMIT} 根（与生产信号脚本一致）")
    print("★ 入场 = T+1 开盘价；只用 strong 级别买卖点")
    print()
    print("★ A+C 方案（D13）口径说明：")
    print("  C = 系统不自动卖出。回测里的「卖点离场」是**假设你看到提醒后**")
    print("      决定离场的效果对比，不是系统行为。")
    print(f"  A = 止损参考位{args.stop_pct:.0f}%，同样由你决定是否执行。")
    print()

    cm = CostModel()
    all_trades = []
    per_code = []
    t0 = datetime.now(CST)
    for n, code in enumerate(codes, 1):
        bars = load_bars(con, code)
        if len(bars) < 120:
            continue
        # ★ 入场与出场必须共用同一个 seen + 同一份本根扫描缓存 ——
        #   否则两侧对「什么是新信号」判断不一致，会出现同日买卖。
        shared = {"seen": set(), "last_i": -1, "buys": [],
                  "sells": [], "zs": None,
                  "sell_levels": tuple(args.sell_levels.split(","))}
        r = backtest(code, bars, make_entry_fn(bars, shared),
                     strategy_id="chanlun_base",
                     exit_fn=make_exit_fn(
                         shared, mode=args.exit_mode),
                     stop_loss_pct=args.stop_pct or None, cost=cm)
        if r.trades:
            all_trades.extend(r.trades)
            per_code.append((code, r))
        if n % 10 == 0:
            logger.info(f"[{n}/{len(codes)}] 累计成交 {len(all_trades)} 笔")

    # ---- 汇总
    tot_ret = sum(t.net_pnl for t in all_trades)
    wins = [t for t in all_trades if t.net_pnl > 0]
    tot_cost = sum(t.cost for t in all_trades)
    gross = sum(t.gross_pnl for t in all_trades)
    print()
    print("=" * 78)
    print("汇总结果")
    print("=" * 78)
    print(f"  产生成交的标的: {len(per_code)} / {len(codes)}")
    print(f"  总成交笔数    : {len(all_trades)}")
    if all_trades:
        wr = len(wins) / len(all_trades)
        print(f"  胜率          : {wr:.1%}"
              f"{'' if len(all_trades) >= 10 else '（样本<10，按规则不显示）'}")
        print(f"  净盈亏合计    : {tot_ret:+,.0f} 元")
        print(f"  毛盈亏合计    : {gross:+,.0f} 元")
        print(f"  成本合计      : {tot_cost:,.0f} 元")
        print(f"  成本拖累      : {tot_cost / abs(gross) * 100:.1f}%"
              if abs(gross) > 1e-9 else "  成本拖累      : —")
        print(f"  平均持有天数  : {sum(t.hold_days or 0 for t in all_trades) / len(all_trades):.1f}")
        # ★★ 大盘超额 —— 不给基准就等于没测（实测：个股-2.11% vs 大盘-2.30%）
        bm = build_benchmark(con, all_trades)
        if bm is not None:
            ex = bm.summary([t.ret_pct for t in all_trades])
            if ex.get("excess_avg") is not None:
                print(f"  --- 大盘基准 ---")
                print(f"  基准均涨跌    : {ex['benchmark_avg']:+.2f}%")
                print(f"  超额收益      : {ex['excess_avg']:+.2f}% "
                      f"(中位 {ex['excess_median']:+.2f}%)")
                ew = (f"{ex['excess_winrate']:.1%}"
                      if ex.get("excess_winrate") is not None else "不足")
                print(f"  超额胜率      : {ew}")
                if abs(ex["excess_avg"]) < 0.3:
                    print("  ★ 超额接近 0 ⇒ 信号只是**跟随大盘涨跌**，没有 alpha。")
                    print("     这与「绝对收益为负」是两回事，别混为一谈。")
        else:
            print("  --- 大盘基准 ---")
            print("  ⚠ 未能构造基准（缺 pct_chg 数据），无法判断 alpha。")
        print()
        print("  出场原因分布:")
        for reason, cnt in sorted(
                ((r, sum(1 for t in all_trades if t.exit_reason == r))
                 for r in {t.exit_reason for t in all_trades}),
                key=lambda x: -x[1]):
            rs = [t.ret_pct for t in all_trades if t.exit_reason == reason]
            print(f"    {reason:>12s}: {cnt:>4d} 笔  平均 {sum(rs) / len(rs):+.2f}%")
        print()
        print("  ★ 逐笔明细（前 10 笔）:")
        for t in all_trades[:10]:
            print(f"    {t.code} 入{t.entry_date}@{t.entry_price:.2f} "
                  f"出{t.exit_date}@{t.exit_price:.2f} "
                  f"{t.ret_pct:+6.2f}% {t.exit_reason}")
    print()
    # ---- A 方案对照实验：不同止损位下的效果
    if args.compare_exits:
        print()
        print("=" * 78)
        print("A 方案对照实验 · 不同止损位（假设你按该止损执行）")
        print("=" * 78)
        print(f"{'止损位':>10s}{'笔数':>6s}{'胜率':>8s}{'平均':>9s}"
              f"{'最差单笔':>10s}{'净盈亏':>12s}{'平均持有':>9s}")
        base_net = None
        for stop in (None, 12.0, 10.0, 8.0, 6.0, 5.0):
            ts: list = []
            for code in codes:
                bars = load_bars(con, code)
                if len(bars) < 120:
                    continue
                sh = {"seen": set(), "last_i": -1, "buys": [], "sells": []}
                rr = backtest(code, bars, make_entry_fn(bars, sh),
                              strategy_id="chanlun_base",
                              exit_fn=make_exit_fn(sh),
                              stop_loss_pct=stop, cost=cm)
                ts.extend(rr.trades)
            if not ts:
                continue
            w = len([t for t in ts if t.net_pnl > 0])
            net = sum(t.net_pnl for t in ts)
            worst = min(t.ret_pct for t in ts)
            avg = sum(t.ret_pct for t in ts) / len(ts)
            hold = sum(t.hold_days or 0 for t in ts) / len(ts)
            label = "不启用" if stop is None else f"-{stop:.0f}%"
            if base_net is None:
                base_net = net
            delta = "" if stop is None else \
                f"（较不启用收窄 {(1 - abs(net / base_net)) * 100:.0f}%）"
            print(f"{label:>10s}{len(ts):>6d}{w / len(ts) * 100:>7.1f}%"
                  f"{avg:>8.2f}%{worst:>9.2f}%{net:>12,.0f}{hold:>8.1f}d{delta}")
        print()
        print("  ★ 止损的代价是**胜率下降**（砍掉的是本来要反弹的仓位），")
        print("    换来的是**最差单笔与净亏大幅收窄**。该选哪档取决于你能承受")
        print("    多大回撤 —— 系统只给参考位，不替你决定。")

    # ---- D15：离场触发源对比（提高卖点覆盖率）
    if args.compare_exit_modes:
        print()
        print("=" * 78)
        print("D15 · 离场触发源对比（卖点覆盖率问题）")
        print("=" * 78)
        print("  背景：缠论卖点累计出现 5553 次，但「本日新可见」仅 122 次(2.2%)，")
        print("        而「收盘跌破最近中枢下沿」占 46.4% 的天数 ⇒ 卖点天然稀疏。")
        print()
        print(f"{'离场模式':>16s}{'卖点档':>10s}{'笔数':>6s}{'胜率':>8s}"
              f"{'平均':>9s}{'最差单笔':>10s}{'净盈亏':>12s}{'平均持有':>9s}")
        modes = [("chan", "strong"), ("chan", "normal"),
                 ("chan_all", "strong,normal"),
                 ("zs", "strong"), ("zs_ma", "strong"), ("none", "-")]
        for m, lv in modes:
            ts: list = []
            for code in codes:
                bars = load_bars(con, code)
                if len(bars) < 120:
                    continue
                sh = {"seen": set(), "last_i": -1, "buys": [], "sells": [],
                      "zs": None, "sell_levels": tuple(lv.split(","))}
                rr = backtest(code, bars, make_entry_fn(bars, sh),
                              strategy_id="chanlun_base",
                              exit_fn=make_exit_fn(sh, mode=m),
                              stop_loss_pct=args.stop_pct or None, cost=cm)
                ts.extend(rr.trades)
            if not ts:
                continue
            w = len([t for t in ts if t.net_pnl > 0])
            net = sum(t.net_pnl for t in ts)
            worst = min(t.ret_pct for t in ts)
            avg = sum(t.ret_pct for t in ts) / len(ts)
            hold = sum(t.hold_days or 0 for t in ts) / len(ts)
            wr = f"{w / len(ts) * 100:.1f}%" if len(ts) >= 10 else "不足"
            label = {"chan": "仅缠论卖点", "chan_all": "缠论含normal",
                     "zs": "+中枢破位", "zs_ma": "+中枢+均线",
                     "none": "无离场(对照)"}[m]
            print(f"{label:>16s}{lv:>10s}{len(ts):>6d}{wr:>8s}"
                  f"{avg:>8.2f}%{worst:>9.2f}%{net:>12,.0f}{hold:>8.1f}d")
        print()
        print("  ★ 看「平均持有」：越短说明卖点覆盖率越高（D14 里 14 天= 卖点太少）。")
        print("  ★ 但覆盖率不等于收益 —— 要同时看平均收益与净盈亏。")

    print(f"  耗时 {(datetime.now(CST) - t0).total_seconds():.1f} 秒")
    print()
    print("  ⚠️ 注意：单只标的的收益不可直接加总为组合收益 ——")
    print("     每笔都按 10万本金满仓计算，实际需按组合资金分配。")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())