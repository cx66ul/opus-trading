# -*- coding: utf-8 -*-
"""听校工作台（M10 第 ④ 步的前置）——把「要听什么、听完要答什么」固化下来。

================================ 为什么是这个形态 ==================================
「把 36 条音轨听完」是不可执行的任务：全量57 分钟，而承载决策的只有几十秒。
且ASR 已给逐字时间戳 + 逐字置信度 ⇒ 可以**机器先排序，人只听高价值片段**。

★★ 排序不能按置信度低/时长短来排，要按**决策价值**排：
   一条 ASR 置信度 0.3 的「今天天气不错」不值得听；
   一条置信度 0.95 的「亏钱效应面积大于30% 清仓」**必须**听——
   因为它是唯一能让`admit()` 通过的样本，错了整条规则就废了。
⇒ 分数 = 决策价值（含量化阈值 × 含动作 × 影响面）× 置信度**反向**加权
   （置信度只用于「提示这里可能有识别错误」，不用于决定听不听）。

========================== 三个必须由人回答的问题 ==========================
  Q1 原文是什么（含数字与单位）—— ASR 的「面积大于30%」可能是「面积大于3O%」
     或「占比大于30%」，**数字错一位规则就废**
  Q2 统计口径是什么 —— 30% 的分母是涨停家数？连板家数？还是亏钱个股占比？
     口径不同，规则在回测里就是两个东西
  Q3 是普适原则还是当次个案 —— 博主说「10日线」可能只是那天的一只票

★ 三问任一答不出 ⇒ `speech_verified`保持 False，规则不得进 `rule_library`。

用法：
    python -m scripts.build_listening_queue# 生成待听校清单
    python -m scripts.build_listening_queue --top 20   # 只看优先级前 20
    python -m scripts.build_listening_queue --cut        # 切出音频片段
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PKG = _ROOT / "data" / "creator_corpus" / "Douyin-Corpus-20261010"
RESEARCH = PKG / "data" / "creator-research"
OUT_JSON = _ROOT / "data" / "listening_queue.json"
OUT_MD = _ROOT / "data" / "listening_queue.md"
CUT_DIR = _ROOT / "data" / "listening_cuts"

# ── 决策价值特征 ──────────────────────────────────────────────────
# 量化阈值：数字 + 单位/量词。缺了它规则不可编译。
NUM = re.compile(r"(?:\d+(?:\.\d+)?)\s*(?:%|个点|倍|成仓|万|亿|成|手|cm|块)")
# 动作
ACT = re.compile(r"(买入|卖出|加仓|减仓|清仓|建仓|空仓|观望|止盈|止损|低吸|"
                 r"追高|打板|抄底|逃顶|上车|离场|进场|兑现|冻结|停止)")
# 口径依赖词：出现它就必须问 Q2（分母/基准/时点）
SCOPE = re.compile(r"(面积|占比|比例|换手|量能|成交量|成交额|市值|连板|涨停|跌停|"
                   r"亏钱效应|炸板|红盘|绿盘|分母|口径|广度|集中度)")
# 术语歧义：ASR 错一个字就完全变意思
TERMS = re.compile(r"(DC|炸板|核按钮|A字|反包|闷杀|承接|分歧|低吸|首阴|"
                   r"卡位|龙头|补涨|退潮|发酵|高标|中位)")

LOW_CONF = 0.80      # 逐字置信度低于此值视为「可能识别错」


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def load_works() -> list[dict]:
    out = []
    for creator_dir in sorted(RESEARCH.iterdir()):
        if not creator_dir.is_dir():
            continue
        for vid_dir in sorted(creator_dir.iterdir()):
            ap = vid_dir / "asr.json"
            if not ap.exists():
                continue
            asr = _load(ap)
            out.append({
                "creator": creator_dir.name,
                "video_id": vid_dir.name,
                "dir": vid_dir,
                "asr": asr,
                "audio": vid_dir / "audio.wav",
                "candidates": [],
            })
    #挂上 candidate 与 title
    jl = PKG / "corpus.jsonl"
    if jl.exists():
        for line in jl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            for w in out:
                if w["video_id"] == d.get("video_id"):
                    w["candidates"] = d.get("candidates") or []
                    w["title"] = d.get("title", "")
                    w["pub_time"] = d.get("pub_time", "")
                    w["evidence"] = d.get("evidence") or []
    return out


def score_segment(seg: dict) -> dict:
    """→ 该ASR 段的决策价值评分与听校问题。"""
    text = seg.get("text", "") or ""
    words = seg.get("words") or []
    if not words:
        return {"seg": None, "score": 0.0}
    probs = [w.get("probability", 1.0) for w in words]
    low = [w for w in words if w.get("probability", 1.0) < LOW_CONF]
    mean_p = sum(probs) / len(probs)
    low_ratio = len(low) / len(words)

    has_num = bool(NUM.search(text))
    has_act = bool(ACT.search(text))
    has_scope = bool(SCOPE.search(text))
    n_terms = len(set(TERMS.findall(text)))

    # ★ 决策价值：量化阈值与动作才是「能让规则成立」的东西。
    #   纯观点（"大盘不好"）对规则库贡献 0，不该占听校时间。
    core = (3.0 if has_num else 0.0) + (2.0 if has_act else 0.0)
    # 有候选规则直接指向本段 ⇒ 最高优先（错一段就废一条规则）
    score = core + 1.2 * n_terms + (1.0 if has_scope else 0.0)
    # 低置信是「提示」不是「门槛」：置信度低才更该听，但要标出来
    priority = score + 2.0 * low_ratio
    return {
        "seg": {"start": seg.get("start"), "end": seg.get("end"),
                "text": text},
        "score": round(score, 2),
        "priority": round(priority, 2),
        "mean_prob": round(mean_p, 3),
        "low_ratio": round(low_ratio, 3),
        "low_words": [{"w": w.get("word"), "t": w.get("start"),
                       "p": round(w.get("probability", 0), 3)}
                      for w in low][:12],
        "has_num": has_num, "has_act": has_act,
        "has_scope": has_scope, "terms": sorted(set(TERMS.findall(text))),
    }


def cut_audio(src: Path, dst: Path, t0: float, t1: float) -> tuple[bool, str]:
    """切出 [t0,t1) 片段。

    ★★ 必须显式重写 `nframes`，不能用 `getparams()`：
       `getparams()` 返回的是**原文件**的 nframes（581024），
       而 `writeframes` 只写片段长度⇒ RIFF 头声明 581024 帧、
       文件实际只有 3s 数据 ⇒ 读的时候必然 EOFError。
       这是"文件写出来了但是 0 字节/读不出"的经典成因。
    ★ 失败必须抛出来，**不许静默返回 None**（静默失败会让人以为切片已完成）。
    """
    if not src.exists():
        return False, f"音频不存在：{src}"
    pad = 0.25   # 前后各留一点余量，避免切掉首字尾字
    t0, t1 = max(0.0, t0 - pad), t1 + pad
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with wave.open(str(src), "rb") as w:
            fr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            total = w.getnframes()
            a, b = int(t0 * fr), min(total, int(t1 * fr))
            if b <= a:
                return False, f"时间轴越界：{t0:.2f}-{t1:.2f} 超出 {total/fr:.2f}s"
            w.setpos(a)
            frames = w.readframes(b - a)
        with wave.open(str(dst), "wb") as o:
            o.setnchannels(ch)
            o.setsampwidth(sw)
            o.setframerate(fr)
            o.setnframes(len(frames) // (ch * sw))   # ★ 关键：写实际帧数
            o.writeframes(frames)
        # 写完立刻回读自检 —— 落盘的产物必须当场验证，不能假定成功
        with wave.open(str(dst), "rb") as v:
            got = v.getnframes() / v.getframerate()
        want = (b - a) / fr
        if abs(got - want) > 0.05:
            return False, f"回读时长不符：写入{want:.2f}s，读回{got:.2f}s"
        return True, ""
    except (wave.Error, OSError, EOFError) as e:
        return False, f"{type(e).__name__}: {e}"


def build(do_cut: bool = False) -> list[dict]:
    works = load_works()
    tasks = []
    for w in works:
        cand_ids = {c.get("id") for c in w["candidates"]}
        ev = w.get("evidence") or []
        # evidence 的秒数 → 该秒数落在哪一段上（用于标「本段被候选引用」）
        for i, seg in enumerate(w["asr"].get("segments") or []):
            a = score_segment(seg)
            if a["seg"] is None:
                continue
            if a["score"] <= 0 and a["low_ratio"] < 0.5:
                continue      # 既无决策价值又没识别疑点 ⇒ 不占用听校时间
            # ★ 纯低置信的段只在「疑点严重」时才留：低置信是普遍现象
            #   （实测中位 18% 词都在0.8 以下），若只要low_ratio>0 就收，
            #   317 段里288 段都是它—— 队列被噪声淹没，真正要听的排不进来。
            if a["score"] <= 0 and a["low_ratio"] < 0.75:
                continue
            # ★ 无决策价值但识别疑点严重的段，也留着（让人听一遍确认没规则），
            #   不过排序沉底，混在 `priority` 里即可。
            risky = a["has_num"] or a["has_act"] or a["has_scope"] \
                or bool(a["terms"])
            t0 = a["seg"]["start"] or 0.0
            t1 = a["seg"]["end"] or (t0 + 3)
            hit_ev = [e for e in ev
                      if _sec(e) is not None and t0 <= _sec(e) <= t1]
            rec = {
                "creator": w["creator"], "video_id": w["video_id"],
                "title": w.get("title", ""),
                "pub_time": w.get("pub_time", ""),
                "seg_idx": i, "start": round(t0, 2), "end": round(t1, 2),
                "asr_text": a["seg"]["text"],
                "score": a["score"], "priority": a["priority"],
                "mean_prob": a["mean_prob"], "low_ratio": a["low_ratio"],
                "low_words": a["low_words"],
                "has_num": a["has_num"], "has_act": a["has_act"],
                "has_scope": a["has_scope"], "terms": a["terms"],
                "evidence_ids": [e.get("id") for e in hit_ev],
                "candidate_ids": [c.get("id") for c in w["candidates"]
                                  if set(c.get("evidence_refs") or [])
                                  & {e.get("id") for e in hit_ev}],
                "audio": str(w["audio"].relative_to(_ROOT)),
            }
            if do_cut:
                out = CUT_DIR / w["creator"] / f"{w['video_id']}_seg{i}.wav"
                ok, why = cut_audio(w["audio"], out, t0, t1)
                rec["cut"] = str(out.relative_to(_ROOT)) if ok else None
                rec["cut_error"] = "" if ok else why
            tasks.append(rec)
    tasks.sort(key=lambda r: -r["priority"])
    return tasks


def _sec(e: dict):
    s = e.get("seconds")
    if isinstance(s, (int, float)):
        return float(s)
    if isinstance(s, str) and ":" in s:
        try:
            m, rest = s.split(":", 1)
            return float(m) * 60 + float(rest)
        except ValueError:
            return None
    return None


def render_md(tasks: list[dict], top: int | None = None) -> str:
    L = ["# 听校工作台", "",
         f"共 **{len(tasks)}** 个片段待听校"
         f"（36 条音轨全量 57 分钟，此处只列**承载决策**的片段）。", "",
         "每条必须回答三个问题，任一答不出 ⇒ `speech_verified` 保持 False。", "",
         "| Q1 原文（含数字与单位） | Q2 统计口径/分母 | Q3 普适 or 个案 |",
         "|---|---|---|", ""]
    sel = tasks[:top] if top else tasks
    for n, t in enumerate(sel, 1):
        flag = []
        if t["has_num"]:
            flag.append("**含数值**")
        if t["has_act"]:
            flag.append("**含动作**")
        if t["has_scope"]:
            flag.append("**口径依赖**")
        L.append(f"## {n}. [{t['creator']}/{t['video_id']}] "
                 f"`seg{t['seg_idx']}` {t['start']:.1f}–{t['end']:.1f}s")
        L.append("")
        L.append(f"- 作品：{t['title']}（{t['pub_time']}）")
        L.append(f"- 决策价值 **{t['score']}** / 优先级 **{t['priority']}**"
                 f"｜{'、'.join(flag) or '无特征'}")
        L.append(f"- ASR 均置信 **{t['mean_prob']}**，低置信词占比 "
                 f"{t['low_ratio']*100:.0f}%"
                 + (f"，疑似错词：{' '.join(w['w'] for w in t['low_words'])}"
                    if t["low_words"] else ""))
        L.append(f"- ASR 原文：`{t['asr_text']}`")
        if t["evidence_ids"]:
            L.append(f"- 引用出处：`{'、'.join(t['evidence_ids'])}`"
                     f"（候选 {'、'.join(t['candidate_ids'])}）")
        if t.get("cut"):
            L.append(f"- 音频片段：`{t['cut']}`")
        else:
            L.append(f"- 原始音频：`{t['audio']}` "
                     f"（跳到 {t['start']:.1f}s）")
        L.append("")
        L.append(f"- [ ] Q1 逐字原文（含数字单位）｜"
                 f"Q2 口径/分母｜ Q3 普适 or 个案")
        L.append("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--cut", action="store_true", help="切出音频片段")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.status:
        if not OUT_JSON.exists():
            print("尚未生成，跑一次 build")
            return
        tasks = _load(OUT_JSON)["tasks"]
        import collections
        c = collections.Counter(t["creator"] for t in tasks)
        print(f"待听校 {len(tasks)} 段｜" +
              "｜".join(f"{k}:{v}" for k, v in sorted(c.items())))
        return

    tasks = build(do_cut=a.cut)
    ncut = sum(1 for t in tasks if t.get("cut"))
    errs = [t for t in tasks if a.cut and not t.get("cut")]
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(
        {"generated_from": str(PKG), "n": len(tasks), "tasks": tasks},
        ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(tasks), encoding="utf-8")

    ncand = sum(1 for t in tasks if t["candidate_ids"])
    print(f"待听校片段 **{len(tasks)}**｜其中被候选规则引用 **{ncand}**")
    print(f"含数值 {sum(1 for t in tasks if t['has_num'])}"
          f"｜含动作 {sum(1 for t in tasks if t['has_act'])}"
          f"｜口径依赖 {sum(1 for t in tasks if t['has_scope'])}"
          f"｜有识别疑点 {sum(1 for t in tasks if t['low_ratio']>0)}")
    dur = sum(t["end"] - t["start"] for t in tasks)
    print(f"总时长 {dur:.0f} 秒 = {dur/60:.1f} 分钟（全量 57.0 分钟）")
    if a.cut:
        print(f"音频切片 **{ncut}/{len(tasks)}**"
              + (f"｜★失败 {len(errs)}：{errs[0]['cut_error']}"
                 if errs else ""))
    print(f"→ {OUT_JSON}\n→ {OUT_MD}")


if __name__ == "__main__":
    main()