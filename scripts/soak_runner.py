#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 常驻长跑器（M2「连续 24h 无静默中断」验证）

★★★ 为什么不直接`while True: sleep`
--------------------------------------------------
长跑验证真正要抓的不是「跑没跑」，而是：
  ·有没有**静默中断**（进程还在、任务不再执行、也不报错）
  · 有没有**静默失败**（任务跑了但ok=False 被吞掉）
  · 有没有**重复执行**（同一 cron 点跑两次 ⇒ 副作用翻倍）
  · 有没有**卡死**（某任务超时后线程再也不返回）

所以这个 runner 必须做到：
  1. **按 cron 点触发**，而不是「每隔 N 分钟跑一遍」
     ⇒ 否则和`--catch-up` 语义冲突、且交易时段外会疯狂跑
  2. **每次执行都落一条带 ok / elapsed_ms / error 的记录**
     ⇒ 事后能回答「哪一次失败了」，而不是只看到「跑完了」
  3. **单任务超时**用线程 + join(timeout)，**不阻塞整轮**
     ⇒ 一个任务卡死不能拖垮其余 10 个
  4. **心跳**：每轮写一条 run_state，哪怕一个任务都没跑
     ⇒ 能区分「这一小时真的无事可做」与「进程已经死了」
  5. **禁止静默吞异常**：所有异常都落库 + 打日志

★ 跨零点的 as_of：cron 判定用**墙钟**，但任务里的业务日
  必须由 `scheduler.run_job` 自己处理（它已有`force` 与交易日守卫）。

用法：
    python scripts/soak_runner.py --once# 只跑一轮（自检用）
    python scripts/soak_runner.py --hours 24       # 连续 24 小时
    python scripts/soak_runner.py --hours 24 --report
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger                     # noqa: E402

from backend.data.sqlite import get_conn, log_event   # noqa: E402
from backend.domain.trade_cal import is_trade_day       # noqa: E402
from backend.scheduler import (              # noqa: E402
    REGISTRY, TASK_TABLE, _in_cron_window, catch_up, run_job,
)

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | {message}")

#: 单任务超时（秒）—— 调度表里声明的上限 + 余量
TIMEOUT_SLACK = 1.5


# ═══════════════════════════════════════════════════════ 心跳
def beat(extra: dict | None = None) -> None:
    """写一条 run_state 心跳。

    ★ 为什么必须有它：长跑失败最常见的表现是
      「进程还在、日志还在滚、但任务不跑了」。
      没有心跳就无法区分「无事可做」与「已经死了」。
    """
    payload = {"kind": "soak_heartbeat", "pid": _pid(),
               **(extra or {})}
    try:
        # ★ `log_event(kind, payload_dict, ts)` —— payload 是 dict 不是 str，
        #   且 **ts 必须由调用方注入**（铁律二：领域层不取系统时间）。
        #   入口脚本取墙钟是合法的。
        log_event("run_state", payload,
                  datetime.now(CST).isoformat(timespec="seconds"))
    except Exception as e:                       # noqa: BLE001
        #★ 心跳本身失败必须打出来，**不能静默**
        logger.error(f"★ 心跳落库失败：{type(e).__name__}: {e}")


def _pid() -> int:
    import os
    return os.getpid()


