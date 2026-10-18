# -*- coding: utf-8 -*-
"""K 线标注渲染器（单文件 HTML，零依赖）。

★★★ 这个项目的定位约束（D13 锁定，不可违反）
------------------------------------------------
**系统是决策副驾，不是自动交易机器人。**
`overlay.build_overlay()` 产出的 `Mark` 里，卖点标记带 `advisory=True`，
止损位是「**参考位**」而非指令。所以本渲染器：

  ·卖点画成**空心上三角 + "提醒"** 标签，颜色用**灰蓝**（不是红色警报）
  · 买点画成**实心上三角**
  · 止损位画成**虚线 + 「参考位」**，且**三个价位并列展示**（不替用户选）
  · 全图固定显示 `overlay.disclaimer`

★ 一旦UI 里出现「建议卖出」「应立即减仓」这类祈使表述，
  系统就从「副驾」变成了「交易员」——
  这不是文案偏好，是项目定位的边界。`tests/test_kline_ui.py` 有断言守护。

★ 为什么自己画而不用图表库
--------------------------
项目无前端、无 node、无网络可用（实测多数行情源在本机被拦）。
与其引入无法安装的依赖，不如用**内联 SVG** 直接画 ——
单文件、离线可开、无版本风险。

★ 配色（中国股市习惯，涨红跌绿 —— 全局约定，不得反转）
--------------------------------------------------
涨 = 红 `#d94b4b` / 跌 = 绿 `#2f9e6e` / 中枢 = 灰带 / 买点 = 红实心 /
卖点提醒 = 灰蓝空心 / 止损参考 = 虚线灰。

运行：
    python scripts/render_kline.py 000001
    python scripts/render_kline.py 600519 --name贵州茅台 --out data/k_600519.html
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST = timezone(timedelta(hours=8))

# 画布
W, H = 1280, 720
PAD_L, PAD_R = 56, 24
PAD_T, PAD_B = 28, 210# 下方留给止损参考位与图例
PLOT_H = H - PAD_T - PAD_B

# 配色（涨红跌绿 —— 中国习惯）
C_UP = "#d94b4b"
C_DOWN = "#2f9e6e"
C_GRID = "#e8e8e8"
C_AXIS = "#888"
C_ZS = "rgba(120,120,140,0.10)"
C_ZS_EDGE = "rgba(120,120,140,0.45)"
C_BUY = "#d94b4b"
C_SELL = "#5a7a9a"          # ★ 灰蓝，不是红色 —— 卖点是提醒不是警报
C_STOP = "#9a9a9a"
C_TEXT = "#333"


def _esc(s) -> str:
    return html.escape(str(s))


def _bis_points(bis: list[dict], idx_of: dict[str, int]
                ) -> list[tuple[int, float]]:
    """笔序列 → 折线端点 [(bar_index, price), ...]（按时间正序）。

    ★★ `ov.bis` 是**已序列化的 dict**，键为
       `{dir, high, low, start_date, end_date}` —— **没有价格字段对**，
       端点要靠 `dir` 拼出来：
         dir='up'   → 起点 low、终点 high
         dir='down' → 起点 high、终点 low
       （曾误写 `b["start_price"]` ⇒ KeyError）

    相邻笔共享端点，故按x 递增去重。
    """
    seq: list[tuple[int, float]] = []
    for b in bis:
        i0 = idx_of.get(b.get("start_date"))
        i1 = idx_of.get(b.get("end_date"))
        if i0 is None or i1 is None:
            continue
        up = str(b.get("dir", "")).lower() != "down"
        p0 = float(b["low"] if up else b["high"])
        p1 = float(b["high"] if up else b["low"])
        for i, px in ((i0, p0), (i1, p1)):
            if not seq or seq[-1][0] != i:
                seq.append((i, px))
    return seq


def render(code: str, name: str, ov) -> str:
    """`KlineOverlay` → 单文件 HTML。"""
    bars = ov.bars
    if not bars:
        return "<!doctype html><meta charset='utf-8'><p>无 K 线数据</p>"

    n = len(bars)
    hi = max(b["high"] for b in bars)
    lo = min(b["low"] for b in bars)
    pad = (hi - lo) * 0.06 or hi * 0.06 or 1.0
    hi += pad
    lo -= pad

    plot_w = W - PAD_L - PAD_R
    cw = plot_w / n                      # 每根 K 线宽度

    def x_of(i: int) -> float:
        return PAD_L + cw * (i + 0.5)

    def y_of(px: float) -> float:
        return PAD_T + (hi - px) / (hi - lo) * PLOT_H

    idx_of = {b["date"]: i for i, b in enumerate(bars)}
    parts: list[str] = []

    # ---------------- CSS ----------------
    parts.append(f"""<style>
