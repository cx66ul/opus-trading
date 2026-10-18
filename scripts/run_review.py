"""M8 实跑：对 signal 表里的历史信号做回溯验证 + 错误归因。

用法：
  python scripts/run_review.py --date 2026-10-08        # 评估某日之前的信号
  python scripts/run_review.py --all --max 200           # 评估全部（限量）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                                     # noqa: E402

from backend.data import duck# noqa: E402
from backend.data.sqlite import get_conn, log_event          # noqa: E402
from backend.domain.review import (                           # noqa: E402
    FAIL_REASONS, HORIZONS, MIN_SAMPLE_FOR_WINRATE,
    aggregate_stats, classify_failure, eval_signal, group_by_strength, mark_human_label, save_eval,
)

CST = timezone(timedelta(hours=8))


# ------------------------------------------------------------ 辅助
def _bars_after(code: str, sig_date: str, need: int = 20) -> list[dict]:
    """取信号日之后的前向 K 线（升序）。"""
    rows = duck.get_ro_conn().execute(
        """SELECT trade_date, close, high, low FROM kline_daily
           WHERE code = ? AND trade_date > ?
           ORDER BY trade_date ASC LIMIT ?""",
        [code, sig_date, need]).fetchall()
    return [{"trade_date": str(r[0]), "close": float(r[1]),
             "high": float(r[2]), "low": float(r[3])} for r in rows]


def _market_ret(sig_date: str, horizon: int) -> float | None:
    """同期大盘等权涨跌幅（用本地日线全市场均值代替指数）。

    ★ DuckDB 不支持 `DATE ?` 语法（占位符不能贴在函数名后），
      必须写成 `CAST(? AS DATE)` —— 实测踩过Parser Error。
    """
    row = duck.get_ro_conn().execute(
        """SELECT avg(pct_chg) FROM kline_daily
           WHERE trade_date > CAST(? AS DATE)
             AND code NOT LIKE '8%' AND code NOT LIKE '4%'
             AND trade_date IN (
               SELECT DISTINCT trade_date FROM kline_daily
               WHERE trade_date > CAST(? AS DATE)
               ORDER BY trade_date LIMIT ?)""",
        [sig_date, sig_date, horizon]).fetchone()
    if row and row[0] is not None:
        # N 日累计 ≈ 日均 × N（复利近似，误差可接受）
        return round(float(row[0]) * horizon, 2)
    return None


def _mkt_ret_cached(sig_date: str) -> dict[int, float]:
    out = {}
    for h in HORIZONS:
        r = _market_ret(sig_date, h)
        if r is not None:
            out[h] = r
    return out


# ------------------------------------------------------------ 主流程
def run(as_of: datetime, max_n: int = 200, only_pending: bool = True) -> dict:
    conn = get_conn()
    where = "WHERE ts < ?" if only_pending else ""
    sigs = conn.execute(
        f"""SELECT id, code, name, kind, ts, price, confidence, level,
                   eval_json, human_label, rule_version, param_hash
            FROM signal {where}
            ORDER BY ts DESC LIMIT ?""",
        ([as_of.date().isoformat(), max_n] if only_pending else [max_n])).fetchall()
    if not sigs:
        logger.info("无待评估信号")
        return {"evaluated": 0}

    logger.info(f"待评估信号 {len(sigs)} 条")
    evals = []
    fail_counts: dict[str, int] = {r: 0 for r in FAIL_REASONS}
    n_saved = n_skip = 0

    for row in sigs:
        (sid, code, name, kind, ts, price, conf, level,
         eval_json, hlabel, rule_ver, param_hash) = row
        if not price:
            continue
        bars = _bars_after(code, ts, 20)
        if len(bars) < 5:                # 至少要5 日才有评估意义
            continue
        ev = eval_signal(sid, code, kind, ts, float(price), bars,
                         params_ver=rule_ver or "chan-v1",
                         param_hash=param_hash or "", as_of=as_of,
                         level=level or "")
        evals.append(ev)
        n_saved += save_eval(ev, as_of)
        if n_saved == 0:
            n_skip += 1

        # 错误归因（仅对失败信号）
        h5 = ev.outcomes.get(5)
        if h5 and h5.ret_pct is not None and not h5.hit:
            mk = _mkt_ret_cached(ts)
            r = classify_failure({"level_conflict": False}, ev,
                                 market_ret=mk.get(5), sector_ret=None)
            if r:
                fail_counts[r] += 1

    logger.info(f"评估完成：{len(evals)} 条（写入 {n_saved} / 已存在跳过 {n_skip}）")

    # ---- 聚合统计
    print()
    print("=" * 78)
    print(f"信号回溯验证（{len(evals)} 条，{as_of.date()}）")
    print("=" * 78)
    overall = {}
    print(f"  {'窗口':>5s}{'买点样本':>9s}{'买点胜率':>10s}{'平均收益':>10s}"
          f"{'盈亏比':>8s} | {'卖点样本':>9s}{'卖点胜率':>10s}{'平均收益':>10s}")
    for h in HORIZONS:
        st = aggregate_stats(evals, h)
        overall[h] = st
        b, sl = st["buy"], st["sell"]
        bw = f"{b['winrate']:.1%}" if b["winrate"] is not None else "不显示"
        sw = f"{sl['winrate']:.1%}" if sl["winrate"] is not None else "不显示"
        ba = f"{b['avg_ret']:+.2f}%" if b["avg_ret"] is not None else "—"
        sa = f"{sl['avg_ret']:+.2f}%" if sl["avg_ret"] is not None else "—"
        bp = f"{b['pl_ratio']:.2f}" if b["pl_ratio"] is not None else "—"
        print(f"  {h:>4d}日{b['samples']:>9d}{bw:>10s}{ba:>10s}{bp:>8s} | "
              f"{sl['samples']:>9d}{sw:>10s}{sa:>10s}")

    print()
    print("  ★ 买卖方向必须分开看。实测教训：143 条混合算得胜率 87.4%、")
    print("    平均收益 -3.3% —— 两个方向互相抵消，混合胜率毫无意义。")

    if not overall[5]["buy"]["enough_sample"]:
        print(f"  ★ 买点样本不足 {MIN_SAMPLE_FOR_WINRATE} 条，按附录 5.4 不显示胜率")
        print(f"    {overall[5]['buy']['winrate_note']}")
        print(f"    这不是 bug —— 「1 单 1 胜 = 100%」是最危险的误导")

    # ---- 错误归因
    print()
    print("=" * 78)
    print("错误归因聚类（附录 7.1④）")
    print("=" * 78)
    total_fail = sum(fail_counts.values())
    if total_fail == 0:
        print("  无失败信号（5 日窗口全部命中）")
    else:
        for k, v in sorted(fail_counts.items(), key=lambda x: -x[1]):
            bar = "█" * max(1, v * 30 // total_fail)
            print(f"  {k:10s} {v:>4d} 条 ({v/total_fail:>6.1%}) {bar}")
        print()
        print("  ★ 归因顺序：大盘拖累 → 板块退潮 → 消息误判")
        print("    先排除系统性原因，避免把市场风险误判成模型问题")

    # ---- 分策略（附录 7.1⑤）
    print()
    print("=" * 78)
    print("分类胜率对比（附录 7.1⑤策略对比）")
    print("=" * 78)
    by_kind: dict[str, list] = {}
    for ev in evals:
        by_kind.setdefault(ev.kind, []).append(ev)
    print(f"  {'类型':10s}{'样本':>6s}{'买点胜率':>10s}{'平均收益':>10s}{'卖点胜率':>10s}{'平均收益':>10s}")
    for kind in sorted(by_kind):
        st = aggregate_stats(by_kind[kind], 5)
        b, sl = st["buy"], st["sell"]
        bw = f"{b['winrate']:.1%}" if b["winrate"] is not None else "不足"
        sw = f"{sl['winrate']:.1%}" if sl["winrate"] is not None else "不足"
        ba = f"{b['avg_ret']:+.2f}%" if b["avg_ret"] is not None else "—"
        sa = f"{sl['avg_ret']:+.2f}%" if sl["avg_ret"] is not None else "—"
        print(f"  {kind:10s}{st['samples']:>6d}{bw:>10s}{ba:>10s}{sw:>10s}{sa:>10s}")
    print()
    print("  「不足」= 样本 <10，按规则不显示胜率（不是 0%）")

    # ----★★ 按 kind + level 分组（不分组会得出相反结论）
    print()
    print("=" * 78)
    print("按 kind + 强度分档（必须分档：同一 kind 内强弱表现可差 60 个百分点）")
    print("=" * 78)
    by_lv = group_by_strength(evals, 5)
    print(f"  {'kind/level':>16s}{'样本':>6s}{'买点胜率':>10s}{'平均收益':>10s}"
          f"{'卖点胜率':>10s}{'平均收益':>10s}")
    for key in sorted(by_lv):
        st = by_lv[key]
        b, sl = st["buy"], st["sell"]
        bw = f"{b['winrate']:.1%}" if b["winrate"] is not None else "不足"
        sw = f"{sl['winrate']:.1%}" if sl["winrate"] is not None else "不足"
        ba = f"{b['avg_ret']:+.2f}%" if b["avg_ret"] is not None else "—"
        sa = f"{sl['avg_ret']:+.2f}%" if sl["avg_ret"] is not None else "—"
        print(f"  {key:>16s}{st['samples']:>6d}{bw:>10s}{ba:>10s}{sw:>10s}{sa:>10s}")
    print()
    print("  说明：3buy/weak 是「已突破、回抽未确认」的观察态，不是买入信号，")
    print("        其价格口径为信号日收盘价，不可与 strong 同表比较优劣。")

    # ---- 报告落盘
    out = _ROOT / "data" / "review_report.json"
    report = {
        "as_of": as_of.isoformat(timespec="seconds"),
        "evaluated": len(evals),
        "horizons": {str(h): overall[h] for h in HORIZONS},
        "fail_reasons": fail_counts,
        "by_kind": {k: aggregate_stats(v, 5) for k, v in by_kind.items()},
        "by_kind_level": by_lv,
        "params_frozen": "规则版本与参数哈希取自 signal 表，不用当前值",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"复盘报告已写出: {out}")

    log_event("review_run",
              {"evaluated": len(evals),
               "fail_total": total_fail,
               "enough_sample": overall[5]["enough_sample"]},
              as_of.isoformat(timespec="seconds"))
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="M8 信号回溯验证")
    ap.add_argument("--date", help="as_of 日期")
    ap.add_argument("--max", type=int, default=200)
    ap.add_argument("--all", action="store_true", help="含已评估的信号")
    args = ap.parse_args()
    as_of = (datetime.fromisoformat(args.date).replace(tzinfo=CST)
             if args.date else datetime.now(CST))
    logger.info(f"as_of = {as_of.isoformat(timespec='seconds')}")
    run(as_of, max_n=args.max, only_pending=not args.all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())