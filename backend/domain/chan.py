# -*- coding: utf-8 -*-
"""
Hawk · 缠论引擎（M4 · 附录 C）

对应提示词 M4 的七个构件：
    merge_klines    包含处理
    find_fractals   分型
    build_bi        笔（含老笔/新笔两套标准）
    build_segment   线段（含/不含缺口两种）
    build_zhongshu  中枢（延伸/扩展/新生）
    detect_beichi   背驰（MACD 面积）
    scan_signals    三类买卖点

★★★ 铁律二：纯函数引擎（附录 M.3）
    所有函数签名都是 f(输入, as_of, params) -> 输出，
    **禁止 datetime.now() / time.time()**。
    这不是形式主义 —— 只有这样，M8 复盘才能用「当时的数据」重算引擎，
    从而回答「当时为什么给出这个信号」。已有 AST 级测试守着这条。

★ 停牌处理（附录 C.3）
    停牌日接口不返回数据 / 返回全 0。引擎必须**跳过**这些 bar，
    否则连续同价 bar 会让包含处理产生错误结构。
    本实现在 _clean_bars() 统一处理。

缠论口径说明（重要，避免"实现得不一样"的争议）：
    本实现遵循**新笔标准**（默认）与**老笔标准**（可切换），差异在
    build_bi 的 min_gap：
      · 新笔：顶底分型之间至少 1 根独立 K 线（间隔 >= 4 合并 K线）
      · 老笔：顶底分型必须相邻（间隔 >= 1 合并 K线）
    分型的判定严格按「中间 K 线的高点/低点都高/低」。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST_OFFSET = None     # 本模块不涉及时区换算，时间字符串原样传递


# ================================================================== 数据结构
@dataclass
class Bar:
    """标准 K 线。

    ★★ 索引语义（踩过的坑，务必遵守）：
      ``i0``      = 该 bar 在 **clean_bars 返回的 bars 列表**中的下标。
                    必须与列表位置一致 —— merge_klines / build_bi /
                    _seg_macd_area 全链路都拿它去索引 bars 或 hist。
      ``raw_idx`` = 在**传入 rows 原始序列**中的下标，仅用于回溯定位，
                    不得用于索引 bars（停牌行被剔除后两者不相等）。

    早期版本把 i0 写成「原始序列下标」，而 bars 列表已剔除停牌/脏数据行，
    两者一旦不等，下游 ``bars[a.i0 : b.i1+1]`` 就会整体错位 ——
    实测同一段真实 K 线仅插入一根停牌 bar，笔端点日期与背驰比值全部漂移。
    """

    d: str                # 日期 YYYY-MM-DD
    o: float
    h: float
    l: float
    c: float
    v: float= 0.0
    pct: float = 0.0
    i0: int = 0           # ★ bars 列表下标（不是原始行下标）
    raw_idx: int = 0      # 原始 rows 下标，仅供回溯

    @property
    def i1(self) -> int:
        return self.i0


@dataclass
class MergedBar:
    """包含处理后的合并 K 线。"""

    h: float
    l: float
    i0: int               # 覆盖原始 bar 的起始索引
    i1: int               # 覆盖原始 bar 的结束索引
    direction: int = 1# 合并方向：1=向上取高|低，-1=向下

    @property
    def count(self) -> int:
        return self.i1 - self.i0 + 1


@dataclass
class Fractal:
    """分型。"""

    kind: str             # top | bottom
    idx: int# 在合并序列中的索引
    h: float
    l: float
    i0: int               # 对应原始 bar 索引
    i1: int


@dataclass
class Bi:
    """笔。

    ★ start_raw / end_raw 是**原始 bar 序列**的索引（用于 MACD 面积计算）；
      start_i / end_i 是**合并序列**的索引（用于结构定位）。
      两套坐标系都必须保留 —— 早期版本只留了合并序列索引，
      导致 MACD 面积算错（实测出现 0.0 与 25.156 这种离谱比值）。
    """

    idx: int
    direction: str# up | down
    high: float
    low: float
    start_i: int          # 起始（合并序列索引）
    end_i: int            # 结束（合并序列索引）
    start_date: str
    end_date: str
    fractal: Fractal      # 终结分型
    start_raw: int = 0    # 起始在原始 bar 序列的索引
    end_raw: int = 0      # 结束在原始 bar 序列的索引


@dataclass
class Segment:
    """线段。"""

    idx: int
    direction: str
    high: float
    low: float
    start_i: int
    end_i: int
    bi_start: int
    bi_end: int
    has_gap: bool = False


@dataclass
class ZhongShu:
    """中枢。"""

    idx: int
    high: float
    low: float
    start_i: int
    end_i:int
    start_date: str
    end_date: str
    bi_count: int         # 构成中枢的笔数
    status: str = "extend"    # extend（延伸）| expand（扩展）| new（新生）
    is_last: bool = True      # 是否为当前最后一个（未完成）中枢

    @property
    def amplitude(self) -> float:
        """中枢振幅 %。"""
        return round((self.high / self.low - 1) * 100, 2) if self.low else 0.0


@dataclass
class Beichi:
    """背驰判定结果。

    ★ cur_bi_idx / prev_bi_idx 记录**笔的索引**，让调用方无需反推即可定位。
      早期版本只记 ratio，scan_signals 只能重新猜哪个笔对应背驰，
      导致「拿下跌笔和反弹笔比面积」这种方向性错误。
    """

    kind: str             # top_beichi | bottom_beichi
    ratio: float          # 当前段MACD 面积 / 前同向段面积（<1 为背驰）
    prev_area: float
    cur_area: float
    confirmed: bool = False
    cur_bi_idx: int = -1
    prev_bi_idx: int = -1


@dataclass
class Signal:
    """信号。生成即冻结，禁止事后修改（附录 4.10）。"""

    kind: str             # 1buy 2buy 3buy 1sell 2sell 3sell
    level: str            # strong | normal | weak
    bi_idx: int
    price: float
    date: str
    reason: str
    confidence: float
    zhongshu: ZhongShu | None = None


@dataclass
class ChanResult:
    """引擎总输出。UI 与信号层消费此结构。"""

    code: str
    as_of: str
    bars: list[Bar] = field(default_factory=list)
    merged: list[MergedBar] = field(default_factory=list)
    fractals: list[Fractal] = field(default_factory=list)
    bis: list[Bi] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    zhongshus: list[ZhongShu] = field(default_factory=list)
    beichis: list[Beichi] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    # ------- 便捷视图（附录 4.11UI 需要的派生指标）-------
    def last_zhongshu(self) -> ZhongShu | None:
        return self.zhongshus[-1] if self.zhongshus else None

    def position(self) -> str:
        """当前价格相对最后中枢的位置。"""
        zs = self.last_zhongshu()
        if not zs or not self.bars:
            return "无中枢"
        px = self.bars[-1].c
        if px > zs.high:
            return "中枢上方"
        if px < zs.low:
            return "中枢下方"
        return "中枢内"

    def bi_direction(self) -> str | None:
        return self.bis[-1].direction if self.bis else None


# ================================================================== 指标
def ema(values: list[float], n: int) -> list[float]:
    """指数移动平均。k = 2/(n+1)。"""
    k = 2.0 / (n + 1)
    out: list[float] = []
    e: float | None = None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def macd_series(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    """MACD (DIF, DEA, MACD柱)。

    注意：缠论的背驰用**柱状面积**衡量，需注意缩放约定 ——
    这里柱值 = (DIF - DEA) * 2（国内常用口径）。
    """
    e12, e26 = ema(closes, 12), ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = ema(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    return dif, dea, hist


# ================================================================== 清洗
def clean_bars(rows: list[dict], suspend_tol: float = 0.0) -> list[Bar]:
    """清洗原始 K 线：排序、去停牌、校验 OHLC。

    ★ 停牌处理（附录 C.3）：
      停牌日表现为 high=low=open=0 或 volume=0。
      若不剔除，连续的同价 bar 会让包含处理判定错误、产生虚假分型。
      这里统一剔除，并在 Bar 里保留原始索引以便回溯。
    """
    bars: list[Bar] = []
    for idx, r in enumerate(rows):
        try:
            h, l = float(r["high"]), float(r["low"])
            o, c = float(r["open"]), float(r["close"])
            v = float(r.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        # 停牌或脏数据剔除
        if h <= 0 or l <= 0 or c <= 0:
            continue
        if h < l:                      # OHLC 不自洽，跳过
            continue
        bars.append(Bar(d=str(r.get("trade_date") or r.get("d") or ""),
                        o=o, h=h, l=l, c=c, v=v,
                        pct=float(r.get("pct_chg") or 0),
                        i0=len(bars),      # ★ bars 列表下标（剔除后仍连续）
                        raw_idx=idx))      # 原始行下标，仅供回溯
    return bars


# ================================================================== ① 包含处理
def merge_klines(bars: list[Bar]) -> list[MergedBar]:
    """包含关系处理（附录 C.2 步骤 1）。

    规则（标准定义）：
      若当前 K 线被前一根 K 线包含（h<=前h 且 l>=前l，或反之），则合并：
      · 向上处理：取两者的高点、低点中**较高/较高**者 -> new_h=max, new_l=max
      · 向下处理：new_h=min, new_l=min
      方向由前一根合并 K 线与再前一根比较决定。

    ★ 返回的 MergedBar 记录 i0/i1（覆盖的原始区间），
      后续所有构件的「原始索引」都可由此回溯 —— 这是复盘能对齐的关键。
    """
    if not bars:
        return []
    merged: list[MergedBar] = [
        MergedBar(h=bars[0].h, l=bars[0].l, i0=bars[0].i0, i1=bars[0].i0)
    ]
    for b in bars[1:]:
        last = merged[-1]
        # 判断是否有包含关系
        contains = (b.h <= last.h and b.l >= last.l) or (b.h >= last.h and b.l <= last.l)
        if not contains:
            merged.append(MergedBar(h=b.h, l=b.l, i0=b.i0, i1=b.i0))
            continue
        # 有包含 -> 按方向合并
        if len(merged) >= 2:
            up = merged[-1].h > merged[-2].h
        else:
            up = b.h > last.h          # 首两根无法判定方向时的保守约定
        if up:
            last.h = max(last.h, b.h)
            last.l = max(last.l, b.l)
        else:
            last.h = min(last.h, b.h)
            last.l = min(last.l, b.l)
        last.i1 = b.i0
        last.direction = 1 if up else -1
    return merged


# ================================================================== ② 分型
def find_fractals(merged: list[MergedBar]) -> list[Fractal]:
    """顶底分型（附录 C.2 步骤 2）。

    顶分型：中间 K 线的 high 是三者最高、low 是三者最高
    底分型：中间 K 线的 low 是三者最低、high 是三者最低
    """
    fr: list[Fractal] = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b.h > a.h and b.h > c.h and b.l > a.l and b.l > c.l:
            fr.append(Fractal("top", i, b.h, b.l, b.i0, b.i1))
        elif b.h < a.h and b.h < c.h and b.l < a.l and b.l < c.l:
            fr.append(Fractal("bottom", i, b.h, b.l, b.i0, b.i1))
    return fr


# ================================================================== ③ 笔
def build_bi(merged: list[MergedBar], fractals: list[Fractal],
             strict: str = "new", bars: list[Bar] | None = None) -> list[Bi]:
    """笔（附录 C.2 步骤 3）。

    strict:
      "new"（新笔，默认）—— 顶底分型间至少 1 根独立 K 线，即分型间隔 >= 4 合并 K 线
      "old"（老笔）—— 顶底分型相邻即可，间隔 >= 1
    """
    if len(fractals) < 2:
        return []
    min_gap = 4 if strict == "new" else 1

    raw: list[Fractal] = [fractals[0]]
    for f in fractals[1:]:
        last = raw[-1]
        if f.kind == last.kind:
            # 同类分型：取更极端的那个（顶取更高，底取更低）
            if (f.kind == "top" and f.h > last.h) or (f.kind == "bottom" and f.l < last.l):
                raw[-1] = f
        else:
            if f.idx - last.idx >= min_gap:
                raw.append(f)
    if len(raw) < 2:
        return []

    bis: list[Bi] = []
    for k in range(len(raw) - 1):
        a, b = raw[k], raw[k + 1]
        direction = "up" if a.kind == "bottom" else "down"
        # 用原K 线取端点极值，避免被合并后的边界影响
        if bars:
            # ★ 原始索引区间：从起始分型到终结分型，覆盖整笔
            seg_start = max(0, min(a.i0, len(bars) - 1))
            seg_end = max(seg_start, min(b.i1, len(bars) - 1))
            seg = bars[seg_start : seg_end + 1]
            high = max(x.h for x in seg)
            low = min(x.l for x in seg)
            d0 = seg[0].d
            d1 = seg[-1].d
        else:
            seg_start, seg_end = a.i0, b.i1
            high = max(a.h, b.h)
            low = min(a.l, b.l)
            d0, d1 = "", ""
        bis.append(Bi(idx=k, direction=direction, high=high, low=low,
                      start_i=a.idx, end_i=b.idx, start_date=d0, end_date=d1,
                      fractal=b, start_raw=seg_start, end_raw=seg_end))
    return bis


# ================================================================== ④ 线段
def build_segment(bis: list[Bi], strict: str = "new") -> list[Segment]:
    """线段（附录 C.2 步骤 4）。

    实现要点：
      · 以笔为单位，若连续三笔同向重叠则线段延续（标准特征序列法的简化实现）
      · strict="strict" 时要求不含缺口（跳空），含缺口则打断
    """
    if len(bis) < 3:
        return []
    segs: list[Segment] = []
    i = 0
    while i + 2 < len(bis):
        a, b, c = bis[i], bis[i + 1], bis[i + 2]
        # 简化判据：b 的方向与 a 相反，且 c 与 a 同向 -> a..c 构成一个线段
        if b.direction != a.direction and c.direction == a.direction:
            seg_high = max(x.high for x in (a, b, c))
            seg_low = min(x.low for x in (a, b, c))
            has_gap = _has_gap(bis, i, i + 3)
            if strict == "strict" and has_gap:
                i += 1
                continue
            segs.append(Segment(idx=len(segs), direction=a.direction,
                                high=seg_high, low=seg_low,
                                start_i=a.start_i, end_i=c.end_i,
                                bi_start=i, bi_end=i + 3, has_gap=has_gap))
            i += 3
        else:
            i += 1
    return segs


def _has_gap(bis: list[Bi], i0: int, i1: int) -> bool:
    """判断笔区间内是否存在缺口（后一笔起点超出前一笔终点）。"""
    for k in range(i0, min(i1, len(bis) - 1)):
        nxt = bis[k + 1]
        cur = bis[k]
        if cur.direction == "up" and nxt.low > cur.high:
            return True
        if cur.direction == "down" and nxt.high < cur.low:
            return True
    return False


# ================================================================== ⑤ 中枢
def build_zhongshu(bis: list[Bi], bars: list[Bar] | None = None,
                   min_bi: int = 3) -> list[ZhongShu]:
    """中枢（附录 C.2 步骤 5）。

    定义：连续三笔重叠区间即中枢。后续走势仍被包含则延伸（extend），
    超出则结束；新的一组三笔重叠则新生（new）。
    """
    zs_list: list[ZhongShu] = []
    i = 0
    while i + min_bi - 1 < len(bis):
        trio = bis[i : i + min_bi]
        hi = min(x.high for x in trio)
        lo = max(x.low for x in trio)
        if lo < hi:
            j = i + min_bi
            while j < len(bis):
                nxt = bis[j]
                if nxt.high <= hi and nxt.low >= lo:
                    j += 1                      # 延伸
                else:
                    break
            last = bis[j - 1]
            zs_list.append(ZhongShu(
                idx=len(zs_list), high=round(hi, 2), low=round(lo, 2),
                start_i=trio[0].start_i, end_i=last.end_i,
                start_date=trio[0].start_date, end_date=last.end_date,
                bi_count=j - i, status="extend",
                is_last=(j >= len(bis)),
            ))
            i = j
        else:
            i += 1
    return zs_list


# ================================================================== ⑥ 背驰
def _seg_macd_area(bi: Bi, hist: list[float], direction: str) -> float:
    """计算某一笔区间内MACD 柱面积（同向取正）。

    ★ 坐标系（实测踩坑总结）：
      hist 按**原始 bar 序列**计算，因此必须用 Bi.start_raw/end_raw，
      不能用 start_i/end_i（那是合并序列的下标，坐标系不同）。
      早期版本误用合并序列索引，MACD 面积算错，
      实测出现过ratio=0.0（区间太窄）与 ratio=25.156（错位偏移）。
    """
    if not hist:
        return 0.0
    m0 = max(0, min(bi.start_raw, len(hist) - 1))
    m1 = max(m0, min(bi.end_raw, len(hist) - 1))
    if m1 <= m0:
        return 0.0
    part = hist[m0 : m1 + 1]
    if direction == "up":
        return sum(x for x in part if x > 0)
    return sum(-x for x in part if x < 0)


# ★ 背驰面积门槛（附录 5.3「度量式」的实现参数）
#   分母过小会产生无物理意义的巨值比（实测 a1=0.063 / a2=1.586 => ratio=25.156），
#   分子为 0 则表示该波段 MACD 柱全为反方向（不是背驰，是无正面积）。
#   两者都必须过滤，否则「背驰」标签会建立在噪声上。
MIN_BEICHI_AREA = 1.0


def detect_beichi(bis: list[Bi], hist: list[float], bars: list[Bar],
                  min_area: float = MIN_BEICHI_AREA) -> list[Beichi]:
    """背驰判定（附录 C.2 步骤 6）：MACD 柱面积比较。

    规则：当前同向段的 MACD 面积 < 前一同向段面积 -> 背驰。
    这是**度量式**判定（附录 5.3 要求用「面积度量」，不靠主观感觉）。

    ★ 关键：笔严格交替，因此「同向的两个波段」= 同向笔列表中相邻的两个元素，
      它们中间隔着一根反向笔。早期待错成 bis[k] vs bis[k+1]（那是反向的两笔），
      方向都不同，比出来的 ratio 毫无意义（实测出现 0.0 / 25.156）。

    ★ min_area 门槛（实测必需）：
      分母（前一波段面积）过小时，比值会爆表—— 实测出现 a1=0.063 而 a2=1.586
      导致 ratio=25.156。这种比值没有物理意义（不是背驰，是噪声比大小）。
      故分母低于门槛时**不判定背驰**，直接跳过。
    """
    out: list[Beichi] = []
    for direction, kind in (("up", "top_beichi"), ("down", "bottom_beichi")):
        idxs = [k for k, b in enumerate(bis) if b.direction == direction]
        if len(idxs) < 2:
            continue
        for j in range(1, len(idxs)):
            prev_k, cur_k = idxs[j - 1], idxs[j]
            a1 = _seg_macd_area(bis[prev_k], hist, direction)
            a2 = _seg_macd_area(bis[cur_k], hist, direction)
            # ★ 双向门槛（实测必需）：
            #   分母过小 -> 比值爆表（实测 a1=0.063 / a2=1.586 => ratio=25.156）
            #   分子为0  -> 该波段 MACD 柱全为反方向，不是背驰而是「无正面积」
            #   两者都必须跳过，否则会产生看似精确实则无意义的比值。
            if a1 < min_area or a2 < min_area:
                continue
            ratio = round(a2 / a1, 3)
            out.append(Beichi(kind=kind, ratio=ratio, prev_area=round(a1, 3),
                              cur_area=round(a2, 3), confirmed=ratio < 1.0,
                              cur_bi_idx=cur_k, prev_bi_idx=prev_k))
    return out


# ================================================================== ⑦ 三类买卖点
def scan_signals(bis: list[Bi], zhongshus: list[ZhongShu],
                 beichis: list[Beichi], bars: list[Bar],
                 hist: list[float], as_of: datetime | str,
                 strict: str = "new") -> list[Signal]:
    """三类买卖点扫描（附录 C.2 步骤 7）。

    定义（标准缠论）：
      一买：下跌趋势中出现底背驰
      二买：一买之后回调不破前低
      三买：向上离开中枢后回抽不跌破中枢上沿（ZG）

    口径说明：
      未完成笔不产生信号（只用已确认的笔），避免信号反复。
    """
    signals: list[Signal] = []
    if not bis:
        return signals

    # ---- 一买/二买：底背驰 ----
    # ★ 直接用 detect_beichi 给出的笔索引，不再自己比一次。
    #   笔严格交替（相邻必反向），拿 bis[k] 与 bis[k+1] 比面积等于
    #   「拿下跌笔和反弹笔比」，方向都不同（实测出现 0.0 / 25.156 这类离谱值）。
    bot_bc: dict[int, Beichi] = {
        bc.cur_bi_idx: bc for bc in beichis
        if bc.kind == "bottom_beichi" and bc.confirmed and bc.cur_bi_idx >= 0
    }
    down_idxs = [k for k, b in enumerate(bis) if b.direction == "down"]
    for k in down_idxs:
        if k not in bot_bc:
            continue
        b = bis[k]
        bc = bot_bc.get(k)
        conf = round(min(0.95, 0.55 + (1 - bc.ratio) * 0.4), 3) if bc else 0.6
        signals.append(Signal(
            kind="1buy", level="strong" if (bc and bc.ratio < 0.8) else "normal",
            bi_idx=k, price=b.low, date=b.end_date,
            reason=f"下跌段底背驰（MACD 面积比 {bc.ratio if bc else 'NA'}）",
            confidence=conf))

    # 二买：1buy 之后反弹，再回调低点高于前低
    for i in range(len(down_idxs) - 1):
        k1, k2 = down_idxs[i], down_idxs[i + 1]
        if k1 not in bot_bc:
            continue
        b1, b2 = bis[k1], bis[k2]
        if b2.low > b1.low:
            signals.append(Signal(
                kind="2buy", level="normal", bi_idx=k2, price=b2.low,
                date=b2.end_date,
                reason=f"一买后回抽低点 {b2.low:.2f} 高于一买低点 {b1.low:.2f}",
                confidence=0.65))

    # ---- 三买：向上离开中枢后回抽不破 ZG ----
    for zs in zhongshus:
        after = [k for k, b in enumerate(bis) if b.start_i > zs.end_i]
        if not after:
            continue
        # 找向上离开中枢的笔
        exit_k = None
        for k in after:
            if bis[k].direction == "up" and bis[k].high > zs.high:
                exit_k = k
                break
        if exit_k is None:
            continue
        pull = [k for k in after if k > exit_k and bis[k].direction == "down"]
        if not pull:
            # ★★ 弱三买的入场价必须用**信号日收盘价**，不能用突破笔最高价。
            #   实测（M8 三买归因，1200 只真实样本）：
            #     弱三买胜率 20.0%、平均 -4.31%；强三买胜率 86.2%、平均 +7.75%。
            #   逐例核查发现弱三买的price 恒等于信号日**最高价**
            #   （5/5 案例 信号价/当日最高 = 1.000），比当日收盘高 3.5%~16.2%。
            #   用当日最高价当「买入价」在现实中根本无法成交 —— 那是事后挑出的
            #   最贵价位（典型如 605179：信号价 31.48 = 当日最高，当日收盘 27.09，
            #   冲高回落 -14%），统计上必然被算成大亏。
            #   「弱三买」的真实语义是「已突破但回抽未确认，观察用」，
            #   不是「现在这个价可以买」——所以只能给收盘价，且不得进推荐池。
            sig_i = bis[exit_k].end_raw
            px = bars[sig_i].c if 0 <= sig_i < len(bars) else bis[exit_k].high
            signals.append(Signal(
                kind="3buy", level="weak", bi_idx=exit_k, price=px,
                date=bis[exit_k].end_date,
                reason=f"已向上离开中枢上沿 {zs.high:.2f}，尚未回抽确认"
                       f"（观察价=信号日收盘 {px:.2f}，非最高价）",
                confidence=0.4, zhongshu=zs))
            continue
        pk = pull[0]
        pb = bis[pk]
        if pb.low > zs.high:
            signals.append(Signal(
                kind="3buy", level="strong", bi_idx=pk, price=pb.low,
                date=pb.end_date,
                reason=f"回抽低点 {pb.low:.2f} 未破中枢上沿 {zs.high:.2f}（三买成立）",
                confidence=0.8, zhongshu=zs))

    # ---- 三卖：向下离开中枢后反抽不上破 ZD ----
    for zs in zhongshus:
        after = [k for k, b in enumerate(bis) if b.start_i > zs.end_i]
        if not after:
            continue
        exit_k = None
        for k in after:
            if bis[k].direction == "down" and bis[k].low < zs.low:
                exit_k = k
                break
        if exit_k is None:
            continue
        pull = [k for k in after if k > exit_k and bis[k].direction == "up"]
        if pull:
            pk = pull[0]
            pb = bis[pk]
            if pb.high < zs.low:
                signals.append(Signal(
                    kind="3sell", level="strong", bi_idx=pk, price=pb.high,
                    date=pb.end_date,
                    reason=f"反抽高点 {pb.high:.2f} 未上破中枢下沿 {zs.low:.2f}（三卖成立）",
                    confidence=0.8, zhongshu=zs))

    # 一卖/二卖 对称处理
    top_bc: dict[int, Beichi] = {
        bc.cur_bi_idx: bc for bc in beichis
        if bc.kind == "top_beichi" and bc.confirmed and bc.cur_bi_idx >= 0
    }
    up_idxs = [k for k, b in enumerate(bis) if b.direction == "up"]
    for k in up_idxs:
        if k not in top_bc:
            continue
        b = bis[k]
        signals.append(Signal(
            kind="1sell", level="strong", bi_idx=k, price=b.high, date=b.end_date,
            reason="上涨段顶背驰", confidence=0.7))
    for i in range(len(up_idxs) - 1):
        k1, k2 = up_idxs[i], up_idxs[i + 1]
        if k1 not in top_bc:
            continue
        b1, b2 = bis[k1], bis[k2]
        if b2.high < b1.high:
            signals.append(Signal(
                kind="2sell", level="normal", bi_idx=k2, price=b2.high,
                date=b2.end_date,
                reason=f"一卖后回落高点 {b2.high:.2f} 低于一卖高点 {b1.high:.2f}",
                confidence=0.65))

    signals.sort(key=lambda s: (s.bi_idx, s.kind))
    return signals


# ================================================================== 顶层入口
def analyze(code: str, rows: list[dict], as_of: datetime | str,
            strict_bi: str = "new", strict_segment: str = "new") -> ChanResult:
    """缠论引擎顶层入口。

    ★ 纯函数：as_of 由调用方注入，本函数不取系统时间（铁律二 / 附录 M.3）。
    ★ as_of 会写入结果，确保回测时能知道「这是为哪个时点算的」。
    """
    as_of_str = (as_of.isoformat(timespec="seconds") if isinstance(as_of, datetime)
                 else str(as_of))
    bars = clean_bars(rows)
    merged = merge_klines(bars)
    fractals = find_fractals(merged)
    bis = build_bi(merged, fractals, strict=strict_bi, bars=bars)
    segments = build_segment(bis, strict=strict_segment)

    closes = [b.c for b in bars]
    _, _, hist = macd_series(closes)
    zhongshus = build_zhongshu(bis, bars=bars)
    beichis = detect_beichi(bis, hist, bars)
    signals = scan_signals(bis, zhongshus, beichis, bars, hist,
                           as_of=as_of_str, strict=strict_bi)

    return ChanResult(
        code=code, as_of=as_of_str, bars=bars, merged=merged, fractals=fractals,
        bis=bis, segments=segments, zhongshus=zhongshus, beichis=beichis,
        signals=signals,
        params={"strict_bi": strict_bi, "strict_segment": strict_segment,
                "bar_count": len(bars)},
    )


if __name__ == "__main__":
    from backend.data import duck

    print("=" * 74)
    print("=== M4 缠论引擎 · 真实数据实测 ===")
    print("=" * 74)

    for code in ("600519", "300750", "002594"):
        rows = duck.get_daily(code, limit=250)
        if len(rows) < 60:
            print(f"\n{code}: K线不足（{len(rows)}），跳过")
            continue
        res = analyze(code, rows, as_of="2026-10-08T15:00:00+08:00")
        print(f"\n【{code}】as_of={res.as_of}")
        print(f"  K线 {len(res.bars)} 根 → 合并 {len(res.merged)} 根"
              f"（合并掉 {len(res.bars) - len(res.merged)} 根包含关系）")
        print(f"  分型 {len(res.fractals)} 个"
              f"（顶{sum(1 for f in res.fractals if f.kind=='top')} /"
              f" 底{sum(1 for f in res.fractals if f.kind=='bottom')}）")
        print(f"  笔 {len(res.bis)} 段 | 线段 {len(res.segments)} 段")
        print(f"  中枢 {len(res.zhongshus)} 个")
        for zs in res.zhongshus[-2:]:
            print(f"    #{zs.idx} {zs.low:.2f}~{zs.high:.2f} 振幅{zs.amplitude:.1f}%"
                  f" {zs.start_date}~{zs.end_date} {zs.status}")
        print(f"  背驰 {len(res.beichis)} 个"
              f"（确认{sum(1 for b in res.beichis if b.confirmed)}）")
        for b in res.beichis[:3]:
            print(f"    {b.kind} 面积比{b.ratio} 确认={b.confirmed}")
        print(f"  信号 {len(res.signals)} 个")
        for s in res.signals[:4]:
            print(f"[{s.kind}] {s.date}价{s.price:.2f} 强度={s.level} {s.reason}")
        print(f"  当前位置: {res.position()} | 当前笔方向: {res.bi_direction()}")

    print("\n" + "=" * 74)
    print("所有输出均由 f(输入, as_of, params) 计算，未取系统时间（铁律二）")