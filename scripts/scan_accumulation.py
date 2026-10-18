#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hawk · 主力吸筹/控盘形态扫描（S1→S7）

对自选股 + 持仓股（可用 --codes 覆盖）跑量价状态机，输出当前所处阶段、
自选股标签、护盘底线与确认进场信号。

用法：
    python scripts/scan_accumulation.py                 # 扫自选 + 持仓
    python scripts/scan_accumulation.py --codes 600519,000001
    python scripts/scan_accumulation.py --json          # 机器可读输出
    python scripts/scan_accumulation.py --history 600519  # 看单只的流转记录

设计说明：
  · 数据全部来自本地 kline_daily（含 open/high/low/close/volume/turnover），
    不额外联网 —— 该模型只需要日线，本地已有 137 万行覆盖 5570 只。
  · as_of 由调用方注入，默认取本地日线的最新交易日（而非系统当前时间），
    避免在非交易日/盘中拿到半截数据后误判。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from backend.common import logging_setup  # noqa: E402
from backend.data import duck  # noqa: E402
from backend.data.sqlite import get_conn  # noqa: E402
from backend.domain.accumulation import (  # noqa: E402
    STAGE_CN, AccParams, Stage, evaluate, summary_table)

CST = timezone(timedelta(hours=8))

# 状态机需要的最少历史：S1 的「前5日均量」要 5 根，加形态本身约 10 根，
# 留足 60 根以覆盖 S4 的换手率窗口与边界情形。
LOOKBACK_BARS = 120


def _target_codes(explicit: list[str] | None) -> list[str]:
    if explicit:
        return [c.strip() for c in explicit if c.strip()]
    conn = get_conn()
    codes: list[str] = []
    for sql in ("SELECT code FROM position WHERE status='open'",
                "SELECT code FROM watchlist"):
        try:
            codes.extend(r[0] for r in conn.execute(sql).fetchall())
        except Exception as exc:                       # 表不存在/字段变动
            logger.warning(f"读取代码失败（{sql[:40]}…）: {exc}")
    seen, out = set(), []
    for c in codes:                                    # 去重且保持持仓优先
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _load_bars(con, codes: list[str]) -> tuple[dict[str, list[dict]], str]:
    """取每只股票最近 LOOKBACK_BARS 根日线。返回 (数据, 最新交易日)。"""
    if not codes:
        return {}, ""
    latest = con.execute(
        "SELECT max(trade_date) FROM kline_daily").fetchone()[0]
    if latest is None:
        return {}, ""
    ph = ",".join("?" * len(codes))
    rows = con.execute(
        f"""
        WITH r AS (
            SELECT code, trade_date, open, high, low, close, volume, turnover,
                   pct_chg,
                   row_number() OVER (PARTITION BY code
                                      ORDER BY trade_date DESC) AS rn
            FROM kline_daily
            WHERE code IN ({ph}) AND trade_date <= DATE '{latest}'
        )
        SELECT code, trade_date, open, high, low, close, volume, turnover, pct_chg
        FROM r WHERE rn <= {LOOKBACK_BARS}
        ORDER BY code, trade_date
        """,
        codes,
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r[0], []).append({
            "trade_date": r[1], "open": r[2], "high": r[3], "low": r[4],
            "close": r[5], "volume": r[6], "turnover": r[7], "pct_chg": r[8],
        })
    return out, str(latest)


def main() -> int:
    ap = argparse.ArgumentParser(description="主力吸筹/控盘形态扫描（S1→S7）")
    ap.add_argument("--codes", help="逗号分隔的代码，缺省用自选+持仓")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--history", help="只输出该股的流转记录")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_BARS,
                    help=f"回看根数（默认 {LOOKBACK_BARS}）")
    a = ap.parse_args()

    logging_setup.setup("INFO")
    con = duck.get_ro_conn()

    if a.history:
        explicit = [a.history]
    else:
        explicit = a.codes.split(",") if a.codes else None
    codes = _target_codes(explicit)
    if not codes:
        logger.warning("没有可扫描的标的（自选与持仓均为空，且未指定 --codes）")
        return 1

    bars_map, latest = _load_bars(con, codes)
    if not bars_map:
        logger.warning("本地日线无这些代码的数据，请先运行日线同步")
        return 1

    missing = [c for c in codes if c not in bars_map]
    if missing:
        logger.warning(f"{len(missing)} 只无本地日线，跳过：{missing[:8]}")

    # as_of 用本地日线的最后交易日收盘时点，不用系统时间
    as_of = datetime.fromisoformat(f"{latest}T15:00:00").replace(tzinfo=CST)
    logger.info(f"扫描 {len(bars_map)} 只，数据截至 {latest}")

    params = AccParams()
    results = {c: evaluate(c, rows, as_of, params)
               for c, rows in bars_map.items()}

    if a.history:
        r = results[a.history]
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if a.json:
        print(json.dumps({c: r.to_dict() for c, r in results.items()},
                         ensure_ascii=False, indent=2))
        return 0

    table = summary_table(results)
    print()
    print("=" * 78)
    print(f"【主力吸筹形态】数据截至 {latest}，共扫描 {len(results)} 只")
    print("=" * 78)
    if not table:
        print("  当前没有处于吸筹形态中的标的。")
        print()
        idle = [c for c, r in results.items() if r.stage is Stage.IDLE]
        print(f"  （{len(idle)} 只处于「无形态」状态）")
        return 0

    print(f"{'代码':<8}{'阶段':<10}{'标签':<10}{'买点':<6}"
          f"{'护盘底线':<10}{'入场区间':<18}")
    print("-" * 78)
    for row in table:
        sup = f"{row['support']:.2f}" if row["support"] else "—"
        entry = ("%.2f ~ %.2f" % tuple(row["entry"])) if row["entry"] else "—"
        print(f"{row['code']:<8}{row['stage']:<10}{row['tag'] or '—':<10}"
              f"{'★是' if row['is_buy'] else '否':<6}{sup:<10}{entry:<18}")

    print()
    print("阶段含义：S1 放量爆拉 → S2 上影线堆叠 → S3 缩量洗盘 → "
          "S4 换手趋稳 → S5 护盘底线 → S6 量价齐升 → S7 确认进场")
    print("★ 本结果仅为形态识别，不构成投资建议。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
