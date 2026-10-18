# -*- coding: utf-8 -*-
"""★ 六态 `market_regime` 门控 —— 把 M5 状态机真正接进入场过滤。

★★★ 为什么需要这个脚本（D17 之后的续作）
------------------------------------------
`run_regime_gate.py` 已用**等权指数 60 日滚动**验证过门控，结论是：
    none    样本外超额 +1.42%
    up      样本外超额 +1.75%      ← 只提升 +0.33pp
    no_flat 样本外超额 +1.23%
⇒ **门控有效但增益很小**，D17因此判定「暂不建议上线」。

★ 那为什么还要再试一次？因为它用的是**另一类信息**
------------------------------------------------
  · 等权指数滚动 = **趋势**信息（这只股票池整体涨没涨）
  · 六态状态机   = **情绪/结构**信息（涨停家数、连板高度、
                                昨日涨停溢价、炸板率、量能）
D16 已证明买点「在上涨市有 alpha（+1.61%）、震荡市最差（-1.28%）」，
而六态里恰好有一档叫「修复」—— 语义上就是**从冰点回升**，
正是「买点该出现」的环境。

⇒ 若「趋势门控增益小」是因为趋势信息与买点 alpha 相关性低，
   那换成结构信息应该能拿到更多。这是要验证的，不是假设。

★★ 三条口径纪律（全部来自前几轮踩过的坑）
------------------------------------------
1. **只用 point-in-time 状态**。`market_regime` 是逐日回填的，
   每行只用该日及之前的数据算成（`regime.py` 的输入全是当日口径），
   **无前视**。但仍要自检：状态 ts 与K 线日期必须对齐，
   且不能用「收盘后才知道的状态」去筛「当天的买点」——
   盘中 09:30 决策时，当天 15:00 的状态是未知的。
   ⇒ **只能用 `ts < 决策日` 的状态**，见 `_RegimeGate`。

2. **全样本连续跑，只按入场日统计**。不能按状态切片重跑
   （会断 `seen` 连续性、信号归零 —— `run_oos` 的坑）。

3. **必须与 `none` 基线同口径对照**。门控只减少交易数，
   若只看门控组的绝对收益，会把「交易变少」误读成「策略变好」。

★ 为什么还要额外做「标签泄漏」对照
------------------------------------
市场状态有 6 档，若门控组合过多，很容易**在 6 个标签上挑出样本外最好的那个**
—— 那等于把样本外当训练集。防护：
  · 门控方案**事先写死**在 `GATES` 里，不做穷举
  · 只报告预设方案的对比，不报告「最优方案」
  · 每个方案都跑 `stability_check`（样本量稳定性）

运行：
    python scripts/run_regime_gate6.py --limit 120
    python scripts/run_regime_gate6.py --limit 120 --stability
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb                                    # noqa: E402

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

from backend.domain.backtest import CostModel, backtest      # noqa: E402
from backend.domain.regime import REGIME_META                # noqa: E402

CST = timezone(timedelta(hours=8))

#★ 门控方案**事先写死**，不做穷举 ——
#   6 档两两组合有 30 种，若「遍历所有组合挑样本外最好的」
#   就等于把样本外当训练集（`run_oos` 已踩过这个坑）。
GATES = {
    "none":      None,                                # 基线：不过滤
    # ── D16 直译：只在「启动/发酵」出买点 ──
    "trend_on":  ("start", "ferment"),
    # ── 放宽：含「修复」（语义 = 从冰点回升）──
    "trend_repair": ("start", "ferment", "repair"),
    # ── 只避开最差档 ──
    "avoid_bad":("start", "ferment", "repair"),
    # ── 反向对照：只在最差档（退潮）出买点（预期最差）──
    #  ★ 原用「高潮」做反向对照，但阈值重校准后 251 天里**高潮 0 天**
    #   （p90=77 家 + 炸板率 p50 以上，两条同时成立才触发）
    #   ⇒ `peak_only` 放行 0%，无任何成交，当对照是废的。
    #   改用「退潮」（4 天）—— 语义上也更该测：退潮=禁新开仓。
    "ebb_only":   ("ebb",),
}

# 决策时点：09:30 开盘。当天 15:00 的状态此刻**未知** ⇒只能用 < D
DECISION_HOUR = 9
DECISION_MIN = 30


# ================================================================== 状态门控
class RegimeGate:
    """★ 按 `market_regime` 表做 point-in-time 门控。

    ★★★ 为什么必须用 `ts < 决策日` 而不是 `ts <= 决策日`
    --------------------------------------------------
    `market_regime` 每行的 ts 是该交易日 **15:00**（收盘后算出的状态）。
    而买点决策发生在 **09:30开盘** —— 此刻当天 15:00 的状态**还不存在**。
    若用 `ts <= 当天`，就把「收盘后才知道的信息」用于「开盘时的决策」
    ⇒ **典型前视偏差**，且回测结果会虚高。

    实测这类偏差很难发现：结果「看起来合理」（超额更高），
    但那是因为用到了未来信息。
    ⇒ 本类**只暴露 `ts < decision_date` 的状态**，无「宽松」选项。
    """

    def __init__(self, rows: list[tuple]) -> None:
        # rows: [(date_str, state)]，升序
        self._states: dict[str, str] = {}
        for d, s in rows:
            self._states[str(d)[:10]] = s
        self._dates = sorted(self._states)

    def state_on(self, decision_date: str) -> str | None:
        """决策日**之前**最近一个已收盘交易日的状态。

        决策日若本身已有 15:00 的状态（例如收盘后回测），
        也不返回它 —— 因为决策发生在当天 09:30。
        """
        prior = [d for d in self._dates if d < decision_date]
        if not prior:
            return None
        return self._states[prior[-1]]

    def allow(self, decision_date: str, states) -> bool:
        """该日是否允许出买点。

        ★ 状态**查不到**时返回 True（放行）而不是 False：
          「没数据」不等于「环境差」。
          若return False，会把「状态表覆盖不足」变成
          「策略在某些日子完全不出信号」—— 而原因不是市场不好，
          是数据缺失。这种静默的信号萎缩极难察觉。
        """
        if states is None:
            return True
        s = self.state_on(decision_date)
        if s is None:
            return True
        return s in states

    def coverage(self, states) -> dict:
        """门控覆盖率 —— 门控太强会把信号全滤掉，必须能看到。"""
        n = len(self._dates)
        if not n or states is None:
            return {"allowed_pct": 100.0, "n_days": n, "by_state": {}}
        cnt: dict[str, int] = defaultdict(int)
        for d in self._dates:
            cnt[self._states[d]] += 1
        allowed = sum(v for k, v in cnt.items() if k in states)
        return {
            "allowed_pct": round(allowed / n * 100, 1),
            "n_days": n,
            "by_state": dict(cnt),
        }


def load_gate(con) -> RegimeGate:
    rows = con.execute(
        "SELECT CAST(ts AS DATE), state FROM market_regime ORDER BY ts"
    ).fetchall()
    return RegimeGate([(str(r[0]), r[1]) for r in rows])


# ================================================================== 门控装配
def make_gated_entry(shared: dict, gate: RegimeGate, states):
    """把状态门控包在 `entry_fn` 外层。

    ★ 与 `run_regime_gate` 同样的分层：门控在**信号层**，
      引擎不知道市场状态（与 D13「系统给参考」定位一致）。
    """
    inner = rb.make_entry_fn(shared.get("bars", []), shared)
    if states is None:
        return inner

    def gated(bar, i, allb):
        if not gate.allow(bar.d, states):
            return None
        return inner(bar, i, allb)
    return gated


# ================================================================== 统计
def pick_codes(con, scan: int, limit: int, seed: int = 42) -> list[str]:
    """★ **随机采样**取码。

    ★★ 两个曾经踩过的坑，都要避开：

    1. `ORDER BY code LIMIT n` —— 代码号有序，取前 N 只恰好全是
       `920xxx` 北交所（实测前 200 只全是北交所），样本完全不代表市场。
    2. **先 `ORDER BY code LIMIT scan` 再随机** —— 这样抽出来的池子
       本身就偏。实测 scan=800 时池子里 100% 是深市（`000`/`001`/`002`），
       随机后抽 100 只仍是清一色深市 —— 随机救不了偏池。

    ⇒ 正确做法：**先在全库范围内随机抽 limit 只**，
       `scan` 只用于「历史不足的票先排除」（避免抽到次新股）。
    """
    if not rb.PARQUET.exists():
        return []
    # 先过滤掉历史不足的票（用全库，不先排序限量）
    pool = [r[0] for r in con.execute(
        f"""SELECT code FROM read_parquet('{rb.PARQUET.as_posix()}')
            GROUP BY code HAVING count(*) >= 200""").fetchall()]
    if not pool:
        return []
    rng = random.Random(seed)
    # ★ 先随机打乱再取 limit（不是在排序后的子集里抽）
    rng.shuffle(pool)
    picked = sorted(pool[:limit])
    # 自检：市场分布若极度集中，说明池子又有问题
    buckets: dict[str, int] = defaultdict(int)
    for c in picked:
        buckets[c[:3]] += 1
    top = max(buckets.values()) / max(1, len(picked))
    if top > 0.6:
        print(f"  ⚠ 样本市场分布集中：{dict(buckets)}"
              f"（单一前缀占 {top:.0%}）—— 池子可能有偏")
    return picked


def run_gated(codes: list[str], con, gate: RegimeGate, states,
              stop_pct: float = 5.0, exit_mode: str = "chan",
              oos_ratio: float = 0.4,
              cost: CostModel | None = None) -> dict:
    """全样本连续跑一次门控回测，返回样本内/外统计。

    ★ 与 `run_regime_gate.run_gated` **同口径**（同样的 shared 结构、
      同样的 `sell_levels=("strong",)`、同样的 seen 连续语义），
      这样两者的 Δ 才能直接比。
    """
    cost = cost or CostModel()
    trades = []
    for code in codes:
        bars = rb.load_bars(con, code)
        if len(bars) < 150:
            continue
        shared = {"seen": set(), "last_i": -1, "buys": [], "sells": [],
                  "zs": None, "sell_levels": ("strong",), "bars": bars}
        entry = make_gated_entry(shared, gate, states)
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
        "train": stats_with_bench(ts[:split], bench[:split], "训练段"),
        "oos": stats_with_bench(ts[split:], bench[split:], "样本外"),
    }


def stats_with_bench(trades, bench: list, label: str) -> dict:
    """统计 + 大盘超额。★ 没有超额就不能判断 edge（D15/D90）。"""
    base = oo.stats_of(trades, label)
    ex = [(t.ret_pct, b) for t, b in zip(trades, bench) if b is not None]
    rets = [e[0] for e in ex]
    if not rets:
        base.update({"excess_avg": None, "excess_winrate": None,
                     "bench_avg": None, "excess_median": None})
        return base
    n = len(rets)
    diffs = sorted(r - b for r, b in ex)
    base.update({
        "excess_avg": round(sum(diffs) / n, 2),
        "excess_median": diffs[n // 2],
        "excess_winrate": (round(len([x for x in diffs if x > 0]) / n, 4)
                           if n >= 10 else None),
        "bench_avg": round(sum(b for _, b in ex) / n, 2),
    })
    return base


def main() -> int:
    ap = argparse.ArgumentParser(description="六态 market_regime 门控")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--scan", type=int, default=800)
    #★★★ 单位必须是**百分数**（`stop_loss_pct=5.0` 表示 5%），
    #   不是比例（`0.05`）。曾写成 default=0.05⇒ 变成 **0.05% 止损**，
    #   买入当天必然触价 ⇒ 328 笔全部 `stop_loss`，
    #   而每笔收益恒等于**往返成本 -0.9%**（佣金 0.05+ 滑点 0.4 + 印花税 0.05）。
    #
    #   ★ 这个 bug 的症状极具欺骗性：不同门控的统计值**完全相同**
    #     （超额都等于平均收益、胜率全 0），
    #     看起来像「门控毫无影响」，实际是止损把所有交易都砍成了当日对倒。
    #     它是被「所有门控结果一模一样」这个反常现象逼出来的。
    ap.add_argument("--stop-loss", type=float, default=5.0,
                    help="止损百分比（如 5 表示 5%%，★不是 0.05）")
    ap.add_argument("--exit-mode", default="chan")
    ap.add_argument("--stability", action="store_true", help="样本量稳定性检验")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    # ★★ 两个连接必须分开：
    #   · pcon —— **内存 DuckDB**，用于 read_parquet（`rb.load_bars` /
    #     `rb.build_benchmark` 的 SQL 全部走 read_parquet）
    #   · dcon —— **库文件 DuckDB**，用于读 market_regime
    #   误用同一个连接时：`load_bars` 查不到任何bar ⇒ 成交为空或极少，
    #   而 `build_benchmark` 同样失败 ⇒ bench 为空 ⇒
    #   「所有门控的超额都等于平均收益、胜率全是 0%」——
    #   ★ 症状是不同门控结果**完全相同**，一眼就该看出是 bug，
    #     但不看这一点就会误读成「门控毫无影响」。
    pcon = duckdb.connect()
    dcon = duckdb.connect(str(_ROOT / "data" / "hawk.duckdb"), read_only=True)
    gate = load_gate(dcon)
    if not gate._dates:
        print("market_regime 为空 —— 先跑 scripts/backfill_regime.py")
        return 1

    print("=" * 92)
    print("六态市场状态门控（M5 状态机 → 入场过滤）")
    print("=" * 92)
    print(f"状态表覆盖 {len(gate._dates)} 个交易日："
          f"{gate._dates[0]} ~ {gate._dates[-1]}")
    cnt: dict[str, int] = defaultdict(int)
    for d in gate._dates:
        cnt[gate._states[d]] += 1
    for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]):
        m = REGIME_META.get(k, {"label": k, "emoji": "?"})
        print(f"  {m['emoji']} {m['label']:4s} {v:4d} 天 {v / len(gate._dates):6.1%}")
    print()
    print("★ 门控覆盖率（门控太强会把信号全滤掉）:")
    for name, states in GATES.items():
        c = gate.coverage(states)
        label = "基线" if states is None else \
            "+".join(REGIME_META[s]["label"] for s in states)
        print(f"  {name:14s} 放行 {c['allowed_pct']:5.1f}%   {label}")
    print()

    codes = pick_codes(pcon, args.scan, args.limit)
    # ★ 显式打印止损值：单位错误（0.05 vs 5.0）从输出就能看出来
    print(f"标的 {len(codes)} 只 | 止损 {args.stop_loss}% | "
          f"离场 {args.exit_mode} | 基准=全市场等权")
    print()
    cost = CostModel()
    result: dict[str, dict] = {}

    for name, states in GATES.items():
        o = run_gated(codes, pcon, gate, states,
                      stop_pct=args.stop_loss, exit_mode=args.exit_mode,
                      cost=cost)
        if "error" in o:
            print(f"{name:14s} {o['error']}")
            continue
        st = o["oos"]
        result[name] = {"stats": st, "total": o["total"]}
        ex = st.get("excess_avg")
        exs = f"{ex:+.2f}%" if ex is not None else "—"
        wr = st.get("winrate")
        # ★ `stats_of` 返回的 winrate 已是 0~1 比例，格式化为百分比时
        #   用 `{:.1%}` 即可；再乘 100 会得到「3058%」这种荒唐值。
        print(f"{name:14s} 笔数{o['total']:>5d}  样本外{st['n']:>4d}  "
              f"胜率{(f'{wr:.1%}' if wr is not None else '—'):>7s}  "
              f"平均{(st['avg_ret'] if st['avg_ret'] is not None else 0):>6.2f}%  "
              f"超额{exs:>8s}  {st.get('note', '')}")

    base = result.get("none", {}).get("stats", {})
    b_ex = base.get("excess_avg")
    b_win = base.get("winrate")
    print()
    print("=" * 92)
    print("★ 与基线（none）对比 —— 门控只减少交易数，须看**超额**变化")
    print("=" * 92)
    b_tr = result.get("none", {}).get("total", 0)
    for name in GATES:
        if name == "none" or name not in result:
            continue
        st = result[name]["stats"]
        ex = st.get("excess_avg")
        if ex is None or b_ex is None:
            continue
        delta = ex - b_ex
        n_diff = result[name]["total"] - b_tr
        wr = st.get("winrate")
        wr_delta = (f"{(wr - b_win) * 100:+.1f}pp"
                    if wr is not None and b_win is not None else "—")
        # （此处乘 100 是对的：差值要用百分点 pp 表示）
        print(f"  {name:14s} 超额{ex:+.2f}%  Δ={delta:+.2f}pp  "
              f"成交{n_diff:+d} 笔  胜率Δ{wr_delta:>8s}  "
              f"{'★改善' if delta > 0.05 else ('变差' if delta < -0.05 else '持平')}")

    # 反向对照：只在退潮出买点，必须明显差于基线，否则说明门控没生效
    for ctl in ("ebb_only",):
        if ctl not in result:
            continue
        c = result[ctl]["stats"]
        c_ex = c.get("excess_avg")
        if c_ex is None or b_ex is None:
            print(f"  反向对照 {ctl} 样本不足（{result[ctl]['total']} 笔），无法判定")
            continue
        print()
        print(f"  反向对照 {ctl} 超额 {c_ex:+.2f}% "
              f"（应明显低于基线 {b_ex:+.2f}% —— 否则说明门控没起作用）")
        if c_ex >= b_ex:
            print(f"  ★★ {ctl} 不低于基线 ⇒ 门控逻辑可能没生效，需排查")
        else:
            print(f"  ✔ 方向正确（比基线低 {b_ex - c_ex:.2f}pp）⇒ 门控确实在起作用")

    if args.stability:
        print()
        print("=" * 92)
        print("样本量稳定性检验（D14：单一样本量的结论不作数）")
        print("=" * 92)
        for name in ("none", "trend_on", "trend_repair"):
            states = GATES[name]
            print(f"\n【{name}】")
            series = []
            for n in (60, 120, 200, 300):
                sub = pick_codes(pcon, args.scan, n)
                o = run_gated(sub, pcon, gate, states,
                              stop_pct=args.stop_loss,
                              exit_mode=args.exit_mode, cost=cost)
                if "error" in o:
                    print(f"   {n:>4d} 只  {o['error']}")
                    continue
                ex = o["oos"].get("excess_avg")
                exs = f"{ex:+.2f}%" if ex is not None else "—"
                print(f"   {n:>4d} 只  成交{o['total']:>4d}  "
                      f"样本外{o['oos']['n']:>4d}  超额{exs:>8s}")
                series.append(ex)
            ok = [x for x in series if x is not None]
            if len(ok) >= 2:
                pos = sum(1 for x in ok if x > 0)
                print(f"   ⇒{len(ok)} 档中 {pos} 档为正"
                      f"  {'稳定' if pos == len(ok) or pos == 0 else '★不稳定（符号随样本量翻转）'}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n已写出 {args.json_out}")

    dcon.close()
    pcon.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())