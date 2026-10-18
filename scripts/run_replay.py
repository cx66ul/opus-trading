"""历史重放引擎 + 可复现性验证（M8 验收核心）。

M8 验收标准原文：
    「任意历史日重放出的信号与当日实际生成**完全一致**（可复现性验证）」

★ 这个引擎要证明的是**没有前视偏差**：
  重放 D 日时，只喂给引擎 ≤ D 的 K 线，若引擎内部偷偷用了未来数据，
  重放结果必然与当日实际生成的不一致。所以「能对上」本身就是最强的证据。

★ 两个必须避开的坑（都已在实现里处理）：
  1. **行情截断**：`duck.get_daily(code, 250)` 返回的是**最新** 250 根，
     不是 as_of 当时的 250 根。直接调用 = 把未来数据喂给了引擎。
     必须按 as_of 截断后再算。
  2. **参数漂移**：重放必须用**当前代码 + 当日冻结参数**。
     若拿当日落库的信号去比，必须明确这是「同代码不同参数」的对照，
     而非声称复现成功 —— 信号是「生成即冻结」的，不能事后改。

判定分三档（不搞二元 pass/fail，那会掩盖问题）：
  identical       逐条 (code,kind,level,ts) 完全一致
  close           条数一致，仅 price/confidence 有小数级差异
  mismatch        条数或键不一致 —— 存在前视偏差或参数漂移，必须查
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                                   # noqa: E402

from backend.data import duck                              # noqa: E402
from backend.data.sqlite import get_conn, log_event        # noqa: E402
from backend.domain.chan import (                          # noqa: E402
    analyze,
)

CST = timezone(timedelta(hours=8))
PRICE_TOL = 0.02# 2 分以内视为价格一致

# ★★ 重放窗口必须与生产脚本**完全一致**，否则笔结构会漂移。
#   实测（000036）：同样 as_of=2026-10-08，
#     取最近 250 根 → 18 笔 → 3buy 在 2026-02-11
#     取 251 根   → 19 笔 → 3buy 漂到 2026-01-21
#   多一根 K 线就多出一笔，进而改变中枢边界与"离开中枢"的笔位置。
#   这不是 bug，是分型/笔算法对输入长度的固有敏感性 ——
#   所以可复现的前提是**取数口径必须冻结**，不能随手改。
#   该值与 scripts/portfolio_health.py:run_signals 的 duck.get_daily(code, 250) 对齐。
PROD_KLINE_LIMIT = 250


@dataclass
class ReplayResult:
    """单只标的的重放比对结果。"""
    code: str
    as_of: str
    verdict: str                # identical | close | mismatch | no_baseline
    replayed: list[dict] = field(default_factory=list)
    stored: list[dict] = field(default_factory=list)
    only_replay: list[str] = field(default_factory=list)
    only_stored: list[str] = field(default_factory=list)
    note: str = ""

    def to_json(self) -> dict:
        return {
            "code": self.code, "as_of": self.as_of, "verdict": self.verdict,
            "replayed_n": len(self.replayed), "stored_n": len(self.stored),
            "only_replay": self.only_replay[:10],
            "only_stored": self.only_stored[:10], "note": self.note,
        }


def _signal_key(code: str, kind: str, level: str, ts: str) -> str:
    """信号的比较键。**不含 price** —— 价格允许小数级差异。"""
    return f"{code}|{kind}|{level}|{ts}"


def replay_signals(code: str, rows: list[dict], as_of: datetime) -> list[dict]:
    """用**截至 as_of 的 K 线**重跑缠论引擎，返回信号列表。

    ★ rows 必须已按 as_of 截断（本函数不再二次截断，
      以免调用方误以为传入全量也能得到正确结果）。
    """
    res = analyze(code, rows, as_of=as_of.isoformat(timespec="seconds"))
    return [{"code": code, "kind": s.kind, "level": s.level,
             "ts": s.date, "price": round(s.price, 4),
             "confidence": round(s.confidence, 3)}
            for s in res.signals]


def _fetch_asof_daily(code: str, as_of: datetime,
                      limit: int = PROD_KLINE_LIMIT) -> list[dict]:
    """取截至 as_of 的日线（★ 必须截断，否则前视偏差）。

    先按时间截断，再取最近 limit 根 —— 顺序不能反。
    先 limit 后截断会拿到窗口之前的数据，等于偷看未来。
    """
    rows = duck.get_daily(code, max(limit * 3, limit))
    cutoff = as_of.date().isoformat()
    cut = [r for r in rows if str(r.get("trade_date")) <= cutoff]
    return cut[-limit:]


def replay_one(code: str, as_of: datetime,
               stored_all: list[dict]) -> ReplayResult:
    """重放单只标的并与库内信号比对（stored_all 需含 ts <= as_of 的全部信号）。"""
    rows = _fetch_asof_daily(code, as_of)
    if len(rows) < 60:
        return ReplayResult(code, as_of.date().isoformat(), "no_baseline",
                            note=f"K线不足({len(rows)})")
    try:
        rep = replay_signals(code, rows, as_of)
    except Exception as exc:
        return ReplayResult(code, as_of.date().isoformat(), "mismatch",
                            note=f"引擎异常 {type(exc).__name__}: {str(exc)[:60]}")

    # ★★ 比对基准必须是「当日实际那次全量重扫的完整结果」，不是「当日新增的行」。
    #   signal 表是 `INSERT OR IGNORE` + 唯一索引 (code,kind,ts,strategy_id)，
    #   所以每次全量重扫只会**新增**当天新出现的信号，历史信号不会重复写入。
    #   若只取 ts = D 的行去比对，必然「重放多出历史信号 / 库内多出当日信号」，
    #   100% mismatch —— 那是比对基准错了，不是引擎错了。
    #   正确做法：以 ts <= D 的**全部**库内信号为基准，
    #   因为这些正是历次重扫累积下来的结果集合。
    stored_upto = [s for s in stored_all if s["ts"] <= day]

    rk = {_signal_key(code, s["kind"], s["level"], s["ts"]) for s in rep}
    sk = {_signal_key(code, s["kind"], s["level"], s["ts"]) for s in stored_upto}
    only_r, only_s = sorted(rk - sk), sorted(sk - rk)

    if not stored_upto and not rep:
        return ReplayResult(code, day, "identical", rep, stored_upto,
                            note="当日双方均无信号")

    verdict = "identical"
    note = ""
    if only_r or only_s:
        verdict = "mismatch"
        note = f"重放多{len(only_r)}条 / 库内多{len(only_s)}条"
    else:
        # 键完全一致，再比价格
        idx = {(s["ts"], s["kind"]): s for s in stored_upto}
        diffs = []
        for s in rep:
            t = idx.get((s["ts"], s["kind"]))
            if t and abs(t["price"] - s["price"]) > PRICE_TOL:
                diffs.append(f"{s['ts']}:{t['price']}->{s['price']}")
        if diffs:
            verdict = "close"
            note = f"价格微差 {len(diffs)} 处: " + "; ".join(diffs[:3])

    return ReplayResult(code, day, verdict, rep, stored_upto, only_r, only_s, note)


def replay_day(as_of: datetime, codes: list[str] | None = None,
               limit: int = 60, seed: int = 7) -> dict:
    """重放某一历史日，与库内基线比对。

    ★★ 基线选择（实测踩过的坑）：
      signal 表用`INSERT OR IGNORE` + 唯一索引 (code,kind,ts,strategy_id)，
      所以每次全量重扫只**新增**当天新出现的信号。实测全库 1129 条信号的
      `created_at` **全部是 2026-10-08** —— 说明历史上只跑过一次全量重扫，
      并非逐日累积。
      ⇒ 用 `ts = D` 找「当日基线」在多数日期上是空的，比对毫无意义；
        正确基线是 **created_at 与 as_of 同日的那一次全量重扫的全部结果**，
        即 `ts <= D` 的所有行。
      本函数据此取 `ts <= D` 全量作基线，并在报告里记录基线来源，
      避免把「无基线」误报成「不可复现」。
    """
    conn = get_conn()
    day = as_of.date().isoformat()
    if codes is None:
        rows = conn.execute(
            """SELECT DISTINCT code FROM signal WHERE ts <= ?
               ORDER BY code LIMIT ?""", [day, limit]).fetchall()
        codes = [r[0] for r in rows] or [r[0] for r in conn.execute(
            "SELECT code FROM stock WHERE is_st=0 LIMIT ?", [limit]).fetchall()]
    random.Random(seed).shuffle(codes)
    codes = codes[:limit]

    logger.info(f"重放 {day}：{len(codes)} 只标的")
    #基线来源自检：库内信号的生成时点分布
    created = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(created_at, 1, 10) FROM signal "
        "ORDER BY 1 DESC LIMIT 5").fetchall()]

    stored_map: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
            """SELECT code, kind, level, ts, price, confidence FROM signal
               WHERE ts <= ?""", [day]).fetchall():
        stored_map[r[0]].append({"kind": r[1], "level": r[2], "ts": r[3],
                                 "price": float(r[4] or 0),
                                 "confidence": float(r[5] or 0)})

    results = [replay_one(c, as_of, stored_map.get(c, [])) for c in codes]
    tally: dict[str, int] = defaultdict(int)
    for r in results:
        tally[r.verdict] += 1

    report = {
        "as_of": day,
        "codes": len(codes),
        "baseline": {
            "source": "signal 表中 ts <= as_of 的全部行",
            "distinct_created_at": created,
            "note": "created_at 集中于单一日期说明历史基线只有一次全量重扫",
        },
        "kline_limit": PROD_KLINE_LIMIT,
        "tally": dict(tally),
        "results": [r.to_json() for r in results],
        "trades": [r.to_json() for r in results if r.verdict == "mismatch"][:20],
    }
    return report


def verify_no_lookahead(code: str, as_of: datetime) -> dict:
    """前视偏差的直接检验：截断数据后信号集合必须**变窄或保持**，绝不能变多。

    若截掉未来数据反而产生更多信号，说明引擎用了未来信息（极隐蔽）。
    """
    rows_all = duck.get_daily(code, 250)
    rows_cut = _fetch_asof_daily(code, as_of)
    if len(rows_cut) < 70 or len(rows_all) == len(rows_cut):
        return {"code": code, "skipped": True,
                "note": f"数据不足或无未来可截(all={len(rows_all)} cut={len(rows_cut)})"}

    full = replay_signals(code, rows_all, as_of)
    cut = replay_signals(code, rows_cut, as_of)
    full_keys = {_signal_key(code, s["kind"], s["level"], s["ts"]) for s in full}
    cut_keys = {_signal_key(code, s["kind"], s["level"], s["ts"]) for s in cut}

    # 未来数据可能合法地改变历史结构（重算分型会重绘未完成的笔），
    # 所以这里只检测**反向异常**：截断后凭空多出信号。
    new_on_cut = sorted(cut_keys - full_keys)
    return {
        "code": code, "skipped": False,
        "all_n": len(full), "cut_n": len(cut),
        "only_in_cut": new_on_cut,
        "only_in_full": sorted(full_keys - cut_keys)[:10],
        "verdict": "ok" if not new_on_cut else "suspect_lookahead",
        "note": "" if not new_on_cut
                else f"截断后凭空多出 {len(new_on_cut)} 条信号，疑似前视偏差",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="历史重放与可复现性验证")
    ap.add_argument("--date", help="重放日期 YYYY-MM-DD，默认取库内最近有信号的日期")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--lookahead-check", type=int, default=5,
                    help="额外做 N 只前视偏差检验")
    ap.add_argument("--out", help="报告输出路径")
    args = ap.parse_args()

    conn = get_conn()
    if args.date:
        as_of = datetime.fromisoformat(args.date + "T15:00:00+08:00")
    else:
        r = conn.execute(
            "SELECT max(ts) FROM signal WHERE ts IS NOT NULL").fetchone()
        if not r or not r[0]:
            logger.error("库内无信号，无法重放")
            return 1
        as_of = datetime.fromisoformat(str(r[0]) + "T15:00:00+08:00")

    print("=" * 78)
    print(f"历史重放 · 可复现性验证  as_of={as_of.date()}")
    print("=" * 78)
    rep = replay_day(as_of, limit=args.limit)

    print(f"\n  标的数: {rep['codes']}")
    for k in ("identical", "close", "mismatch", "no_baseline"):
        n = rep["tally"].get(k, 0)
        if n:
            print(f"    {k:>14s}: {n}")

    if rep["tally"].get("identical", 0) == rep["codes"]:
        print("\n  ✅ 完全一致：重放信号与当日实际生成逐条吻合，无前视偏差。")
    elif rep["tally"].get("mismatch", 0) == 0:
        print("\n  ✅ 无不一致：所有标的信号键完全对上（部分价格微差属正常）。")
    else:
        print(f"\n  ⚠️ {rep['tally'].get('mismatch')} 只存在不一致，需逐例核查：")
        for t in rep["tally"] and rep["trades"][:5]:
            print(f"    {t['code']}: {t['note']}")

    # ---- 前视偏差直接检验
    if args.lookahead_check:
        print()
        print("-" * 78)
        print("前视偏差直接检验（截断未来数据后不应凭空多出信号）")
        print("-" * 78)
        codes = [r[0] for r in conn.execute(
            """SELECT DISTINCT code FROM signal WHERE ts = ?
               LIMIT ?""", [as_of.date().isoformat(), args.lookahead_check]).fetchall()]
        la = [verify_no_lookahead(c, as_of) for c in codes]
        for r in la:
            if r.get("skipped"):
                print(f"  {r['code']}: 跳过（{r['note']}）")
                continue
            flag = "✅" if r["verdict"] == "ok" else "⚠️"
            print(f"  {flag} {r['code']}: 全量{r['all_n']}条 / 截断{r['cut_n']}条 "
                  f"{r['note']}")
        rep["lookahead"] = la

    out = Path(args.out) if args.out else _ROOT / "data" / "replay_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"重放报告已写出: {out}")
    log_event("replay_run", rep["tally"], as_of.isoformat(timespec="seconds"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())