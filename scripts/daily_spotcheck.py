"""每日随机抽查（M8 · 附录 7.1「每日随机抽查」+ 7.2 防事后诸葛亮）。

★ 为什么需要这个：
  自动统计只能告诉你「三买整体胜率 86%」，但回答不了
  「模型当时**为什么**给出这条信号，它对不对」。
  随机抽查的作用是每天人工核对若干条，把系统的推理过程暴露给人看，
  从而发现**系统自己没意识到的问题**（例如口径错了、因子权重反了）。

★ 三条硬规则：
  1. **随机而非全量**：全量看会不自觉只挑「看起来对的」，
     随机抽样才能得到无偏的准确率估计。
  2. **先冻结、后看结果**：打标必须基于「当时的推理链 + 当时的行情」，
     不允许看到 5 日收益后才决定标correct 还是 wrong。
     本脚本输出里刻意**不包含前向收益**，避免暗示。
  3. **打标只追加不覆盖**：human_label 每次变更都写 event_log，
     保留谁在什么时候标了什么，便于追溯改判。

★ 关于 level 分档（M8 三买归因结论）：
  抽查会把 strong 与 weak **分开呈现**，不混排。
  因为实测二者胜率相差 66 个百分点，混在一起看会得出
  「三买表现平平」的错误印象。

跑法：
  python scripts/daily_spotcheck.py --n 8
  python scripts/daily_spotcheck.py --date 2026-09-24   # 查历史某日
  python scripts/daily_spotcheck.py --label 12 correct  # 人工打标
  python scripts/daily_spotcheck.py --stats# 打标一致性统计
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                                   # noqa: E402

from backend.data.sqlite import get_conn, log_event         # noqa: E402
from backend.domain.review import mark_human_label          # noqa: E402

CST = timezone(timedelta(hours=8))
LABELS = ("correct", "wrong", "unclear")


@dataclass
class SpotItem:
    """一条待抽查信号及其完整推理链。"""
    signal_id: int
    code: str
    name: str
    ts: str
    kind: str
    level: str
    price: float
    confidence: float
    reason: str
    chan_snapshot: dict = field(default_factory=dict)
    market_state: str = ""
    human_label: str | None = None
    prev_label: str | None = None

    @property
    def strength_key(self) -> str:
        return f"{self.kind}/{self.level}" if self.level else self.kind

    def to_json(self) -> dict:
        return {
            "signal_id": self.signal_id, "code": self.code, "name": self.name,
            "ts": self.ts, "kind": self.kind, "level": self.level,
            "strength_key": self.strength_key, "price": self.price,
            "confidence": self.confidence, "reason": self.reason,
            "chan_snapshot": self.chan_snapshot,
            "market_state": self.market_state,
            "human_label": self.human_label,
        }


def _load_snapshot(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"_parse_error": "chan_snapshot 不是合法 JSON"}


def pick_random(conn, day: str | None, n: int, seed: int,
                unlabeled_only: bool = True) -> list[SpotItem]:
    """随机抽取当日信号。

    ★ 用 SQL 的随机排序在**全量**里抽样，而不是先 LIMIT 再随机 ——
      后者等价于「只从最早的 200 条里抽」，会造成选择偏差。
    """
    rng = random.Random(seed)
    if day:
        rows = conn.execute(
            """SELECT id, code, name, ts, kind, level, price, confidence,
                      reason, chan_snapshot, market_state, human_label
               FROM signal WHERE ts = ?""", [day]).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, code, name, ts, kind, level, price, confidence,
                      reason, chan_snapshot, market_state, human_label
               FROM signal ORDER BY ts DESC LIMIT ?""", [n * 20]).fetchall()

    if unlabeled_only:
        unlabeled = [r for r in rows if not r[11]]
        rows = unlabeled or rows

    # 先在全量里随机取样，再截到 n
    picked = rng.sample(rows, min(n, len(rows)))
    out = []
    for r in picked:
        out.append(SpotItem(
            signal_id=r[0], code=r[1], name=r[2], ts=r[3], kind=r[4],
            level=r[5] or "", price=float(r[6] or 0),
            confidence=float(r[7] or 0), reason=r[8] or "",
            chan_snapshot=_load_snapshot(r[9]), market_state=r[10] or "",
            human_label=r[11],
        ))
    return out


def render(items: list[SpotItem]) -> str:
    """渲染成便于人工核对的纯文本卡片。"""
    lines = ["", "=" * 78,
             "每日随机抽查 · 推理链",
             "=" * 78,
             "★ 请**先判断信号是否成立**（看结构与理由），再打标。",
             "  不要去看后续涨跌 —— 那是结果，不是理由是否成立。",
             "  打标： correct=信号成立/合理 | wrong=不成立 | unclear=数据不足无法判断",
             "=" * 78]
    for it in items:
        snap = it.chan_snapshot or {}
        lines += [
            "",
            f"【#{it.signal_id}】{it.code} {it.name}  {it.ts}",
            f"  信号类型: {it.strength_key}   置信度: {it.confidence:.2f}"
            f"   报价: {it.price:.2f}",
            f"  判定理由: {it.reason}",
        ]
        if snap and "_parse_error" not in snap:
            lines.append("  当时结构快照:")
            lines.append(f"    K线 {snap.get('bars_used', '?')} 根 → "
                         f"笔 {snap.get('bi_count', '?')} 段 → "
                         f"中枢 {snap.get('zhongshu_count', '?')} 个")
            if snap.get("last_zhongshu"):
                zs = snap["last_zhongshu"]
                lines.append(f"    最后中枢: {zs.get('low')} ~ {zs.get('high')}"
                             f"（{zs.get('start_date')} ~ {zs.get('end_date')}）")
            lines.append(f"    确认背驰 {snap.get('beichi_count', '?')} 个"
                         f" | 距今 {snap.get('days_ago', '?')} 天"
                         f" | 引擎 {snap.get('engine', '?')}")
        elif snap:
            lines.append(f"  ⚠️ 快照异常: {snap.get('_parse_error')}")
        if it.market_state:
            lines.append(f"  当时市场状态: {it.market_state}")
        if it.human_label:
            lines.append(f"  已有打标: {it.human_label}")
    lines += ["", "=" * 78, ""]
    return "\n".join(lines)