*{{box-sizing:border-box}}
body{{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  color:{C_TEXT};background:#fafafa}}
h1{{font-size:19px;margin:0 0 2px}}
.meta{{color:#777;font-size:12.5px;margin-bottom:10px}}
.meta b{{color:{C_TEXT}}}
.card{{background:#fff;border:1px solid #e6e6e6;border-radius:8px;padding:14px 16px}}
/* ★ 免责声明必须显眼 —— 系统给参考，不给指令 */
.disc{{margin-top:12px;padding:9px 12px;background:#fff8e6;border:1px solid #f0d9a0;
  border-radius:6px;font-size:12.5px;color:#8a6d1a}}
.legend{{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 0;font-size:12.5px}}
.legend i{{display:inline-block;width:11px;height:11px;margin-right:5px;
  vertical-align:-1px;border-radius:2px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:8px}}
th,td{{border:1px solid #eee;padding:5px 8px;text-align:left;vertical-align:top}}
th{{background:#f6f6f6;font-weight:600}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.hint{{color:#888;font-size:12px;margin-top:6px}}
</style>""")

    # ---------------- 头部 ----------------
    parts.append(f"""<div class="card">
<h1>{_esc(name)} <span style="color:#888;font-weight:400">{_esc(code)}</span></h1>
<div class="meta">
  数据截至 <b>{_esc(ov.as_of)}</b> ｜ K 线 <b>{n}</b> 根 ｜ 笔 <b>{len(ov.bis)}</b> 段
  ｜ 中枢 <b>{len(ov.zhongshus)}</b> 个 ｜ 当前位置 <b>{_esc(ov.position)}</b>
</div>""")

    # ---------------- SVG ----------------
    svg: list[str] = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}">']

    # 横向网格 + 右侧价格轴
    for k in range(5):
        price = lo + (hi - lo) * k / 4
        y = y_of(price)
        svg.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                   f'stroke="{C_GRID}" stroke-width="1"/>')
        svg.append(f'<text x="{W - PAD_R + 4}" y="{y + 4:.1f}" font-size="11" '
                   f'fill="{C_AXIS}">{price:.2f}</text>')

    # ---- 中枢矩形（画在 K 线之下）----
    for z in ov.zhongshus:
        i0 = idx_of.get(z.get("start_date"))
        i1 = idx_of.get(z.get("end_date"))
        if i0 is None or i1 is None or i1 < i0:
            continue
        x0, x1 = x_of(i0) - cw / 2, x_of(i1) + cw / 2
        y0, y1 = y_of(z["high"]), y_of(z["low"])
        svg.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{max(1.0, x1 - x0):.1f}" '
                   f'height="{max(1.0, y1 - y0):.1f}" fill="{C_ZS}" '
                   f'stroke="{C_ZS_EDGE}" stroke-width="1" stroke-dasharray="3 2"/>')

    # ---- 笔连线 ----
    seq = _bis_points(ov.bis or [], idx_of)
    if len(seq) >= 2:
        d = " ".join(("M" if j == 0 else "L") + f"{x_of(i):.1f},{y_of(p):.1f}"
                     for j, (i, p) in enumerate(seq))
        svg.append(f'<path d="{d}" fill="none" stroke="#5a7a9a" stroke-width="1.3" '
                   f'opacity="0.85"/>')

    # ---- K 线 ----
    bw = max(1.0, min(9.0, cw * 0.62))
    for i, b in enumerate(bars):
        up = b["close"] >= b["open"]
        col = C_UP if up else C_DOWN
        x = x_of(i)
        yo, yc = y_of(b["open"]), y_of(b["close"])
        yh, yl = y_of(b["high"]), y_of(b["low"])
        svg.append(f'<line x1="{x:.1f}" y1="{yh:.1f}" x2="{x:.1f}" y2="{yl:.1f}" '
                   f'stroke="{col}" stroke-width="1"/>')
        top, hgt = min(yo, yc), max(abs(yc - yo), 1.0)
        if up:      # A 股：阳线空心（红框）
            svg.append(f'<rect x="{x - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                       f'height="{hgt:.1f}" fill="#fff" stroke="{col}" stroke-width="1"/>')
        else:       # 阴线实心
            svg.append(f'<rect x="{x - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                       f'height="{hgt:.1f}" fill="{col}" stroke="{col}" stroke-width="1"/>')

    # ---- 标注 ----
    stops = [m.price for m in ov.current_stop_refs if m.price]
    mark_rows = []
    for m in ov.marks:
        i = idx_of.get(m.date)
        if i is None:
            continue
        x, y = x_of(i), y_of(m.price)
        if m.kind == "buy":
            # 买点：实心上三角（朝下，标在低点下方）
            tri = (x, y + 16), (x - 6.5, y + 28), (x + 6.5, y + 28)
            svg.append(f'<polygon points="{" ".join(f"{a:.1f},{b_:.1f}" for a, b_ in tri)}" '
                       f'fill="{C_BUY}" stroke="#fff" stroke-width="1.2"/>')
            svg.append(f'<text x="{x:.1f}" y="{y + 40:.1f}" font-size="11" fill="{C_BUY}" '
                       f'text-anchor="middle" font-weight="600">买</text>')
        else:
            # ★ 卖点：空心**上**三角 + 灰蓝色 —— 是「提醒」不是「警报」
            tri = (x, y - 16), (x - 6.5, y - 28), (x + 6.5, y - 28)
            svg.append(f'<polygon points="{" ".join(f"{a:.1f},{b_:.1f}" for a, b_ in tri)}" '
                       f'fill="#fff" stroke="{C_SELL}" stroke-width="1.6"/>')
            svg.append(f'<text x="{x:.1f}" y="{y - 33:.1f}" font-size="11" fill="{C_SELL}" '
                       f'text-anchor="middle">卖·提醒</text>')
        mark_rows.append(m)

    # ---- 止损参考位（虚线，三档并列）----
    for m in ov.current_stop_refs:
        if not m.price:
            continue
        y = y_of(m.price)
        if PAD_T <= y <= PAD_T + PLOT_H:
            svg.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                       f'stroke="{C_STOP}" stroke-width="1" stroke-dasharray="6 4"/>')
            svg.append(f'<text x="{PAD_L + 4}" y="{y - 4:.1f}" font-size="11" '
                       f'fill="{C_STOP}">参考位 {m.price:.2f}</text>')

    # 成交量副图
    vol_top = PAD_T + PLOT_H + 18
    vol_h = 56
    vmax = max((b.get("volume") or 0) for b in bars) or 1
    for i, b in enumerate(bars):
        v = b.get("volume") or 0
        bh = vol_h * v / vmax
        x = x_of(i)
        col = C_UP if b["close"] >= b["open"] else C_DOWN
        svg.append(f'<rect x="{x - bw / 2:.1f}" y="{vol_top + vol_h - bh:.1f}" '
                   f'width="{bw:.1f}" height="{max(0.6, bh):.1f}" fill="{col}" opacity="0.55"/>')
    svg.append(f'<line x1="{PAD_L}" y1="{vol_top + vol_h:.1f}" x2="{W - PAD_R}" '
               f'y2="{vol_top + vol_h:.1f}" stroke="{C_AXIS}" stroke-width="1"/>')

    # 日期轴
    for i, b in enumerate(bars):
        if i % max(1, n // 8) == 0:
            svg.append(f'<text x="{x_of(i):.1f}" y="{H - PAD_B + 34:.1f}" font-size="11" '
                       f'fill="{C_AXIS}" text-anchor="middle">{_esc(b["date"][2:])}</text>')

    svg.append("</svg>")
    parts.append("".join(svg))

    # ---------------- 图例 ----------------
    parts.append(f"""<div class="legend">
<span><i style="background:{C_UP}"></i>阳线（涨）</span>
<span><i style="background:{C_DOWN}"></i>阴线（跌）</span>
<span><i style="background:{C_BUY}"></i>买点</span>
<span><i style="background:#fff;border:1.6px solid {C_SELL}"></i>卖点<b>提醒</b>（非指令）</span>
<span><i style="background:{C_ZS};border:1px dashed {C_ZS_EDGE}"></i>中枢</span>
<span><i style="background:transparent;border-top:1px dashed {C_STOP};height:0"></i>止损参考位</span>
</div>""")

    # ---------------- 标注明细 ----------------
    if mark_rows:
        parts.append("<table><tr><th>日期</th><th>类型</th><th>价格</th>"
                     "<th>档位</th><th>信号</th><th>置信</th><th>依据</th></tr>")
        for m in mark_rows:
            kind = ("<b style='color:%s'>买点</b>" % C_BUY) if m.kind == "buy" \
                else ("<span style='color:%s'>卖点<b>提醒</b></span>" % C_SELL)
            parts.append(
                f"<tr><td class='n'>{_esc(m.date)}</td><td>{kind}</td>"
                f"<td class='n'>{m.price:.2f}</td><td>{_esc(m.level)}</td>"
                f"<td>{_esc(m.signal_kind)}</td>"
                f"<td class='n'>{m.confidence:.2f}</td>"
                f"<td>{_esc(m.reason)}</td></tr>")
        parts.append("</table>")
        parts.append("<div class='hint'>★ 卖点为<b>提醒</b>：系统不自动卖出，"
                     "卖出与否由你决定（D13）。</div>")

    # ---------------- 止损参考位 ----------------
    if ov.current_stop_refs:
        parts.append("<table style='margin-top:10px'><tr><th>参考位</th><th>依据</th>"
                     "<th>含义与代价</th></tr>")
        for m in ov.current_stop_refs:
            parts.append(
                f"<tr><td class='n'>{m.price:.2f}</td><td>{_esc(m.stop_basis)}</td>"
                f"<td>{_esc(m.stop_note)}</td></tr>")
        parts.append("</table>")
        parts.append("<div class='hint'>★ 三档<b>并列</b>展示，不替你选："
                     "信号意义 / 幅度适中 / 震荡市适用，各有代价。</div>")

    # ---------------- 免责声明 ----------------
    parts.append(f"<div class='disc'>⚠ {_esc(ov.disclaimer)}</div>")
    parts.append("</div>")

    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>K线标注 " + _esc(code) + "</title>" + parts[0]
            + "</head><body>" + "".join(parts[1:]) + "</body></html>")


# ================================================================== CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="渲染 K 线标注（单文件 HTML）")
    ap.add_argument("code")
    ap.add_argument("--name", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--show-weak", action="store_true", help="含 weak 档买点")
    ap.add_argument("--as-of", default="", help="默认取库里最后交易日")
    args = ap.parse_args()

    import duckdb

    from backend.domain import chan, overlay as ov

    pq = _ROOT / "data" / "snapshots" / "2026-10-08" / "kline_daily.parquet"
    if not pq.exists():
        print(f"Parquet 不存在: {pq}")
        return 1
    con = duckdb.connect()
    rows = con.execute(
        f"""SELECT trade_date, open, high, low, close, volume FROM
            read_parquet('{pq.as_posix()}') WHERE code = ?
            ORDER BY trade_date""", [args.code]).fetchall()
    if not rows:
        print(f"无 {args.code} 的 K 线")
        return 1
    bars = [{"trade_date": str(r[0]), "open": r[1], "high": r[2],
             "low": r[3], "close": r[4], "volume": r[5]} for r in rows[-args.limit:]]
    as_of = args.as_of or str(bars[-1]["trade_date"])[:10]

    res = chan.analyze(args.code, bars, as_of)
    o = ov.build_overlay(args.code, args.name or args.code, bars, res, as_of,
                         show_weak=args.show_weak)
    page = render(args.code, args.name or args.code, o)

    out = Path(args.out) if args.out else (_ROOT / "data" / f"kline_{args.code}.html")
    out.write_text(page, encoding="utf-8")
    con.close()
    print(f"✔ {out}")
    print(f"  K线 {len(o.bars)} 根｜笔 {len(o.bis)}｜中枢 {len(o.zhongshus)}｜"
          f"标注 {len(o.marks)}｜止损参考 {len(o.current_stop_refs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())