# -*- coding: utf-8 -*-
"""在不打扰同步进程的前提下，测量分钟线同步进度。

★ 为什么需要这个工具：
  DuckDB 是**单写多读**引擎，同步进程持有写锁时：
    · `duckdb.connect(path)`         → 抛 IOException（被占用）
    · `cp hawk.duckdb /tmp/`         → Device or resource busy
    · `open('hawk.duckdb.wal','rb')` → PermissionError
    · PowerShell / WMI 查进程        → 沙箱拦截
  ⇒ **唯一能用的三个信号**：文件字节数、未被锁的 SQLite、以及实测换算系数。

★ 换算依据（都是实测，不是猜的）：
  - `BYTES_PER_ROW = 81.8` —— 单独建库写入 48,200 行真实结构数据后测得
    （50 只 × m30/m60 × 482 根），checkpoint 后净存储 3,944,448 字节。
    若是拍脑袋的数，进度就会算错几个百分点。
  - `ROWS_PER_CODE = 964` —— 实测腾讯 m30 与 m60 各返 482 根（`count=720`
    也只给 482，这是接口硬限制，见 sync_minutes.py 顶部说明）。

★ 局限（必须说清，不能让人误读）：
  1. 这是**估算**，误差来自「每行字节数」在真实数据与构造数据间的差异
     （真实浮点数分布更散，压缩率略差 ⇒ 实际进度可能略低于此值）。
  2. 基线 `BASE_DB_BYTES` 是同步启动时的文件大小，**只能手工维护一次**。
     若进程重启过，需重新校准，否则进度会算成负数或超过 100%。
  3. ★★ **checkpoint 会让「DB+WAL 之和」短暂下降**（实测 -1.78%），
     因为 WAL 里是未压缩行组，checkpoint 时压缩写入主文件并清空 WAL。
     所以 `rate_per_hour` 在跨越 checkpoint 的窗口里返回 None，
     **不可用单点差值判断速度**——应连续采样多次取中位。
  4. 若采样正好落在 checkpoint 瞬间，进度会偏低；等下一个采样点即可。

运行：
    python scripts/minute_progress.py            # 单次估算
    python scripts/minute_progress.py --watch 60 # 每60 秒刷新，直到进程结束
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ★ 实测系数（见模块 docstring）
BYTES_PER_ROW = 81.8
ROWS_PER_CODE = 964

# ★ 同步启动时 duckdb 主文件的大小。改一次即可，进程重启后需重新校准。
BASE_DB_BYTES = 228_572_160

DB = _ROOT / "data" / "hawk.duckdb"
WAL = _ROOT / "data" / "hawk.duckdb.wal"
SQLITE = _ROOT / "data" / "hawk.sqlite"

# ★ 与 sync_minutes.py --all 完全一致的选股口径。
#   放在 SQLite 里（不被 DuckDB 锁阻塞），所以同步进行中也能查。
TARGET_SQL = (
    "SELECT id FROM stock "
    "WHERE is_st=0 AND is_suspend=0 AND float_mv BETWEEN 2e9 AND 2e11"
)


def target_count() -> int:
    """目标标的数。走 SQLite，避开 DuckDB 写锁。"""
    if not SQLITE.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
        try:
            return conn.execute(f"SELECT count(*) FROM ({TARGET_SQL})").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def db_bytes() -> tuple[int, int]:
    """返回 (主文件字节, WAL 字节)。取不到时返回 (-1, -1) 而非 0。"""
    def sz(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return -1
    return sz(DB), sz(WAL)


def is_running() -> bool:
    """进程是否还在写库。

    ★ 只用 WAL 的 mtime 判：WAL mtime 新 ⇒ 刚写过 ⇒ 活跃。
      查进程列表在沙箱里不可靠（PowerShell/WMI 被拦），
      而文件 mtime 在任何环境下都能读到。
    """
    try:
        return (time.time() - WAL.stat().st_mtime) < 120
    except OSError:
        return False


def sample_progress() -> dict:
    """采一次样，返回进度快照。"""
    db, wal = db_bytes()
    total = target_count()
    out = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db_bytes": db,
        "wal_bytes": wal,
        "total_bytes": (db + wal) if db >= 0 and wal >= 0 else -1,
        "target_codes": total,
        "running": is_running(),
    }
    if db < 0 or total == 0:
        out["error"] = "无法读取库文件或目标名单"
        return out

    added = max(0, db - BASE_DB_BYTES) + max(0, wal)
    rows = added / BYTES_PER_ROW
    codes = rows / ROWS_PER_CODE
    out["codes_done"] = int(codes)
    out["rows_done"] = int(rows)
    out["pct"] = min(codes / total * 100, 100.0) if total else 0.0
    out["codes_left"] = max(0, int(total - codes))
    return out


def rate_per_hour(prev: dict, cur: dict, dt: float) -> float | None:
    """两次采样间的推进速率（只/小时）。无法比较时返回 None。

    ★ 用「净字节差」（DB+WAL 之和）而非单看 WAL：
      WAL 会被 checkpoint 清空，单看它会算出负速率。
    ★★ 但 checkpoint 本身会让总量**下降**（未压缩行组 → 压缩后落盘），
      实测 2026-10-09 09:21 那次降了 1.78%。
      ⇒ 跨越 checkpoint 的窗口必须返回 None，而不是报告一个「减速」假象。
      调用方应连续采样多次、跳过 None 后取中位。
    """
    if dt <= 0:
        return None
    d_bytes = cur["total_bytes"] - prev["total_bytes"]
    if d_bytes <= 0:                     # 缩小 = 进程重启 或 跨 checkpoint
        return None
    d_codes = d_bytes / BYTES_PER_ROW / ROWS_PER_CODE
    return d_codes / dt * 3600


def fmt(r: dict, rate: float | None = None) -> str:
    if r.get("error"):
        return f"[{r['ts']}] {r['error']}"
    line = (f"[{r['ts']}] {r['codes_done']:>5,}/{r['target_codes']:,} 只 "
            f"({r['pct']:5.1f}%)  {r['rows_done']:>9,} 行  "
            f"库 {r['total_bytes']/1e6:>6.0f} MB"
            f"  {'运行中' if r['running'] else '★已停止'}")
    if rate is not None and rate > 0:
        h = r["codes_left"] / rate
        line += f"  |  {rate:,.0f} 只/小时  剩余 {h:.1f} 小时"
    return line


def main() -> int:
    ap = argparse.ArgumentParser(description="分钟线同步进度（不抢 DuckDB 锁）")
    ap.add_argument("--watch", type=int, metavar="秒",
                    help="每 N 秒刷新一次（默认单次）")
    args = ap.parse_args()

    first = sample_progress()
    print("=" * 74)
    print("分钟线同步进度 · 只读估算（换算系数实测：81.8 字节/行 × 964 行/只）")
    print("=" * 74)
    print(fmt(first))
    print()
    print(f"  目标: sync_minutes.py --all --periods m30,m60 --count 720")
    print(f"  实测: m30/m60 各 482 根/只 → 964 行/只；920xxx(北交所) 腾讯不返回 0 行")
    if first.get("pct"):
        print(f"  ⚠ 基线 BASE_DB_BYTES 需与本次进程启动时对应；"
              f"进程若重启过请重新校准")
    if not args.watch:
        return 0

    prev, prev_t = first, time.monotonic()
    rates: list[float] = []
    try:
        while True:
            time.sleep(args.watch)
            cur = sample_progress()
            now = time.monotonic()
            r = rate_per_hour(prev, cur, now - prev_t)
            # ★ 跨 checkpoint 的窗口 r 为 None —— 跳过并累计，
            #   最终取中位数：单点差值会把压缩波动当成速度变化。
            note = ""
            if r is None:
                note = "  （跨 checkpoint，本次不计速率）"
            else:
                rates.append(r)
                note = (f"  |  {r:,.0f} 只/小时  剩余 "
                        f"{cur['codes_left'] / r:.1f} 小时")
            med = (f"  [近{len(rates)}次中位 {statistics.median(rates):,.0f} 只/小时]"
                   if len(rates) >= 3 else "")
            print(fmt(cur) + note + med)
            if not cur["running"]:
                print("\n★ WAL 已 2 分钟无写入，同步可能已结束 —— "
                      "此时 DuckDB 锁释放，可直接查库核对。")
                break
            prev, prev_t = cur, now
    except KeyboardInterrupt:
        print("\n（手动中断）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
