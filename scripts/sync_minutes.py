"""分钟线同步（解锁 M8 ①③ 与 M9）。

★ 关键约束（实测 2026-10-09，不是猜测）：
  | 周期 | count=320 | count=720 | 覆盖交易日 |
  |------|-----------|----------|-----------|
  | m1   | 482 根    | 482 根   | **2 天**   |
  | m5   | 482 根    | 482 根   | **11 天**  |
  | m15  | 482 根    | 482 根   | 31 天|
  | m30  | 482 根    | **720 根** | 61 天 |
  | m60  | 482 根    | **720 根** | 121 天 |

  两个坑：
  1. **count 只认 320/ 720**，传1000/2000/5000/8000 都**静默回落**到 320，
     返回 482 根且无任何报错（同 M1 的 fqkline 陷阱）。
     若不察觉，会以为"拿到了 320 根其实够用"，实际丢历史。
  2. **m1 只有 2 天、m5 只有 11 天** —— 覆盖远小于 m30/m60。
     所以「全市场 × 1分钟」既慢又没用（存了也只剩2 天）。

★ 本脚本的取舍：
  · 全市场同步 **m30/m60**（回测与多级别分析的主力）
  · **跟踪池 + 持仓 + 自选** 同步 **m5/m1**（消息联动回看需要分钟精度）
  这样既拿到全市场的中期结构，又在关注标的上拿到细粒度。

  受限说明：m1 只有 2 天历史，**M8③「消息后 3 分钟怎么走」只能回看最近 2 天**。
  这是数据源限制，不是实现问题 —— UI 必须标注实际可回溯范围。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                                   # noqa: E402

from backend.data import duck                              # noqa: E402
from backend.data.adapter import bootstrap, get_adapter     # noqa: E402
from backend.data.sqlite import get_conn, log_event        # noqa: E402

CST = timezone(timedelta(hours=8))

# 实测：哪些 count 值是有效的（其他值静默回落）
VALID_COUNTS = (320, 720)

# 各周期的实测覆盖交易日（用于决定同步范围与文档标注）
COVERAGE_DAYS = {"m1": 2, "m5": 11, "m15": 31, "m30": 61, "m60": 121}


def _focus_codes() -> list[str]:
    """跟踪池 + 持仓 + 自选股 —— 需要分钟精度的标的。"""
    c = get_conn()
    out: set[str] = set()
    for q in ("SELECT code FROM watchlist",
              "SELECT code FROM position WHERE status='holding'"):
        try:
            out.update(r[0] for r in c.execute(q).fetchall())
        except Exception as exc:
            logger.warning(f"读取{q[:24]} 失败: {exc}")
    return sorted(out)


def _valid_count(want: int) -> int:
    """把任意 count 归到有效值。传1000 会被静默回落，这里提前纠正。"""
    if want in VALID_COUNTS:
        return want
    for v in sorted(VALID_COUNTS):
        if want <= v:
            return v
    return max(VALID_COUNTS)


def sync_minutes(codes: list[str], periods: list[str], count: int = 720,
                 batch_pause: float = 0.35,
                 as_of: datetime | None = None) -> dict:
    """同步分钟线。分批 + 限流 + 断点续传（DuckDB 主键去重天然幂等）。"""
    bootstrap()
    tx = get_adapter("tencent")
    cnt = _valid_count(count)
    if cnt != count:
        logger.warning(f"count={count} 不是有效值，已纠正为 {cnt}"
                       f"（无效值会静默回落到 320）")

    total_rows = 0
    per_period = {p: 0 for p in periods}
    failed: list[tuple[str, str]] = []
    t0 = time.monotonic()

    for i, code in enumerate(codes, 1):
        for per in periods:
            try:
                bars = tx.fetch_minute(code, per, cnt, as_of=as_of)
                if not bars:
                    continue
                duck.upsert_kline_min(bars)
                total_rows += len(bars)
                per_period[per] += len(bars)
            except Exception as exc:
                failed.append((code, f"{per}: {type(exc).__name__} {str(exc)[:40]}"))
        if i % 50 == 0:
            el = time.monotonic() - t0
            rate = i / max(el, 0.001)
            eta = (len(codes) - i) / max(rate, 0.001)
            logger.info(f"[{i}/{len(codes)}] {total_rows:,} 行 "
                        f"| 速率 {rate:.1f} 只/s | 预计剩余 {eta / 60:.1f} 分钟")
            time.sleep(batch_pause)
        elif i % 10 == 0:
            time.sleep(batch_pause * 0.4)

    el = time.monotonic() - t0
    logger.info(f"同步完成：{len(codes)} 只 × {len(periods)} 周期 -> "
                f"{total_rows:,} 行，耗时 {el / 60:.1f} 分钟，失败 {len(failed)}")
    if failed:
        logger.warning("失败样例: " + "; ".join(f"{c}({e})" for c, e in failed[:4]))

    stats = {"codes": len(codes), "periods": periods, "count": cnt,
             "rows": total_rows, "per_period": per_period,
             "failed": len(failed), "elapsed_min": round(el / 60, 2)}
    log_event("min_sync", stats, (as_of or datetime.now(CST)).isoformat(timespec="seconds"))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="分钟线同步")
    ap.add_argument("--codes", help="代码清单文件")
    ap.add_argument("--periods", default="m30,m60",
                    help="逗号分隔，默认 m30,m60（全市场）")
    ap.add_argument("--focus-only", action="store_true",
                    help="只同步跟踪池+持仓+自选（可含 m1/m5）")
    ap.add_argument("--count", type=int, default=720)
    ap.add_argument("--limit", type=int, help="只处理前 N 只（试跑）")
    ap.add_argument("--all", action="store_true", help="全市场 5571 只")
    args = ap.parse_args()

    as_of = datetime.now(CST)
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]

    if args.focus_only:
        codes = _focus_codes()
        logger.info(f"关注标的 {len(codes)} 只: {codes[:8]}")
        if not periods or periods == ["m30", "m60"]:
            periods = ["m1", "m5", "m30"]     # 关注标的上补分钟精度
    elif args.codes:
        codes = [x.strip() for x in
                 Path(args.codes).read_text(encoding="utf-8").splitlines() if x.strip()]
    elif args.all:
        c = get_conn()
        codes = [r[0] for r in c.execute(
            "SELECT id FROM stock WHERE is_st=0 AND is_suspend=0 "
            "AND float_mv BETWEEN 2e9 AND 2e11").fetchall()]
        logger.info(f"全市场 {len(codes)} 只（市值 20亿~2000亿）")
    else:
        ap.error("需指定 --all / --codes / --focus-only 之一")

    if args.limit:
        codes = codes[: args.limit]

    logger.info(f"开始同步：{len(codes)} 只 × {periods} × count={args.count}")
    for p in periods:
        logger.info(f"  {p} 覆盖率约 {COVERAGE_DAYS.get(p, '?')} 个交易日")
    stats = sync_minutes(codes, periods, args.count, as_of=as_of)

    # ---- 结果核对
    con = duck.get_ro_conn()
    print()
    print("=" * 70)
    print("分钟线同步结果")
    print("=" * 70)
    for row in con.execute(
            """SELECT period, count(*) n, count(DISTINCT code) c,
                      min(ts) lo, max(ts) hi FROM kline_min
               GROUP BY period ORDER BY 1""").fetchall():
        per, n, c_, lo, hi = row
        print(f"  {per:>4s}: {n:>9,} 行 / {c_:>5d} 只  {str(lo)[:16]} ~ {str(hi)[:16]}")
    print()
    print(f"  库内总行数: {duck.table_counts()['kline_min']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())