# -*- coding: utf-8 -*-
"""把 creator-corpus 语料包适配成 corpus_audit 能吃的rows，然后跑体检。

★ 三层，且只有 L3 是闸门
──────────────────────────────────────────────────────────────
L1 evidence 单行quote   —— 平均 10.1 字的字幕行（"或者是半仓"）。
   ★ 对它跑三要素判定等于用错粒度：字幕行是**显示单元**，不是**语义单元**。
     实测 L1 rule_bearing=0%，其中 fin_ratio 仅 6.7%（股票博主口播只有 6.7%
     "像财经"？不可能）—— 这个 0% 是粒度artifact，不是语料质量结论。
L2 candidate 摘要      —— 包方自己写的规则结论，且全部unsupported。
   这是**结论不是语料**，拿它算 rule_bearing 是循环论证。仅交叉验证。
L3 缝合窗口            —— ★ 闸门。把时间邻近的字幕行缝成句子再判定。
   这是口播语料唯一合理的判定单元。

先例（别重犯）：上一轮对 129 条抖音标题跑体检得 rule_bearing=0%，
我据此断言"语料没有方法论"。这次换语料后第一件事是怀疑闸门而不是相信数字——
`fin_ratio=6.7%` 就是那个本该立刻暴露的口径错误。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.domain.corpus_audit import (  # noqa: E402
    audit_corpus, audit_text, verdict,
)

PKG = (Path(__file__).resolve().parent.parent / "data" / "creator_corpus"
       / "Douyin-Corpus-20261010")
OUT = Path(__file__).resolve().parent.parent / "data" / "creator_corpus_audit.json"

# 缝合参数：相邻字幕间隔超过 GAP 秒就断句（标点也算强断点）
GAP = 6.0
MAX_CHARS = 160          # 单窗口上限，防止整段视频缝成一条
BREAK = re.compile(r"[。！？；!?;]")


def load_jsonl() -> list[dict]:
    return [json.loads(l) for l in
            (PKG / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _sec(e: dict) -> float:
    s = e.get("seconds")
    if isinstance(s, (int, float)):
        return float(s)
    if isinstance(s, str) and ":" in s:
        try:
            m, rest = s.split(":", 1)
            return float(m) * 60 + float(rest)
        except ValueError:
            return 0.0
    return 0.0


def stitch(evidence: list[dict], gap: float = GAP,
           max_chars: int = MAX_CHARS) -> list[str]:
    """按时间邻近把字幕行缝成窗口。返回文本列表。"""
    items = [e for e in evidence if (e.get("quote") or "").strip()]
    items.sort(key=_sec)
    out: list[str] = []
    buf: list[str] = []
    prev_t: float | None = None
    for e in items:
        q = e["quote"].strip()
        t = _sec(e)
        cut = False
        if prev_t is not None:
            cut = (t - prev_t) > gap or len("".join(buf)) >= max_chars \
                or bool(BREAK.search("".join(buf)[-3:]))
        if cut and buf:
            out.append("".join(buf))
            buf = []
        buf.append(q)
        prev_t = t
    if buf:
        out.append("".join(buf))
    return out


def rows_from(docs: list[dict], level: str,
              gap: float | None = None, mx: int | None = None) -> list[dict]:
    rows = []
    for d in docs:
        cid = d.get("creator")
        vid = d.get("video_id")
        if level == "L1":
            for e in d.get("evidence") or []:
                q = (e.get("quote") or "").strip()
                if q:
                    rows.append({"content": q, "pub_time": d.get("title", ""),
                                 "strategy_id": d.get("strategy_id", ""),
                                 "transcript_ref":
                                     f"{cid}/{vid}#{e.get('id')}@{_sec(e):.1f}s"})
        elif level == "L2":
            for c in d.get("candidates") or []:
                s = " ".join(str(c.get(k) or "") for k in
                             ("summary", "trigger", "invalidation", "basis"))
                if s.strip():
                    rows.append({"content": s, "pub_time": d.get("title", ""),
                                 "strategy_id": d.get("strategy_id", ""),
                                 "transcript_ref": f"{cid}#{c.get('id')}"})
        else:  # L3
            g = GAP if gap is None else gap
            m = MAX_CHARS if mx is None else mx
            for i, w in enumerate(stitch(d.get("evidence") or [], g, m)):
                rows.append({"content": w, "pub_time": d.get("title", ""),
                             "strategy_id": d.get("strategy_id", ""),
                             "transcript_ref": f"{cid}/{vid}#w{i}"})
    return rows


def show(tag: str, r: dict) -> tuple[bool, str]:
    v, why = verdict(r)
    print(f"\n=== {tag}（{'闸门' if tag.startswith('L3') else '仅参考'}）===")
    for k in ("total", "rule_bearing", "fin_ratio", "has_threshold_ratio",
              "has_action_ratio", "noise_ratio", "dup_ratio"):
        print(f"  {k}: {r[k]}")
    print(f"  by_source: {r['by_source']}")
    print(f"  裁决: {'通过' if v else '不通过'} —— {why}")
    return v, why


def main() -> None:
    docs = load_jsonl()
    print(f"作品 {len(docs)} | evidence "
          f"{sum(len(d.get('evidence') or []) for d in docs)} | candidate "
          f"{sum(len(d.get('candidates') or []) for d in docs)}")
    for k in ("fully_reviewed", "complete_audio_transcript", "listening_review"):
        print(f"  {k}: {sum(1 for d in docs if d.get(k))}/{len(docs)}")

    r1 = audit_corpus(rows_from(docs, "L1"))
    show("L1 字幕单行", r1)
    r2 = audit_corpus(rows_from(docs, "L2"))
    show("L2 包方候选摘要", r2)

    print("\n=== L3 缝合窗口口径敏感度（同一批语料，只改缝合参数）===")
    # ★「长窗口命中、短窗口不命中」是缝合参数造成的artifact，不是语料结论。
    #   只报一个窗口尺寸的数字，等于在参数网格里挑好看的那个（老教训）。
    grid = {}
    for gap, mx in ((6.0, 160), (10.0, 300), (15.0, 600), (30.0, 2000)):
        rg = audit_corpus(rows_from(docs, "L3", gap, mx))
        grid[f"gap={gap:g},max={mx}"] = {
            "total": rg["total"], "rule_bearing": rg["rule_bearing"],
            "fin_ratio": rg["fin_ratio"],
            "has_threshold_ratio": rg["has_threshold_ratio"],
            "has_action_ratio": rg["has_action_ratio"]}
        print(f"  gap={gap:<4g} max={mx:<5d} n={rg['total']:<4d} "
              f"rule_bearing={rg['rule_bearing']:>5}%  "
              f"fin={rg['fin_ratio']}% thr={rg['has_threshold_ratio']}% "
              f"act={rg['has_action_ratio']}%")
    # 回到选定口径
    r3 = audit_corpus(rows_from(docs, "L3", 10.0, 300))
    v3, why3 = show("L3 缝合窗口 (gap=10s,max=300)", r3)

    rows3 = rows_from(docs, "L3", 10.0, 300)
    lens = sorted(len(r["content"]) for r in rows3)
    print(f"\nL3 窗口长度: 中位 {lens[len(lens)//2]} / "
          f"p90 {lens[int(len(lens)*0.9)]} / max {lens[-1]}")
    ok = [r for r in rows3 if audit_text(r["content"]).actionable]
    print(f"\n=== L3 可支撑规则窗口 {len(ok)}/{len(rows3)} "
          f"= {r3['rule_bearing']}% ===")
    for r in ok[:25]:
        print(f"  [{r['transcript_ref']}] {r['content']}")

    OUT.write_text(json.dumps({
        "L1_subtitle_line": {k: v for k, v in r1.items() if k != "items"},
        "L2_candidate_summary": {k: v for k, v in r2.items() if k != "items"},
        "L3_stitched_window": {k: v for k, v in r3.items() if k != "items"},
        "gate": "L3", "verdict_L3": v3, "why_L3": why3,
        "L3_actionable_refs": [{"ref": r["transcript_ref"],
                                "text": r["content"]} for r in ok],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    main()
