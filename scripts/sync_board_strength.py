# -*- coding: utf-8 -*-
"""采集并持久化当前板块快照。

八因子由 backend.domain.strength 计算，行业与概念分别排名。
缺失因子保持 NULL，按可用权重归一化，并记录 partial_factor_v1、
factor_coverage 与逐因子证据。净流入缺少流通市值分母时不计入 d5。
历史量比与排名斜率只读取此前各日最后一条快照。
实时接口不能回填历史；采集时间使用当前时间，原始字段保留供重算。

运行：python scripts/sync_board_strength.py [--dry-run]
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

from loguru import logger  # noqa: E402

from backend.data import duck  # noqa: E402
from backend.data.adapter import bootstrap, get_adapter  # noqa: E402
from backend.domain.strength import percentile, score_boards  # noqa: E402
from backend.domain.trade_cal import is_trade_day, prev_trade_day  # noqa: E402

CST = timezone(timedelta(hours=8))

# 八因子里本表能提供的（附录 4.7 的 d5「主力净流入」终于有了）
AVAILABLE_FACTORS = ("涨跌幅", "净流入(亿)", "上涨占比", "领涨股")


def to_strength(pct_chg: float | None, all_pcts: list[float]) -> float | None:
    """当日涨跌幅 → 横截面**排名百分位**（0~100）。

    ★ 为什么不用绝对涨跌幅：
      银行 +1% 与半导体 +5% 的含义完全不同，跨板块不可比。
      排名百分位天然抗极值，且每天都有分布（不会因为普跌就全挤在0）。

    ★ 无涨跌幅（页面结构变化/停牌）时返回 None ——
      当 0 会被读成「板块涨跌幅 0%」，即「持平」，
      与「无数据」是两回事。
    """
    if pct_chg is None or not all_pcts:
        return None
    return percentile(pct_chg, all_pcts)


def build_rows(
    board_rows: list[dict],
    ts: datetime,
    prev_ranks: dict[str, int] | None = None,
    history: dict[str, list[dict]] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """把适配器输出转成 `board_strength` 行（含排名与排名变化）。"""
    prev_ranks = prev_ranks or {}
    inputs = []
    for r in board_rows:
        past = (history or {}).get(r["board_code"], [])
        past = sorted(
            (p for p in past if p["ts"].date() < ts.date()), key=lambda p: p["ts"]
        )
        amounts = [p.get("amount") for p in past[-5:]]
        volume_ratio = None
        if len(amounts) == 5 and all(a is not None and a > 0 for a in amounts):
            if r.get("amount_yi") is not None:
                volume_ratio = r["amount_yi"] * 1e8 / (sum(amounts) / 5)
        ranks = [p.get("rank") for p in past[-3:]]
        slope = (
            (ranks[0] - ranks[-1]) / 2
            if len(ranks) == 3 and all(v is not None for v in ranks)
            else None
        )
        inputs.append(
            {
                **r,
                "d1_pct": r.get("pct_chg"),
                "d2_rs5": r.get("d2_rs5"),
                "d3_limit_ratio": r.get("d3_limit_ratio"),
                "d4_max_streak": r.get("max_streak"),
                # Raw inflow is not inflow / float market cap.
                "d5_inflow": r.get("d5_inflow"),
                "d6_vol_ratio": volume_ratio,
                "d7_up_ratio": r.get("up_ratio"),
                "d8_rank_slope": slope,
            }
        )
    scored = score_boards(inputs, ts)
    rank_of = {r["board_code"]: r["rank"] for r in scored if r["rank"] is not None}
    out: list[dict] = []
    for r in scored:
        bc = r["board_code"]
        rk = rank_of.get(bc)
        out.append(
            {
                "ts": ts.astimezone(CST).replace(tzinfo=None) if ts.tzinfo else ts,
                "board_code": bc,
                "board_name": r.get("name"),
                "type": r.get("type"),
                # strength = 排名百分位（与 rank 同源，rank 便于人看）
                "pct_chg": r.get("pct_chg"),  # 仅供报告展示，不入库
                "strength": r["strength"],
                "rank": rk,
                # ★ 无上次快照 ⇒ None（不是 0：「排名没变」是另一件事）
                "rank_change": (
                    prev_ranks.get(bc) - rk
                    if rk is not None and bc in prev_ranks
                    else None
                ),
                "limit_up_cnt": None,  # 需成分股，当前不可得
                "limit_down_cnt": None,
                "max_streak": None,
                # 净流入：同花顺单位是**亿元**，落库统一成元
                "net_inflow": (
                    r["net_inflow_yi"] * 1e8
                    if r.get("net_inflow_yi") is not None
                    else None
                ),
                "vol_ratio": r["d6_vol_ratio"],
                "up_ratio": r.get("up_ratio"),
                "leader_code": r.get("leader_code"),
                "amount": r["amount_yi"] * 1e8
                if r.get("amount_yi") is not None
                else None,
                "factor_coverage": r["factor_coverage"],
                "score_method": r["score_method"],
                "factors_json": json.dumps(
                    r["factors"], ensure_ascii=False, allow_nan=False
                ),
                "raw_json": json.dumps(
                    r, ensure_ascii=False, default=str, allow_nan=False
                ),
                "source": "ths_board",
                "source_complete": r.get("collection_complete", True),
            }
        )
    return out, rank_of


def upsert_board_strength(rows: list[dict]) -> int:
    if not rows:
        return 0
    c = duck.get_conn()
    c.executemany(
        """
        INSERT INTO board_strength
          (ts, board_code, board_name, type, strength, rank, rank_change,
           limit_up_cnt, limit_down_cnt, max_streak, net_inflow, vol_ratio,
           up_ratio, leader_code, pct_chg, amount, factor_coverage, score_method,
           factors_json, raw_json, source, source_complete)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ts, board_code) DO UPDATE SET
          board_name=EXCLUDED.board_name, type=EXCLUDED.type,
          strength=EXCLUDED.strength, rank=EXCLUDED.rank,
          rank_change=EXCLUDED.rank_change,
          limit_up_cnt=EXCLUDED.limit_up_cnt,
          limit_down_cnt=EXCLUDED.limit_down_cnt,
          max_streak=EXCLUDED.max_streak, net_inflow=EXCLUDED.net_inflow,
          vol_ratio=EXCLUDED.vol_ratio, up_ratio=EXCLUDED.up_ratio,
          leader_code=EXCLUDED.leader_code, pct_chg=EXCLUDED.pct_chg,
          amount=EXCLUDED.amount, factor_coverage=EXCLUDED.factor_coverage,
          score_method=EXCLUDED.score_method, factors_json=EXCLUDED.factors_json,
          raw_json=EXCLUDED.raw_json, source=EXCLUDED.source,
          source_complete=EXCLUDED.source_complete
        """,
        [
            (
                r["ts"],
                r["board_code"],
                r["board_name"],
                r["type"],
                r["strength"],
                r["rank"],
                r["rank_change"],
                r["limit_up_cnt"],
                r["limit_down_cnt"],
                r["max_streak"],
                r["net_inflow"],
                r["vol_ratio"],
                r["up_ratio"],
                r["leader_code"],
                r["pct_chg"],
                r["amount"],
                r["factor_coverage"],
                r["score_method"],
                r["factors_json"],
                r["raw_json"],
                r["source"],
                r["source_complete"],
            )
            for r in rows
        ],
    )
    return len(rows)


def last_ranks(as_of: datetime) -> dict[str, int]:
    """上一次采集点的排名（算 `rank_change` 用）。

    ★ 必须按**最新那个ts**取，不能按最大行数或全表聚合——
      同一天可能重复采集（重跑），按 ts 分组才对应得上。
    """
    c = duck.get_ro_conn()
    rows = c.execute(
        """SELECT board_code, rank FROM board_strength
           WHERE ts < ? AND rank IS NOT NULL AND score_method='partial_factor_v1'
             AND source_complete IS TRUE
           QUALIFY ts=max(ts) OVER (PARTITION BY type)""",
        [as_of.replace(tzinfo=None)],
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows if r[1] is not None}


def board_history(as_of: datetime) -> dict[str, list[dict]]:
    rows = (
        duck.get_ro_conn()
        .execute(
            """SELECT board_code, ts, amount, rank FROM board_strength
           WHERE CAST(ts AS DATE) < ? AND ts < ?
           QUALIFY row_number() OVER (
             PARTITION BY board_code, CAST(ts AS DATE) ORDER BY ts DESC) = 1
           ORDER BY ts""",
            [as_of.date(), as_of.replace(tzinfo=None)],
        )
        .fetchall()
    )
    output: dict[str, list[dict]] = {}
    for code, ts, amount, rank in rows:
        output.setdefault(code, []).append({"ts": ts, "amount": amount, "rank": rank})
    return output


def print_report(rows: list[dict], ranks: dict[str, int]) -> None:
    print()
    print("=" * 88)
    print("当日板块强度采集")
    print("=" * 88)
    ind = [r for r in rows if r["type"] == "industry"]
    con_ = [r for r in rows if r["type"] == "concept"]
    print(f"行业 {len(ind)} 个 / 概念 {len(con_)} 个，合计 {len(rows)}")
    print("采集字段：涨跌幅、净流入、上涨占比、领涨股")
    print("partial_factor_v1 是降级分；净流入缺少市值分母，不计入 d5")
    print(f"可用权重覆盖：{sorted({r['factor_coverage'] for r in rows})}")
    print(
        "★ 不可得：涨停家数/跌停家数/最高连板（需成分股，ajax 401）、"
        "量比（需成交额序列）"
    )
    print()
    ok = [r for r in ind if r["strength"] is not None]
    ok.sort(key=lambda r: -(r["strength"] or 0))
    print(
        f"{'#':>3s}{'板块':<14s}{'强度':>7s}{'涨跌%':>8s}{'净流入亿':>10s}"
        f"{'广度':>8s}  领涨股"
    )
    for r in ok[:10]:
        ni = r["net_inflow"]
        ni_s = f"{ni / 1e8:+.2f}" if ni is not None else "无"
        ur = r["up_ratio"]
        ur_s = f"{ur:.0%}" if ur is not None else "无"
        pc = r.get("pct_chg")
        pc_s = f"{pc:+.2f}" if pc is not None else "无"
        print(
            f"{r['rank']:>3d}{r['board_name']:<14s}{r['strength']:>7.1f}"
            f"{pc_s:>8s}{ni_s:>10s}{ur_s:>8s}  {r['leader_code'] or '—'}"
        )
    rc = sum(1 for r in rows if r["rank_change"] is not None)
    print()
    if rc:
        moved = sorted(
            [r for r in rows if r["rank_change"] is not None],
            key=lambda r: -abs(r["rank_change"]),
        )[:5]
        print("★ 排名变化最大（需要上次快照，故仅第2 次起有值）:")
        for r in moved:
            print(
                f"    {r['board_name']:<14s}{r['rank_change']:+d} 名（现 #{r['rank']}）"
            )
    else:
        print("⚠首次采集：`rank_change` 全为 NULL（无上次快照可比）——")
        print("  这是**正常**的，不是 bug。第二次采集起才会有排名变化。")
    print()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="采集并落库当日板块强度")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--as-of", default="", help="采集时点，仅允许当前时间附近")
    args = ap.parse_args()

    # ★★★ `ts` 必须按**交易日归一**，不能直接用 `datetime.now()`
    #主键是 `(ts, board_code)`，而板块强度本质是**日频**序列。
    #   早先直接用真实时刻，同一天重跑3 次就产生 3 个 ts
    #   （实测 2026-10-09 有 14:37:30 / 15:00:00 / 15:03:44 三份），
    #   于是「按天取当日板块」必须写 `QUALIFY row_number()...ORDER BY ts DESC`
    #   才能取到一份 —— **日频序列事实上退化成了「一堆日内快照」**，
    #   且 `rank_change` 会在同一天内自己跟自己比。
    #
    #   归一规则：同一交易日一律落到 15:00（收盘后）。
    #   ·盘中采集也会归到 15:00 ⇒ 同日重跑是 upsert 覆盖，天然幂等
    #   · 不会把盘中数据当成另一天的
    #
    # ★★★ 但「15:00 归一」有个前提：**当天必须是交易日**。
    #   实测踩坑（2026-10-10 周六）：非交易日跑这个脚本，实时接口返回的是
    #   **上一交易日收盘**的静态数据，而代码照样 `now.replace(hour=15)` 写成
    #   2026-10-10 15:00 —— 凭空造出一个库内不存在的交易日。
    #   后果实测：board_strength 10-10 有 50 行，但 kline_daily 10-10 是 **0 只**
    #   （因为 10-10 是周六）。任何按日对齐的回填都会把周六当成有行情的一天。
    #   ⇒ 非交易日必须 snap 回**最近一个交易日**，并显式告警。
    now = datetime.now(CST)
    if args.as_of:
        raw = datetime.strptime(args.as_of, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
        if abs((raw - now).total_seconds()) > 300:
            ap.error("实时接口不能生成历史快照；--as-of 必须在当前时间前后 5 分钟内")
        ts = raw
    else:
        ts = now

    day = ts.strftime("%Y-%m-%d")
    if not is_trade_day(day):
        # is_trade_day 在日历未知（future_unknown）时也返回 False，
        # 所以要先排除「日历没覆盖到」这种情况，否则会误判。
        snap = prev_trade_day(day)
        if snap is None or snap == day:
            print(f"★ {day} 非交易日但无法确定最近交易日，拒绝写入"
                  f"（宁可无数据，也不要造一个假交易日）")
            return 3
        print(f"★ {day} 非交易日（实时接口返回的是上一交易日收盘数据）"
              f"⇒ 归一到最近交易日 {snap}")
        ts = datetime.strptime(snap, "%Y-%m-%d").replace(tzinfo=CST)
    ts = ts.replace(hour=15, minute=0, second=0, microsecond=0)

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    # ★ 先开写连接：`get_ro_conn()` 在无写连接时会开只读连接，
    #   而 DuckDB **不允许**同一进程对同一文件同时持有两种配置
    #   （`Can't open a connection ... with a different configuration`）。
    #   `get_ro_conn` 内部会复用已存在的写连接，所以只要写连接先建立就没事。
    duck.init_db()

    bootstrap()
    ths = get_adapter("ths_board")

    all_rows: list[dict] = []
    failures = []
    for kind in ("industry", "concept"):
        try:
            got = ths.fetch_board_list(kind, ts)
            print(f"[{kind}] {len(got)} 个板块")
            all_rows.extend(got)
        except Exception as exc:
            failures.append(kind)
            print(f"[{kind}] 失败: {type(exc).__name__}: {exc}")
    if not all_rows:
        print("未取到任何板块，不写库")
        return 1

    prev = last_ranks(ts)
    rows, ranks = build_rows(all_rows, ts, prev, board_history(ts))
    print_report(rows, ranks)

    if args.dry_run:
        print("--dry-run：未写库")
        return 2 if failures else 0

    n = upsert_board_strength(rows)
    print(f"✔ 已写入 board_strength {n} 行（ts={ts:%Y-%m-%d %H:%M}）")
    total = duck.table_counts().get("board_strength")
    print(f"  表内现有 {total:,} 行")
    if total and total == n:
        print("  ⚠ 表内只有这 1 个采集点 ⇒ **板块强度历史只能靠逐日积累**")
        print("    （同花顺板块 K 线 404 / 成分股 401，无法回填）")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