def label_stats(conn) -> dict:
    """打标结果统计：人工判断 vs 实际结果的一致性。

    ★ 这个对照表是最有价值的输出：
      若人工标correct 的信号实际是亏的，说明**系统的推理与结果脱节**，
      需要查因子权重或口径，而不是继续调参数。
    """
    rows = conn.execute(
        """SELECT id, kind, level, human_label, eval_json FROM signal
           WHERE human_label IS NOT NULL""").fetchall()
    if not rows:
        return {"labeled": 0, "note": "尚无人工打标"}

    tab: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "hit": 0, "ret": [], "kinds": defaultdict(int)})
    for _id, kind, level, label, ej in rows:
        if not ej:
            continue
        try:
            h = json.loads(ej).get("horizons", {}).get("5")
        except (json.JSONDecodeError, TypeError):
            continue
        if not h or h.get("ret_pct") is None:
            continue
        key = f"{kind}/{level}" if level else kind
        cell = tab[f"{key}|{label}"]
        cell["n"] += 1
        cell["hit"] += 1 if h.get("hit") else 0
        cell["ret"].append(h["ret_pct"])
        cell["kinds"][label] += 1

    out = {}
    for key, cell in sorted(tab.items()):
        ret = cell["ret"]
        out[key] = {
            "n": cell["n"],
            "hit_rate": round(cell["hit"] / cell["n"], 3) if cell["n"] else None,
            "avg_ret": round(sum(ret) / len(ret), 2) if ret else None,
        }
    return {"labeled": len(rows), "detail": out}


def main() -> int:
    ap = argparse.ArgumentParser(description="每日随机抽查与人工打标")
    ap.add_argument("--date", help="抽查日期 YYYY-MM-DD，默认最近信号日")
    ap.add_argument("--n", type=int, default=8, help="抽查条数")
    ap.add_argument("--seed", type=int, help="随机种子（不传则随机，用于正式抽查）")
    ap.add_argument("--label", type=int, help="对指定 signal_id 打标")
    ap.add_argument("--label-value", choices=LABELS, help="打标值")
    ap.add_argument("--stats", action="store_true", help="打标一致性统计")
    ap.add_argument("--all-labeled", action="store_true",
                    help="抽已打过标的（复核用）")
    ap.add_argument("--json", help="输出到文件")
    args = ap.parse_args()

    conn = get_conn()
    as_of = datetime.now(CST)

    # ---- 打标
    if args.label is not None:
        if not args.label_value:
            ap.error("--label 需配合 --label-value")
        before = conn.execute(
            "SELECT human_label FROM signal WHERE id = ?",
            [args.label]).fetchone()
        n = mark_human_label(args.label, args.label_value, as_of)
        if n == 0:
            logger.error(f"signal_id={args.label} 不存在")
            return 1
        print(f"已打标 #{args.label}: "
              f"{before[0] if before and before[0] else '(空)'} → {args.label_value}")
        print("（变更已记入 event_log，可追溯改判）")
        return 0

    # ---- 统计
    if args.stats:
        st = label_stats(conn)
        print()
        print("=" * 78)
        print("人工打标 vs 实际结果")
        print("=" * 78)
        if st.get("labeled", 0) == 0:
            print(f"  {st['note']}")
            return 0
        print(f"  已打标 {st['labeled']} 条")
        if not st.get("detail"):
            print("  （其中可评估的为 0 条）")
            print("  原因：eval_json 为空 —— 这些信号还没走满 5 个交易日，")
            print("        或尚未运行 scripts/run_review.py 做回溯评估。")
            print("  打标本身已记录，等评估完成后重新运行 --stats 即可对照。")
            return 0
        print(f"  {'信号/档位/打标':>28s}{'样本':>6s}{'实际命中':>10s}{'平均收益':>10s}")
        for key, v in st["detail"].items():
            hr = f"{v['hit_rate']:.1%}" if v["hit_rate"] is not None else "—"
            ar = f"{v['avg_ret']:+.2f}%" if v["avg_ret"] is not None else "—"
            print(f"  {key:>28s}{v['n']:>6d}{hr:>10s}{ar:>10s}")
        print()
        print("  ★ 标 correct 却实际亏损 = 推理与结果脱节，应查因子/口径，而非调参数。")
        return 0

    # ---- 抽查
    seed = args.seed if args.seed is not None else random.randrange(10 ** 6)
    items = pick_random(conn, args.date, args.n, seed,
                        unlabeled_only=not args.all_labeled)
    if not items:
        logger.info("无信号可抽查")
        return 0

    print(render(items))
    print(f"本次随机种子 seed={seed}（可复现同一批抽样）")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"seed": seed, "as_of": as_of.isoformat(),
                        "items": [i.to_json() for i in items]},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"已写出 {out}")

    log_event("spotcheck", {"seed": seed, "n": len(items),
                            "signal_ids": [i.signal_id for i in items]},
              as_of.isoformat(timespec="seconds"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())