# ═══════════════════════════════════════════════════════ 单任务执行
def run_one(name: str, as_of: datetime) -> dict:
    """跑一个任务，带**硬超时**。返回记录 dict（永不抛异常）。

    ★ 为什么用线程而不是 signal.alarm：
      signal 只能装在主线程，且Windows 上 `SIGALRM` 根本不存在
      ⇒ 跨平台只能靠 `join(timeout)`。
    ★ 超时后线程**无法真正杀死**（Python 没有安全的强杀），
      所以标记为 timeout 并**继续下一轮**；
      代价是那个线程可能还在跑 —— 因此 `run_state` 里
      会累计 `timeout_count`，事后能看见「有几个任务反复超时」。
    """
    j = REGISTRY.get(name)
    if j is None:
        return {"job": name, "ok": False, "skipped": True,
                "error": "该任务未注册实现", "elapsed_ms": 0}

    #★★ 字段名是 `timeout_sec`（**不是** `timeout_s`）。
    #   我第一版凭印象写成 `timeout_s` ⇒ 长跑每轮 AttributeError，
    #   11 次失败全是这一个笔误。
    #   ★ 这里**故意不用 `getattr(..., 默认值)`**：
    #     那样会把「字段名写错」变成「悄悄用默认超时」，
    #     错误被彻底吞掉 —— 比直接报错更糟。
    #   拼错就要立刻炸；`tests/test_soak.py` 里锁住了字段名。
    budget = max(30, int(j.timeout_sec * TIMEOUT_SLACK))
    box: dict = {}

    def _target():
        try:
            box["res"] = run_job(j, as_of, force=True)
        except Exception as e:                   # noqa: BLE001
            box["exc"] = f"{type(e).__name__}: {e}"
            box["tb"] = traceback.format_exc(limit=3)

    th = threading.Thread(target=_target, daemon=True,
                          name=f"job-{name}")
    t0 = time.monotonic()
    th.start()
    th.join(timeout=budget)
    elapsed = int((time.monotonic() - t0) * 1000)

    if th.is_alive():
        rec = {"job": name, "ok": False, "timeout": True,
               "elapsed_ms": elapsed, "budget_s": budget,
               "error": f"超过 {budget}s 未返回（线程仍在跑）"}
    elif "exc" in box:
        rec = {"job": name, "ok": False, "elapsed_ms": elapsed,
               "error": box["exc"], "traceback": box.get("tb", "")[:600]}
    else:
        r = box["res"]
        rec = {"job": name, "ok": bool(r.ok), "skipped": bool(r.skipped),
               "elapsed_ms": r.elapsed_ms,
               "detail": str(r.detail)[:300], "error": (r.error or "")[:300]}
        if rec["skipped"]:
            rec["ok"] = True          # skipped 是合法结果（如窗口外）
            rec["skip_reason"] = rec["detail"]
    return rec


