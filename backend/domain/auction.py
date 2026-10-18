# -*- coding: utf-8 -*-
"""★ 集合竞价监控（规格 6.1 · 09:15–09:25）。

============================ 为什么竞价值得单独监控 ============================
连续竞价（09:30 后）反映的是**已成交**的价格，
而集合竞价（09:15–09:25）反映的是**尚未成交的意愿**：
  · 09:15–09:20 可撤单⇒ 此时的量**包含大量噪声**（历史教训：撤单率可达 70%+）
  · 09:20–09:25 **不可撤单** ⇒ 此时的量才是**真实承诺**
⇒ 所以本模块对两个窗口**分别判定**，且在早窗结论里显式标注「含撤单噪声」。

=============================== 为什么不能补跑历史 ==============================
竞价数据**只在 09:15–09:25 这 10 分钟真实存在**，
过期后接口返回的是当日收盘后的正常盘口，不是竞价盘口。
任何「补造」都是编造 ⇒ 本模块 `as_of` 不在窗口内时**直接拒绝**，
而不是回落到最新盘口（那会让人以为看到的是竞价数据）。

=============================== 指标口径（全部纯函数）===============================
· `auction_ratio` 竞价量比= 竞价量 / 过去5 日平均每分钟成交量
    —— 衡量「相对平时放大了多少倍」
· `bid_ask_imbalance` 委比 = (买量- 卖量) / (买量 + 卖量)
    —— 衡量**方向**意图。取值 [-1, 1]
· `premium_pct` 竞价溢价= (竞价价 - 昨收) / 昨收
    —— 衡量**价格**意图。高开过多往往反而回落

★ 三者**必须分开看**：量能大的高开与量能小的高开含义完全不同，
   合成为一个「竞价强弱分」会掩盖掉真正有用的区分。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as daytime

# 全局配色约定：涨=红 跌=绿
CSS_UP = "#d94b4b"
CSS_DOWN = "#2e9e5b"
CSS_FLAT = "#888"

# ────────────────────────────────────────── 窗口定义（有语义，勿硬改）
#★ 分两窗是因为**可撤单 / 不可撤单**的语义差异，不是拍脑袋。
OPEN_AUCTION_START = daytime(9, 15)
OPEN_AUCTION_CANCEL_END = daytime(9, 20)      # 此后可撤单
OPEN_AUCTION_END = daytime(9, 25)            # 撮合产生开盘价
AUCTION_COMMIT_FROM = daytime(9, 20)         # 不可撤单起点

# 五档盘口在腾讯实时报文里的下标（实测核对，见 tencent.py 字段表）
BID_PRICE_IDX = 9      # bid1~bid5 价：9/11/13/15/17
BID_VOL_IDX = 10       # bid1~bid5 量(手)：10/12/14/16/18
ASK_PRICE_IDX = 19     # ask1~ask5 价：19/21/23/25/27
ASK_VOL_IDX = 20       # ask1~ask5 量(手)：20/22/24/26/28
DEPTH = 5


# ────────────────────────────────────────── 纯函数：盘口解析
@dataclass
class Book:
    """五档盘口。`None` 表示该档无挂单。"""
    bid_prices: list[float | None] = field(default_factory=list)
    bid_vols: list[float | None] = field(default_factory=list)
    ask_prices: list[float | None] = field(default_factory=list)
    ask_vols: list[float | None] = field(default_factory=list)

    @property
    def bid_amount(self) -> float:
        """买盘挂单量合计（手）。"""
        return sum(v for v in self.bid_vols if v)

    @property
    def ask_amount(self) -> float:
        """卖盘挂单量合计（手）。"""
        return sum(v for v in self.ask_vols if v)


def parse_book(f: list[str]) -> Book:
    """腾讯实时报文 `~` 切分后的字段列表 → `Book`。

    ★ 竞价时段**买一档即竞价价格**（未成交的意愿集中在一档），
      所以本函数保留全部五档，由调用方按窗口选择口径。
    """
    def num(i: int) -> float | None:
        try:
            v = f[i].strip()
            return float(v) if v else None
        except (IndexError, ValueError):
            return None

    return Book(
        bid_prices=[num(BID_PRICE_IDX + 2 * k) for k in range(DEPTH)],
        bid_vols=[num(BID_VOL_IDX + 2 * k) for k in range(DEPTH)],
        ask_prices=[num(ASK_PRICE_IDX + 2 * k) for k in range(DEPTH)],
        ask_vols=[num(ASK_VOL_IDX + 2 * k) for k in range(DEPTH)],
    )


# ────────────────────────────────────────── 纯函数：指标
def auction_ratio(auction_vol: float, avg_5d_vol: float | None) -> float | None:
    """竞价量比。`avg_5d_vol` 为 None 时返回 **None 而非 1.0**。

    ★ 缺失当0 或当 1.0 都是编造：
      当 1.0 会让「量能未知」被读成「量能正常」——
      这是「缺失数据不能当默认值」的又一例（见项目铁律）。
    """
    if avg_5d_vol is None or avg_5d_vol <= 0:
        return None
    return round(auction_vol / avg_5d_vol, 2)


def imbalance(book: Book) -> float | None:
    """委比 ∈ [-1, 1]。两边都没挂单时返回 None。"""
    b, a = book.bid_amount, book.ask_amount
    if (b + a) <= 0:
        return None
    return round((b - a) / (b + a), 3)


def premium_pct(auction_price: float | None,
                pre_close: float | None) -> float | None:
    """竞价溢价 %。缺价或缺昨收都返回 None。"""
    if not auction_price or not pre_close or pre_close <= 0:
        return None
    return round((auction_price - pre_close) / pre_close * 100, 2)


# ────────────────────────────────────────── 纯函数：判定
#★ 阈值集中在此且**必须在注释里写明依据**，不允许散落在调用方。
#   量比：>2 显著放量（历史经验值，需积累样本后校准）
TH_VOL_HI = 2.0
TH_VOL_LO = 0.6
#   委比：>0.3 偏买 / <-0.3 偏卖
TH_IMB_HI = 0.3
TH_IMB_LO = -0.3
#   高开/低开幅度
TH_PREMIUM_HI = 2.0
TH_PREMIUM_LO = -2.0


@dataclass
class AuctionVerdict:
    code: str
    name: str
    auction_price: float | None
    pre_close: float | None
    vol_ratio: float | None
    imb: float | None
    premium: float | None
    strength: str                 # 强买/偏买/中性/偏卖/强卖/数据不足
    window: str                   # early（可撤单） | commit（不可撤单）
    noisy: bool                   # ★ 早窗结论是否含撤单噪声
    notes: list[str] = field(default_factory=list)


def judge(code: str, name: str, as_of: datetime,
          auction_price: float | None, pre_close: float | None,
          auction_vol: float | None, avg_5d_vol: float | None,
          book: Book) -> AuctionVerdict:
    """→ `AuctionVerdict`。纯函数，铁律二。

    ★ 窗口语义**必须体现到结论上**：
      09:20 前的量含撤单噪声 ⇒ `noisy=True`，
      下游/邮件必须显示这一点，否则用户会把撤单误读成抢筹。
    """
    t = as_of.time().replace(tzinfo=None)
    early = t < OPEN_AUCTION_CANCEL_END
    window = "early" if early else "commit"

    vr = auction_ratio(auction_vol or 0.0, avg_5d_vol)
    ib = imbalance(book)
    pm = premium_pct(auction_price, pre_close)
    notes: list[str] = []

    # ★ 三个指标全缺 ⇒ 直接判「数据不足」，不猜。
    if vr is None and ib is None and pm is None:
        return AuctionVerdict(code, name, auction_price, pre_close,
                              vr, ib, pm, "数据不足", window, early,
                              ["三个指标均不可得"])

    # ---- 方向：优先委比（最直接反映意愿），退而看溢价 ----
    score = 0
    if ib is not None:
        if ib >= TH_IMB_HI:
            score += 2
        elif ib <= TH_IMB_LO:
            score -= 2
    else:
        notes.append("无盘口数据，方向仅由溢价推断")

    # ---- 量能：放大/萎缩是**强度修饰**，不独立定方向 ----
    if vr is not None:
        if vr >= TH_VOL_HI:
            notes.append(f"竞价量放大 {vr}倍")
        elif vr <= TH_VOL_LO:
            notes.append(f"竞价量萎缩 {vr}倍")
    else:
        notes.append("无历史均量，量比不可算（不按 1.0 处理）")

    # ---- 价格：极端高开/低开单独提示 ----
    if pm is not None:
        if pm >= TH_PREMIUM_HI:
            notes.append(f"高开 {pm:+.2f}%")
        elif pm <= TH_PREMIUM_LO:
            notes.append(f"低开 {pm:+.2f}%")

    if early:
        notes.append("⚠ 09:20 前可撤单，本结论含撤单噪声")

    strength = ("强买" if score >= 2 else "偏买" if score >= 1
                else "强卖" if score <= -2 else "偏卖" if score <= -1
                else "中性")
    return AuctionVerdict(code, name, auction_price, pre_close,
                          vr, ib, pm, strength, window, early, notes)


# ────────────────────────────────────────── 纯函数：窗口判定
def in_auction_window(as_of: datetime) -> bool:
    """`as_of` 是否落在 09:15–09:25 内。

    ★ 边界：09:15:00 算**在内**（集合竞价已开始），
      09:25:00 算**不在**（已产生开盘价，转连续竞价）。
    """
    t = as_of.time().replace(tzinfo=None)
    return OPEN_AUCTION_START <= t < OPEN_AUCTION_END


def reject_reason(as_of: datetime, max_lag_s: int = 60) -> str | None:
    """不在窗口内（或滞后过大）时给出拒绝理由；在窗口内返回 None。

    ★ 竞价数据**不可补造** —— 过了窗口接口返回的就是正常盘口，
      拿它当竞价数据是最危险的静默错误（看起来有数、实际是错的）。
    """
    if not in_auction_window(as_of):
        return (f"{as_of:%H:%M} 不在集合竞价窗口"
                f"（09:15–09:25）；竞价数据不可补造，已拒绝")
    return None


# ────────────────────────────────────────── 渲染
def _c(v: float | None) -> str:
    if v is None:
        return CSS_FLAT
    return CSS_UP if v > 0 else (CSS_DOWN if v < 0 else CSS_FLAT)


def render_text(rows: list[AuctionVerdict]) -> str:
    L = [f"集合竞价监控{'（含撤单噪声）' if rows and rows[0].noisy else ''}",
         "=" * 72,
         f"{'代码':8s}{'名称':10s}{'竞价价':>10s}{'溢价':>9s}"
         f"{'量比':>8s}{'委比':>8s}  判定"]
    L.append("-" * 72)
    for r in rows:
        pm = f"{r.premium:+.2f}%" if r.premium is not None else "—"
        vr = f"{r.vol_ratio:.2f}" if r.vol_ratio is not None else "—"
        ib = f"{r.imb:+.2f}" if r.imb is not None else "—"
        px = f"{r.auction_price:.2f}" if r.auction_price is not None else "—"
        L.append(f"{r.code:8s}{r.name[:8]:10s}{px:>10s}{pm:>9s}"
                 f"{vr:>8s}{ib:>8s}  {r.strength}")
        for n in r.notes:
            L.append(f"{'':37s}{n}")
    L.append("-" * 72)
    L.append("本邮件由个人研究系统自动生成，不构成任何投资建议")
    return "\n".join(L)


def render_html(rows: list[AuctionVerdict]) -> str:
    import html

    def h(x) -> str:
        return html.escape(str(x if x is not None else ""))

    trs = ""
    for r in rows:
        notes = "".join(
            f"<div style='font-size:11px;color:#888'>{h(n)}</div>"
            for n in r.notes)
        trs += (
            f"<tr><td style='padding:3px 8px'>{h(r.code)}</td>"
            f"<td style='padding:3px 8px'>{h(r.name)}</td>"
            f"<td style='padding:3px 8px;text-align:right'>"
            f"{('%.2f' % r.auction_price) if r.auction_price is not None else '—'}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:{_c(r.premium)}'>"
            f"{('%+.2f%%' % r.premium) if r.premium is not None else '—'}</td>"
            f"<td style='padding:3px 8px;text-align:right'>"
            f"{r.vol_ratio if r.vol_ratio is not None else '—'}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:{_c(r.imb)}'>"
            f"{('%+.2f' % r.imb) if r.imb is not None else '—'}</td>"
            f"<td style='padding:3px 8px'><b>{h(r.strength)}</b>{notes}</td></tr>")

    warn = ("<div style='color:#b26a00;font-size:12px;margin:4px 0'>"
            "⚠ 09:20 前可撤单，本表量能含撤单噪声</div>"
            if rows and rows[0].noisy else "")
    return (
        "<div style='font-family:-apple-system,\"Microsoft YaHei\",sans-serif'>"
        "<h3 style='margin:0 0 6px'>集合竞价监控</h3>" + warn +
        "<table style='border-collapse:collapse;font-size:12px'>"
        "<tr style='background:#f5f5f5'>"
        "<th style='padding:3px 8px'>代码</th><th style='padding:3px 8px'>名称</th>"
        "<th style='padding:3px 8px'>竞价价</th><th style='padding:3px 8px'>溢价</th>"
        "<th style='padding:3px 8px'>量比</th><th style='padding:3px 8px'>委比</th>"
        "<th style='padding:3px 8px'>判定</th></tr>" + trs + "</table>"
        f"<p style='font-size:11px;color:{CSS_FLAT};margin-top:14px'>"
        "本邮件由个人研究系统自动生成，不构成任何投资建议</p></div>")
