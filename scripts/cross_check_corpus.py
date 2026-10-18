# -*- coding: utf-8 -*-
"""ASR × OCR 双源交叉 → 数值/口径待确认清单。

================================ 为什么必须双源 ==================================
`asr.json`（听音轨）与 `ocr.json`（读画面字幕）是**两个独立信息源**：
同一个词两边都识别错/都识别对的概率，远低于单源。
实测 `asr="退潮期,全面杀爹"` 而 `ocr="退潮期全面杀跌"` —— ASR 单独不可信。

★ 但双源**不能**取代听校，只能把听校成本从5.2 分钟压到 1 分钟：
  · 两源一致        → 原文基本确定，只剩 Q2 口径（**这才是真缺口**）
  · 两源不一致      → 必须听（挂到高优先）
  · 只有 ASR（无字幕）→ 数字无第二来源，**必须听**

================== 本脚本只做「排缺口」，不做「补口径」 ==================
口径（30% 的分母是涨停家数还是亏钱个股占比）**语料里根本没有**，
任何脚本都补不出来 —— 那只能来自博主本人。
⇒ 输出是**问题清单**，不是答案。绝不填猜测值。

用法：
    python -m scripts.cross_check_corpus              # 生成待确认清单
    python -m scripts.cross_check_corpus --only gaps  # 只看口径缺口
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PKG = _ROOT / "data" / "creator_corpus" / "Douyin-Corpus-20261010"
RESEARCH = PKG / "data" / "creator-research"
QUEUE = _ROOT / "data" / "listening_queue.json"
OUT_MD = _ROOT / "data" / "scope_gaps.md"
OUT_JSON = _ROOT / "data" / "scope_gaps.json"

# ── 数值抽取：必须连单位一起抓，单位错= 规则错 =───────────────────────
NUM_UNIT = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(%|个点|个基点|倍|成仓|成|万手|手|亿|万|块|元|天|日|月)"
    r"|(\d+(?:\.\d+)?)\s*(?:到|至|-|~)\s*(\d+(?:\.\d+)?)")
# 中文数字（ASR 有时给汉字）
CN_NUM = re.compile(r"[一二三四五六七八九十百千万亿]+")

# ★ 口径依赖词：出现它就必须问「分母/基准/时点/时点口径」
SCOPE_TRIGGERS = {
    "面积": "分母是什么？（涨停家数/连板家数/亏钱个股占比/炸板家数）",
    "占比": "分母是什么？（同上，且分子分母是否同一时点）",
    "比例": "分子分母分别是什么？",
    "亏钱效应": "如何量化？下跌股占比/跌停家数/连板断层？",
    "成交量": "沪市/深市/两市合计？单位是成交额还是成交量？",
    "成交额": "同上，且是**全天**还是**截至某时刻**？",
    "换手": "分母是流通股本还是总股本？",
    "市值": "总市值还是流通市值？单位亿元？上下限是否为筛选条件？",
    "炸板": "分母是涨停总数还是曾涨停总数？",
    "连板": "最高板数还是当前连板家数？",
    "红盘": "分母是全市场还是自选？",
    "涨停": "分母是什么？剔除ST 与否？",
    "跌停": "同上（注意涨停排除ST、跌停包含ST）",
    "大盘": "哪个指数？（上证/深证/创业板/科创50）",
    "万手": "单只还是全市场？",
}
# 时点依赖：条件成立的时间窗
TIMING = re.compile(r"(尾盘|盘中|开盘|早盘|午盘|次日|隔日|盘中十|"
                    r"11\s*点|11:\d\d|9:\d\d|14:\d\d|\d+\s*分钟内|"
                    r"半小时|一小时|收盘前)")


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def ocr_rows(creator: str, vid: str) -> list[dict]:
    p = RESEARCH / creator / vid / "ocr.json"
    if not p.exists():
        return []
    rows = load(p).get("rows") or []
    out = []
    for r in rows:
        s = r.get("seconds")
        try:
            t = float(s)
        except (TypeError, ValueError):
            m = re.match(r"(\d+):(\d+(?:\.\d+)?)", str(s))
            if not m:
                continue
            t = float(m.group(1)) * 60 + float(m.group(2))
        txt = (r.get("caption_candidate") or "").strip()
        if txt:
            out.append({"t": t, "text": txt})
    return out


def norm(s: str) -> str:
    """归一化以便跨源比较：去标点空白、常见同音错字不擅自替换。"""
    return re.sub(r"[\s,，。、！？!?；;：:\"'“”‘’…\-—]+", "", s or "")


def dedup_repeat(s: str) -> str:
    """折叠抖音字幕的逐帧重复。

    ★ OCR 每秒采样同一帧，同一句字幕会出现 2~4 遍（`changed` 字段已标记）。
      不折叠的话 OCR 文本长度是 ASR 的 3 倍 ⇒ 相似度被稀释到 0.2~0.4，
      **几乎每段都被误判成「双源不一致」**（实测 66 段，实际冲突只有几段）。

    两步：
      1. `changed=False` 的行是**与上一帧完全相同**的重复 ⇒ 直接丢。
      2. 余下的再按相邻相同单元折叠一次（防同帧内多行同字）。
    """
    t = norm(s)
    for ln in range(len(t) // 2, 2, -1):
        unit = t[:ln]
        if unit and t.startswith(unit) and unit in t[len(unit):]:
            while t.startswith(unit, len(unit)):
                t = t[len(unit):]
            return t
    return t


def ocr_window(creator: str, vid: str, t0: float, t1: float,
               pad: float = 1.5) -> str:
    """→ 该时间窗内的字幕文本（已去重复帧）。"""
    rows = ocr_rows(creator, vid)
    win = [r for r in rows if t0 - pad <= r["t"] <= t1 + pad]
    #1. 只取「本窗内第一帧 + 发生变化的第一帧」——
    #    用全局 changed 判断不了跨窗，改用窗内相邻去重。
    picked: list[str] = []
    prev = None
    for r in win:
        if r["text"] != prev:
            picked.append(r["text"])
        prev = r["text"]
    return dedup_repeat("".join(picked))


CN2NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5,
          "七八": 78, "五六": 56, "二三": 23}

# ★ 量词表必须覆盖真实口语里的**价格/名次单位**，否则汉字数词抽不出来：
#   "低于十块钱" ⇒ 10；"挖题材的48名" ⇒ 48
QUANT = (r"(?:成仓|万手|个点|块钱|块|成|名|手|倍|元|天|板)"
         # ★★ "一个点位" 的"一个点"是**序数+名词**，不是"1 个点"。
         #    漏这个负向断言 ⇒ ASR "大盘的一个点位" 凭空多出数值 1，
         #    OCR 里当然找不到 ⇒ 假冲突（实测 seg20 就是这么来的）。
         #    ★ 连写数词（"两三个点"）由nums_of 的另一条正则负责，
         #      这里**不能留裸 "个"**，否则又会退化成序数。
         r"(?!位)")


def cn_to_num(s: str) -> float | None:
    """'一成'→1.0 '两三个点'→2 '七八个点'→78

    ★ 必须支持**量词前的口语数词**（"两三个点"/"七八个点"/"五六"连写），
      否则 ASR 的"2、3个点"与 OCR 的"两三个点"会永久判成冲突
      （实测这是剩余 6 条假冲突的全部来源）。
    """
    for c, v in CN2NUM.items():
        if s.startswith(c) and (len(s) == 1 or s[1] not in "十百千万亿"):
            return float(v)
    return None


def nums_of(s: str) -> set[float]:
    """抽出可比对的数值集合（阿拉伯 + 汉字），带去重。

    ★ 还要把 ASR 的"2、3个点"这种**顿号并列**还原成 {2,3}，
      才能与 OCR 的"两三个点"（=2）对上。
    """
    out = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", s)}
    #汉字数词 + 量词（QUANT 含负向断言，"一个点位" 不算"1 个点"）
    for m in re.finditer(r"[一两三四五六七八九十半]\s*" + QUANT, s):
        v = cn_to_num(m.group(0))
        if v is not None:
            out.add(v)
    # 汉字数词直接后接阿拉伯数字（OCR 字幕常见混写："五6个点"）
    for m in re.finditer(r"([一两三四五六七八九十半])\s*(\d+(?:\.\d+)?)", s):
        v = cn_to_num(m.group(1))
        if v is not None:
            out.add(v)
        out.add(float(m.group(2)))
    # 汉字连写数词（两三个点 / 七八个点 / 五六个点）
    for m in re.finditer(r"([两三][一二三四五六七八九]|[一二三四五六七八九][两一二三四五六七八九])"
                         r"\s*(?:个点|个|成)", s):
        v = cn_to_num(m.group(1))
        if v is not None:
            out.add(v)
            # "两三个" ⇒ 2 或 3；"七八个" ⇒ 7 或 8
            first = cn_to_num(m.group(1)[0])
            second = cn_to_num(m.group(1)[1])
            for x in (first, second):
                if x is not None and m.group(1)[0] in "两三":
                    out.add(x)
    return out


def ocr_maybe_dropped(asr: str, ocr: str) -> str:
    """检测 OCR 疑似丢字：ASR"4到8名" vs OCR"48名" ⇒ OCR 把"到"丢了。

    ⇒ 这类**不能靠听**（听也是听不到"到"字，那是画面字幕的问题），
      只能标出来让人对着视频画面确认。
    """
    na, no = norm(asr), norm(ocr)
    for m in re.finditer(r"(\d)\s*(?:到|至|-|~)\s*(\d)", na):
        pair = m.group(1) + m.group(2)
        if pair in no and pair not in na:
            return f"OCR疑似丢字：ASR『{m.group(0)}』在OCR 中变成『{pair}』"
    return ""


def text_conflict(asr: str, ocr: str) -> tuple[bool, str]:
    """→ (是否真冲突, 说明)。

    ★★★ 关键：**不能比整句相似度**。
    ASR 段是 2 秒语音切片，OCR 窗是同一秒前后若干句完整字幕
    ⇒ 两者文字范围**本就不重合**，整句相似度天然只有 0.2~0.7。
    实测用整句相似度判「冲突」→ 66 段里几乎全是假冲突。

    ★★ 也不能直接比数字集合：OCR 窗口会溢出到邻句（"没有1万亿"前多了"11点"），
    且"0.5成" 与 "一成" 是同值不同写法 ⇒ 又制造 30+ 假冲突。
    ⇒ 判据：**数值对数值（归一后，且要求 ASR 的每个数都能在 OCR 里找到），
      术语看差集**。数值只要 ASR 侧被OCR 覆盖就不算冲突（允许 OCR 多出邻句的数）。

    真冲突只有两类：
      1. ASR 的某个数在 OCR 里**找不到**（"20%" vs "200%"）—— 规则直接废
      2. 关键术语一边有一边无（"亏欠效应" vs "亏钱效应"）—— 语义变了
    """
    na, no = norm(asr), norm(ocr)
    if not no:
        return True, "无字幕可比"
    #★★ 顺序很关键：**先**判 OCR 丢字（ASR"4到8" vs OCR"48"），
    #    否则它会先被下面判成「数值 4/8 找不到」而永远走不到丢字分支。
    drop = ocr_maybe_dropped(asr, ocr)
    if drop:
        return True, drop
    miss = [f"{x:g}" for x in sorted(nums_of(asr))
            if x not in nums_of(ocr)]
    if miss:
        return True, f"数值在OCR中找不到：{miss}"
    ta, to = set(TERMS_FOUND(na)), set(TERMS_FOUND(no))
    only_a, only_o = ta - to, to - ta
    # OCR 多出邻句术语属正常（窗口溢出）；只有 **ASR 有而 OCR 全无** 才是冲突。
    # 反向（仅 OCR 有）单独降级为提示，不拦。
    if only_a:
        return True, f"术语不一致：ASR有{ sorted(only_a) }但OCR未出现（OCR有{sorted(only_o)}）"
    return False, ""


def TERMS_FOUND(s: str) -> list[str]:
    """片段内出现的财经术语（用于查术语级冲突，不是决策评分）。

    ★ 必须先归一同音错字：ASR 写「亏欠效应」，OCR 写「亏钱效应」，
      不归一就永远判成冲突，而它们其实是**同一个词**——
      归一后这条降为 TEXT_CONFIRMED（文字已确定），只剩口径待答。
    """
    s = norm(s)
    for wrong, right in (("亏欠效应", "亏钱效应"), ("亏钱", "亏钱效应"),
                         ("中卫股", "中位股"), ("高位标地", "高位标的"),
                         ("高位标地", "高位标的"), ("杀爹", "杀跌"),
                         ("悍", "喊"), ("跌得", "跌停")):
        s = s.replace(wrong, right)
    return re.findall(r"(亏钱效应|市值|收盘|盘中|成交量|成交额|"
                      r"仓位|涨停|跌停|炸板|龙头|涨幅|跌幅|分时|中位股|高位标的)",
                      s)


def analyze(tasks: list[dict]) -> list[dict]:
    out = []
    for t in tasks:
        a = t["asr_text"]
        o = ocr_window(t["creator"], t["video_id"], t["start"], t["end"])
        na, no = norm(a), norm(o)
        sim = difflib.SequenceMatcher(None, na, no).ratio() if no else None

        nums = []
        for m in NUM_UNIT.finditer(a):
            nums.append(m.group(0).strip())
        cn = CN_NUM.findall(a)

        #口径触发词
        scope_hits = []
        for kw, q in SCOPE_TRIGGERS.items():
            if kw in a or kw in o:
                scope_hits.append({"keyword": kw, "question": q})
        timing = TIMING.findall(a) or TIMING.findall(o)

        # ★ 判定：只比「数值 + 术语」，不比整句（见 text_conflict 的说明）
        conflict, why = text_conflict(a, o or "")
        if nums or cn:
            if not o:
                verdict = "MUST_LISTEN_NO_OCR"   # 数字无第二来源
            elif conflict:
                verdict = "MUST_LISTEN_DISAGREE"  # 两源在数字/术语上打架
            else:
                verdict = "TEXT_CONFIRMED"        # 两源一致，数字确定
        elif scope_hits or timing:
            verdict = "SCOPE_ONLY"               # 无数字，但口径/时点是缺口
        else:
            continue                             # 无数字无口径，不进本清单

        out.append({
            **{k: t[k] for k in ("creator", "video_id", "title", "pub_time",
                                 "seg_idx", "start", "end", "cut", "audio",
                                 "evidence_ids", "candidate_ids")},
            "asr_text": a,
            "ocr_text": o or None,
            "similarity": round(sim, 3) if sim is not None else None,
            "numbers": nums,
            "cn_numbers": cn[:5],
            "scope_hits": scope_hits,
            "timing": list(dict.fromkeys(timing))[:5],
            "conflict_reason": why,
            "verdict": verdict,
        })
    order = {"MUST_LISTEN_NO_OCR": 0, "MUST_LISTEN_DISAGREE": 1,
             "SCOPE_ONLY": 2, "TEXT_CONFIRMED": 3}
    out.sort(key=lambda r: (order[r["verdict"]],
                            -(len(r["numbers"]) * 2 + len(r["scope_hits"]))))
    return out


V_LABEL = {
    "MUST_LISTEN_NO_OCR": "★ 数字无第二来源（无字幕）→ 必须听",
    "MUST_LISTEN_DISAGREE": "★ 双源不一致 → 必须听",
    "SCOPE_ONLY": "● 口径/时点是缺口（文本已确定）→ 只需答口径",
    "TEXT_CONFIRMED": "○ 两源一致 → 原文已定，只剩口径",
}


def render(items: list[dict], top: int | None = None) -> str:
    L = ["# 数值与口径待确认清单", "",
         "由 `asr.json`（听音轨）× `ocr.json`（读画面字幕）双源交叉生成。",
         "",
         "> **本清单只列缺口，不填答案。** 口径（分母/基准/时点）语料里没有，",
         "> 任何脚本都补不出来 —— 那只能来自博主本人。",
         "> 表中「OCR 原文」为空 = 该片段没有字幕，数字**无第二来源**。", ""]
    from collections import Counter
    c = Counter(i["verdict"] for i in items)
    L.append("| 判定 | 条数 | 含义 |")
    L.append("|---|---|---|")
    for k, n in sorted(c.items(), key=lambda kv: order_of(kv[0])):
        L.append(f"| `{k}` | {n} | {V_LABEL[k]} |")
    L.append(f"| **合计** | **{len(items)}** | |")
    L.append("")
    must = c.get("MUST_LISTEN_NO_OCR", 0) + c.get("MUST_LISTEN_DISAGREE", 0)
    dur = sum(i["end"] - i["start"] for i in items
              if i["verdict"].startswith("MUST"))
    L.append(f"**仍需听 **{must}** 段（{dur:.0f} 秒 = {dur/60:.1f} 分钟）**，"
             f"其余 {len(items)-must} 段只需答口径。")
    L.append("")

    sel = items[:top] if top else items
    for n, it in enumerate(sel, 1):
        L.append(f"## {n}. `{it['verdict']}` "
                 f"[{it['creator']}/{it['video_id']}] "
                 f"{it['start']:.1f}–{it['end']:.1f}s")
        L.append("")
        L.append(f"- {V_LABEL[it['verdict']]}")
        L.append(f"- 作品：{it['title']}（{it['pub_time']}）")
        if it["numbers"]:
            L.append(f"- **ASR 数值**：`{'`、`'.join(it['numbers'])}`")
        if it["cn_numbers"]:
            L.append(f"- 中文数字：`{'`、`'.join(it['cn_numbers'])}`")
        L.append(f"- ASR：`{it['asr_text']}`")
        L.append(f"- OCR：{('`' + it['ocr_text'] + '`') if it['ocr_text'] else '**（无字幕）**'}"
                 + (f"｜整句相似度 {it['similarity']}（**仅供参考，"
                    f"不作为判据**）" if it["similarity"] is not None else ""))
        if it.get("conflict_reason"):
            L.append(f"- ★冲突：{it['conflict_reason']}")
        if it["evidence_ids"]:
            L.append(f"- 出处：`{'`、`'.join(it['evidence_ids'])}`"
                     f"（候选 {'`、`'.join(it['candidate_ids'])}）")
        if it["scope_hits"]:
            L.append("- **必答口径问题**：")
            for s in it["scope_hits"]:
                L.append(f"    - `{s['keyword']}` → {s['question']}")
        if it["timing"]:
            L.append(f"- 涉及时点：`{'`、`'.join(it['timing'])}`"
                     f" → 口径是**盘中某刻**还是**收盘**？")
        if it.get("cut"):
            L.append(f"- 音频：`{it['cut']}`")
        L.append("")
        L.append("- [ ] 口径：____　[ ] 逐字原文：____　"
                 "[ ] 普适/个案：____")
        L.append("")
    return "\n".join(L)


def order_of(v: str) -> int:
    return {"MUST_LISTEN_NO_OCR": 0, "MUST_LISTEN_DISAGREE": 1,
            "SCOPE_ONLY": 2, "TEXT_CONFIRMED": 3}.get(v, 9)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    if not QUEUE.exists():
        sys.exit("先跑 build_listening_queue 生成队列")
    tasks = load(QUEUE)["tasks"]
    items = analyze(tasks)
    if a.only:
        items = [i for i in items if a.only in i["verdict"].lower()
                 or a.only in i["verdict"]]
    OUT_JSON.write_text(json.dumps(
        {"n": len(items), "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    OUT_MD.write_text(render(items), encoding="utf-8")

    from collections import Counter
    c = Counter(i["verdict"] for i in items)
    for k, n in sorted(c.items(), key=lambda kv: order_of(kv[0])):
        print(f"  {k:<24} {n:>4}条")
    must = c.get("MUST_LISTEN_NO_OCR", 0) + c.get("MUST_LISTEN_DISAGREE", 0)
    dur = sum(i["end"] - i["start"] for i in items
              if i["verdict"].startswith("MUST"))
    print(f"合计 {len(items)} 条｜仍需听 {must} 段（{dur:.0f}s = {dur/60:.1f} 分钟）"
          f"｜只需答口径 {len(items)-must} 条")
    print(f"→ {OUT_MD}\n→ {OUT_JSON}")


if __name__ == "__main__":
    main()