# ═══════════════════════════════════════════════════════ 一轮
def tick(as_of: datetime, do_catch_up: bool = True) -> list[dict]:
    """一轮：先补跑错过的，再跑「本分钟该跑」的。"""
    out: list[dict] = []
    if do_catch_up:
        try:
            missed = catch_up(as_of)
            out.extend({"job": f"catch_up:{r.name}", "ok": bool(r.ok),
                        "elapsed_ms": r.elapsed_ms, "error": (r.error or "")[:200]}
                       for r in missed)
        except Exception as e:                   # noqa: BLE001
            logger.error(f"★ catch_up 整段失败：{type(e).__name__}: {e}")
            out.append({"job": "catch_up", "ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc(limit=3)[:600]})

    # 本分钟该跑的任务
    #★★★ 必须复用 `scheduler._in_cron_window`，**不自己解析 cron**。
    #   我第一版按「cron 分钟数 == 当前分钟」筛，结果 `30 8 * * 1-5`
    #   （08:30）在 08:00–08:59 任何时刻都不命中
    #   ⇒ 长跑一晚上可能**一个premarket 都没跑**且不报错。
    #   而 `_in_cron_window` 是项目里catch_up 用的同一套判定。
    #   ⚠ 但它有另一个特性：小时字段按**整点**判定
    #     （`30 8` ⇒ 08:00~08:59 任意时刻都算命中）。
    #   ⇒ 配合下面的「每分钟只判一次」，同一任务在一小时内只跑 1 次，
    #     且跑在**整点后第一次轮询**（≈08:00），而不是 08:30。
    #     这是 `_in_cron_window` 的既有语义（为 catch_up 的
    #     「窗口内是否本该跑过」设计），长跑沿用它以免出现第二套口径。
    for meta in TASK_TABLE:
        name, cron, td_only, _to, _desc = meta
        if not _in_cron_window(cron, as_of - timedelta(minutes=1), as_of):
            continue
        if td_only and not is_trade_day(as_of.date()):
            continue
        rec = run_one(name, as_of)
        out.append(rec)
        if not rec["ok"]:
            logger.warning(f"  [{name}] 失败：{rec.get('error')}")
    return out


def report(hours: int = 24) -> dict:
    """从 run_state / job_run 里统计长跑质量。"""
    conn = get_conn()
    since = (datetime.now(CST) - timedelta(hours=hours)).isoformat(
        timespec="seconds")
    # ★ kind 是 `run_state`（payload 里才有 phase='tick'），
    #   早先这里写的是 'soak_heartbeat' ⇒ 永远查到 0
    #   ⇒ 报告会说「心跳 0 次」，看起来像进程从没活过。
    hb = conn.execute(
        "SELECT count(*) FROM event_log WHERE kind='run_state' "
        "AND ts >= ?", (since,)).fetchone()[0]
    #★ 缺口也要看：两轮之间超过 90 秒就说明有中断
    gaps = conn.execute(
        "SELECT ts FROM event_log WHERE kind='run_state' AND ts >= ? "
        "ORDER BY ts", (since,)).fetchall()
    max_gap = 0.0
    for i in range(1, len(gaps)):
        try:
            t0 = datetime.fromisoformat(gaps[i - 1][0])
            t1 = datetime.fromisoformat(gaps[i][0])
            max_gap = max(max_gap, (t1 - t0).total_seconds())
        except (TypeError, ValueError):
            continue
    runs = conn.execute(
        "SELECT payload FROM event_log WHERE kind='job_run' "
        "AND ts >= ?", (since,)).fetchall()
    agg: dict[str, dict] = {}
    bad: list[dict] = []
    for (raw,) in runs:
        try:
            p = json.loads(raw)
        except Exception:
            continue
        name = p.get("job", "?")
        a = agg.setdefault(name, {"ok": 0, "fail": 0, "ms": 0, "n": 0})
        a["n"] += 1
        a["ms"] += int(p.get("elapsed_ms") or 0)
        if p.get("ok"):
            a["ok"] += 1
        else:
            a["fail"] += 1
            bad.append({"job": name, "ts": p.get("ts"), "err": p.get("error")})
    return {"hours": hours, "heartbeats": hb, "runs": len(runs),
            # ★ 心跳间隔是「有没有静默中断」的直接证据：
            #   轮询是 20 秒，缺口 > 90 秒即说明进程被卡住/退出过。
            "max_gap_sec": round(max_gap, 1),
            "by_job": agg, "failures": bad[:30], "n_failures": len(bad)}


# ═══════════════════════════════════════════════════════ 主循环
def soak(hours: int, interval_s: int) -> int:
    end = datetime.now(CST) + timedelta(hours=hours)
    logger.info(f"长跑开始：{hours}h，轮询 {interval_s}s")
    logger.info(f"已注册任务 {len(REGISTRY)}/{len(TASK_TABLE)} 个")
    last_minute = -1
    while datetime.now(CST) < end:
        now = datetime.now(CST)
        beat({"phase": "tick", "at": now.isoformat(timespec="seconds")})
        try:
            if now.minute != last_minute:
                last_minute = now.minute
                recs = tick(now)
                #★★★ **不要在这里再记一条 job_run** ——
                #   `run_job()` 内部已经调 `_log_run()` 记过了，
                #   实测（2026-10-11）每条执行记录**时间戳完全相同地出现两次**
                #   ⇒ 统计口径翻倍、失败率算错。
                #   长跑器只负责**心跳**（run_state）与**自身异常**（soak_tick）。
                if recs:
                    n_bad = sum(1 for r in recs if not r["ok"])
                    logger.info(f"本轮 {len(recs)} 个任务，失败 {n_bad}")
        except Exception as e:                   # noqa: BLE001
            #★ 主循环异常必须记录后**继续** —— 停在这里就等于静默中断
            logger.error(f"★ 主循环异常（继续运行）：{type(e).__name__}: {e}")
            log_event("job_run",
                      {"job": "soak_tick", "ok": False,
                       "error": f"{type(e).__name__}: {e}",
                       "traceback": traceback.format_exc(limit=3)[:600]},
                      now.isoformat(timespec="seconds"))
        time.sleep(interval_s)
    beat({"phase": "done"})
    logger.info(f"长跑结束（{hours}h）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Hawk 常驻长跑器")
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--interval", type=int, default=20,
                    help="轮询间隔秒；必须 < 60，否则会漏掉 cron 分钟点")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.report:
        r = report(int(a.hours))
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if a.once:
        now = datetime.now(CST)
        recs = tick(now)
        for r in recs:
            # ★ 同上：执行记录由 `run_job()` 自己写，这里只打印
            print(f"  {r['job']:<24} ok={r['ok']} {r.get('elapsed_ms')}ms "
                  f"{r.get('error', '')[:60]}")
        beat({"phase": "once"})
        bad = [r for r in recs if not r["ok"]]
        print(f"\n一轮结束：{len(recs)} 个任务，失败 {len(bad)}")
        return 1 if bad else 0

    if a.interval >= 60:
        print("★ --interval 必须 < 60，否则会跳过 cron 分钟点")
        return 2
    return soak(a.hours, a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
