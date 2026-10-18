# -*- coding: utf-8 -*-
"""★ 周报：策略批量回测 + 稳定性结论（规格 6.1 周六 10:00）。

============================ 为什么周报必须报「不稳定」=========================
本项目已踩过的最贵一次误判（M9）：
    样本量30/60 只时样本外平均 **+0.51%**、门槛**通过**；
    扩到 200 只变成 **-0.15%**、**不通过**；
    再扩到 400 只又变成 **+0.44%**、**又通过**。
⇒ 收益在 0 附近来回穿越。**只报一个样本量的结果 = 在 7 个数字里挑好看的**。

⇒ 本模块的**唯一立场**：`verdict == "unstable"` 时，
   报告里必须**显式写明「没有稳定优势」**，
   绝不允许出现「通过」「有效」这类容易被读成肯定结论的措辞。

=============================== 为什么纯函数 ===============================
铁律二：领域计算必须 `f(输入, as_of, params) → 输出`。
周报要对比「本周 vs 上周」「本周 vs 基准」，
若内部调`datetime.now()` 就**无法复现**——
而周报恰恰是最需要被复核的东西（它会影响实盘决策）。
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass  # noqa: F401  (供后续扩展的结构化上下文)
from datetime import datetime, timedelta
from pathlib import Path

# ★ 全局配色约定：涨=红，跌=绿（中国股市习惯），不得反转
CSS_UP = "#d94b4b"
CSS_DOWN = "#2e9e5b"
CSS_FLAT = "#888"


# ────────────────────────────────────────────────── 纯函数：稳定性判定
def stability_verdict(rows: list[dict]) -> dict:
    """由多档样本量结果判定「方向是否可信」。

    ★ 与 `scripts/run_oos.stability_check` 的判定口径**保持一致**：
      「各档样本外平均收益符号是否一致」。
      这里单独实现而不是 import，是为了让本模块**零依赖**可单测
      （run_oos 依赖 DuckDB 连接）。

    ★ 判据只用 `avg_ret`（平均收益），不用 `winrate`：
      胜率在方向不一致时会互相抵消（历史教训 #46）。
    """
    valid = [r for r in rows
             if r.get("avg_ret") is not None and r.get("oos_n")]
    if not valid:
        return {"verdict": "no_data", "n_pass": 0, "n_total": 0,
                "note": "★ 无有效样本，无法判定稳定性。",
                "rets": []}
    rets = [float(r["avg_ret"]) for r in valid]
    signs = {1 if x > 0 else -1 for x in rets}
    npass = sum(1 for x in rets if x > 0)
    lo, hi = min(rets), max(rets)

    if len(signs) == 1:
        v = "stable_positive" if signs == {1} else "stable_negative"
        note = (f"各档样本外平均收益均为{'正' if 1 in signs else '负'}，方向一致")
    else:
        v = "unstable"
        note = (f"★ 各档样本外平均收益在 0 附近**来回穿越**"
                f"（{lo:+.2f}% ~ {hi:+.2f}%），{npass}/{len(valid)} 档为正 ⇒ "
                f"**没有稳定优势，单点「通过」是小样本巧合**")

    return {"verdict": v, "n_pass": npass, "n_total": len(valid),
            "note": note, "rets": rets, "lo": lo, "hi": hi}


# ────────────────────────────────────────────────── 纯函数：结论措辞
#★ 这张表是本模块最重要的一部分。
#   措辞不是「风格问题」——把unstable 说成「基本有效」，
#   会让读者以为策略可用，而它并不可用。
VERDICT_TEXT = {
    "stable_positive": ("✔ 有稳定优势",
                        "各档样本外收益方向一致为正。仍建议先小仓位实盘观察。"),
    "stable_negative": ("✘ 无优势（方向稳定为负）",
                        "各档样本外收益方向一致为负，当前入场逻辑不应使用。"),
    "unstable":        ("✘ 不显著（没有稳定优势）",
                        "样本外收益随样本量在 0 附近来回穿越，"
                        "**不能据此认为策略有效**。需改进入场/出场逻辑，不是调参数。"),
    "no_data":         ("? 数据不足",
                        "样本不足以做稳定性判定，本次不给出方向性结论。"),
}


def verdict_lines(verdict: str) -> tuple[str, str]:
    """→ (标题, 说明)。★ 未知 verdict 不得静默当成「通过」。"""
    if verdict not in VERDICT_TEXT:
        return ("? 未知结论",
                f"⚠ 未识别的判定值 {verdict!r}，**不得当作通过处理**。")
    return VERDICT_TEXT[verdict]


# ────────────────────────────────────────────────── 纯函数：区间统计
def summarize_signals(rows: list[dict]) -> dict:
    """本周信号按 kind + level 分档统计。

    ★★ 必须分档（历史教训 #56）：3buy/strong 胜率 86.2%、
      3buy/weak 胜率 20.0% —— 混算不仅失真，
      还会因为两档混入比例不同而得出**完全相反**的结论。
    """
    buckets: dict[tuple[str, str], list] = {}
    for r in rows:
        key = (str(r.get("kind") or "?"), str(r.get("level") or "?"))
        buckets.setdefault(key, []).append(r)

    out = []
    for (kind, level), items in sorted(buckets.items()):
        n = len(items)
        wins = [i for i in items if (i.get("ret_pct") or 0) > 0]
        rets = [float(i["ret_pct"]) for i in items
                if i.get("ret_pct") is not None]
        # ★ 盈亏比在无亏损样本时会除零 ⇒ None（∞ 无意义），不是 0
        loss_rets = [x for x in rets if x <= 0]
        gain_rets = [x for x in rets if x > 0]
        pl = (statistics.fmean(gain_rets) / abs(statistics.fmean(loss_rets))
              if loss_rets and gain_rets else None)
        out.append({
            "kind": kind, "level": level, "n": n,
            "winrate": round(100.0 * len(wins) / n, 1) if n else None,
            "avg_ret": round(statistics.fmean(rets), 2) if rets else None,
            "pl_ratio": round(pl, 2) if pl is not None else None,
        })
    return {"buckets": out, "total": len(rows)}


# ────────────────────────────────────────────────── 纯函数：本周概览
def build_weekly_ctx(as_of: datetime) -> dict:
    """装配周报上下文（纯读取，as_of 注入）。

    ★ 时间窗用**交易日**而非自然日：周六凌晨回测时，
      「本周」若按自然日算会把上周日的消息算进来。
      这里的 `week_start` 是 `as_of` 所在周的周一。
    """
    from backend.data.sqlite import get_conn

    con = get_conn()
    d = as_of.date()
    monday = d - timedelta(days=d.weekday())
    # ★ 窗口右端取「昨天」：周报生成时当天（周六）无行情
    end = d - timedelta(days=1)
    lo, hi = monday.isoformat(), end.isoformat()

    def q(sql: str, args=()) -> list:
        # ★ `get_conn()` 返回的是 **sqlite3.Connection**（不是 DuckDB），
        #   `execute()` 直接产出 rows，**没有 `.fetchall()`**。
        #   写成 `con.execute(...).fetchall()` 会抛
        #   `AttributeError: 'list' object has no attribute 'fetchall'`。
        return list(con.execute(sql, args).fetchall())

    # ① 信号命中
    sig_rows = q("""
        SELECT kind, level, code, ts FROM signal
        WHERE substr(ts,1,10) BETWEEN ? AND ?
    """, (lo, hi))

    # ② 持仓与自选
    pos = q("SELECT code, shares, cost FROM position"
            " WHERE status='holding' AND shares>0")
    watch = q("SELECT code FROM watchlist"
              " WHERE add_date IS NULL OR substr(add_date,1,10)<=?"
              " ORDER BY priority DESC, id LIMIT 20", (hi,))

    # ③ 消息统计（按方向分组）
    # ★ `news` 的时间列叫 **`ts`**，不叫 `pub_time`（`corpus` 才是 pub_time）。
    #   猜列名会抛 `no such column` —— 而这错误只在真的有周报时才暴露。
    news = q("SELECT direction, count(*) FROM news"
             " WHERE substr(ts,1,10) BETWEEN ? AND ?"
             " GROUP BY direction", (lo, hi))

    # ④ 市场状态取本周最后一天
    # ★★ `market_regime` 在 **DuckDB**（`backend/data/duck.py`），
    #   不在 SQLite —— 业务表与行情表分属两个库。
    #   在 SQLite 里查它会抛 `no such table`，且这个错误**只在有周报时才暴露**。
    reg_rows: list = []
    try:
        from backend.data import duck

        dcon = duck.get_ro_conn()          # ★ 只读连接，绝不可 close()
        reg_rows = dcon.execute(
            "SELECT state, score, position_cap, ts FROM market_regime"
            " WHERE CAST(ts AS DATE) <= ? ORDER BY ts DESC LIMIT 1",
            [hi]).fetchall()
    except Exception as exc:
        # ★ 查不到**不抛异常**：周报是「报告」，缺一项不该整篇失败。
        #   但必须在报告里显式标注（见下方 regime=None 时的渲染）。
        reg_rows = []
        _regime_err = f"{type(exc).__name__}: {exc}"
    else:
        _regime_err = ""

    return {
        "week_start": lo,
        "week_end": hi,
        "as_of": as_of.isoformat(timespec="seconds"),
        "signals": [{"kind": r[0], "level": r[1], "code": r[2], "ts": r[3]}
                    for r in sig_rows],
        "positions": [{"code": r[0], "shares": r[1], "cost": r[2]}
                      for r in pos],
        "watch": [r[0] for r in watch],
        "news": {r[0]: r[1] for r in news},
        "regime": ({"state": reg_rows[0][0], "score": reg_rows[0][1],
                    "cap": reg_rows[0][2], "ts": str(reg_rows[0][3])}
                   if reg_rows else None),
        # ★ 失败原因单独带出：渲染时能说清是「本周无记录」
        #   还是「查库失败」，两者对读者的含义完全不同。
        "regime_error": _regime_err,
    }


# ────────────────────────────────────────────────── 纯函数：渲染
def _pct_color(v: float | None) -> str:
    if v is None:
        return CSS_FLAT
    return CSS_UP if v > 0 else (CSS_DOWN if v < 0 else CSS_FLAT)


def render_weekly_text(ctx: dict, stability: dict) -> str:
    """周报纯文本版（★ 涨红跌绿靠标记，文本版用符号）。"""
    L: list[str] = []
    L.append("=" * 60)
    L.append(f"Hawk 周报  {ctx['week_start']} ~ {ctx['week_end']}")
    L.append("=" * 60)

    reg = ctx.get("regime")
    if reg:
        L.append(f"市场状态  {reg['state']}（{reg['score']:.1f} 分）"
                 f"  仓位上限 {reg['cap']:.0%}")
    else:
        # ★ 查不到状态必须**显式说明**，不能静默留空 ——
        #   收件人会以为「本周无状态变化」，实际是数据缺失。
        err = ctx.get("regime_error") or ""
        L.append(f"市场状态  ⚠ 无数据（{err or '本周 market_regime 无记录'}）")
    L.append("")

    L.append(f"本周信号  {len(ctx['signals'])} 条")
    if ctx["signals"]:
        kinds: dict[str, int] = {}
        for s in ctx["signals"]:
            kinds[f"{s['kind']}/{s['level']}"] = kinds.get(
                f"{s['kind']}/{s['level']}", 0) + 1
        for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
            L.append(f"    {k:24s} {n:>4d}")
    L.append("")

    if ctx.get("news"):
        n = ctx["news"]
        L.append(f"本周消息  利好 {n.get('positive', 0)} / "
                 f"利空 {n.get('negative', 0)} / "
                 f"中性 {n.get('neutral', 0)}")
        L.append("")

    L.append(f"持仓 {len(ctx['positions'])} 只 · 自选 {len(ctx['watch'])} 只")
    L.append("")

    # ---- 回测稳定性（核心结论）----
    title, note = verdict_lines(stability.get("verdict", "no_data"))
    L.append("-" * 60)
    L.append(f"策略回测  {title}")
    L.append(f"  {note}")
    rows = stability.get("rows") or []
    if rows:
        L.append("")
        L.append(f"  {'标的':>6s} {'样本外':>7s} {'胜率':>8s} "
                 f"{'平均收益':>10s} {'最差':>9s}")
        for r in rows:
            wr = r.get("winrate")
            wr_s = f"{wr:.1%}" if wr is not None else "—"
            ar = r.get("avg_ret")
            ar_s = f"{ar:+.2f}%" if ar is not None else "—"
            wst = r.get("worst")
            wst_s = f"{wst:+.2f}%" if wst is not None else "—"
            L.append(f"  {r.get('codes', 0):>6d} {r.get('oos_n', 0):>7d} "
                     f"{wr_s:>8s} {ar_s:>10s} {wst_s:>9s}")
    L.append("-" * 60)
    L.append("")
    L.append("本邮件由个人研究系统自动生成，不构成任何投资建议")
    return "\n".join(L)


def render_weekly_html(ctx: dict, stability: dict) -> str:
    """周报 HTML 版。★ 涨=红，跌=绿。"""
    import html

    def h(x) -> str:
        return html.escape(str(x if x is not None else ""))

    parts: list[str] = []
    parts.append(
        "<div style='font-family:-apple-system,\"Microsoft YaHei\",sans-serif'>")
    parts.append(
        f"<h2 style='margin:0 0 4px'>Hawk 周报 "
        f"<span style='font-size:13px;color:{CSS_FLAT}'>"
        f"{h(ctx['week_start'])} ~ {h(ctx['week_end'])}</span></h2>")

    reg = ctx.get("regime")
    if reg:
        parts.append(
            f"<div style='margin:6px 0;font-size:13px'>市场状态 "
            f"<b>{h(reg['state'])}</b>（{reg['score']:.1f} 分）"
            f"，仓位上限 <b>{reg['cap']:.0%}</b></div>")
    else:
        parts.append(
            f"<div style='margin:6px 0;font-size:13px;color:{CSS_FLAT}'>"
            f"⚠ 市场状态无数据（"
            f"{h(ctx.get('regime_error') or '本周 market_regime 无记录')}"
            f"）</div>")

    # 信号分档表
    parts.append("<h3 style='font-size:14px;margin:14px 0 4px'>本周信号</h3>")
    if ctx["signals"]:
        agg: dict[str, int] = {}
        for s in ctx["signals"]:
            k = f"{s['kind']} / {s['level']}"
            agg[k] = agg.get(k, 0) + 1
        trs = "".join(
            f"<tr><td style='padding:3px 8px'>{h(k)}</td>"
            f"<td style='padding:3px 8px;text-align:right'>{n}</td></tr>"
            for k, n in sorted(agg.items(), key=lambda x: -x[1]))
        parts.append(f"<table style='border-collapse:collapse;font-size:13px'>"
                     f"{trs}</table>")
    else:
        parts.append(f"<div style='font-size:13px;color:{CSS_FLAT}'>"
                     f"本周无信号落库</div>")

    # 回测稳定性
    title, note = verdict_lines(stability.get("verdict", "no_data"))
    color = CSS_UP if stability.get("verdict") == "stable_positive" else CSS_FLAT
    parts.append("<h3 style='font-size:14px;margin:16px 0 4px'>"
                 "策略批量回测</h3>")
    parts.append(
        f"<div style='font-size:14px;font-weight:600;color:{color}'>"
        f"{h(title)}</div>")
    parts.append(f"<div style='font-size:12px;color:#555;margin:2px 0 6px'>"
                 f"{h(note)}</div>")

    rows = stability.get("rows") or []
    if rows:
        trs = ""
        for r in rows:
            ar = r.get("avg_ret")
            wr = r.get("winrate")
            trs += (
                f"<tr>"
                f"<td style='padding:3px 8px;text-align:right'>"
                f"{r.get('codes', 0)}</td>"
                f"<td style='padding:3px 8px;text-align:right'>"
                f"{r.get('oos_n', 0)}</td>"
                f"<td style='padding:3px 8px;text-align:right'>"
                f"{('%.1f%%' % (wr * 100)) if wr is not None else '—'}</td>"
                f"<td style='padding:3px 8px;text-align:right;"
                f"color:{_pct_color(ar)}'>"
                f"{('%+.2f%%' % ar) if ar is not None else '—'}</td>"
                f"<td style='padding:3px 8px;text-align:right;"
                f"color:{_pct_color(r.get('worst'))}'>"
                f"{('%+.2f%%' % r['worst']) if r.get('worst') is not None else '—'}"
                f"</td></tr>")
        parts.append(
            "<table style='border-collapse:collapse;font-size:12px'>"
            "<tr style='background:#f5f5f5'>"
            "<th style='padding:3px 8px'>标的</th>"
            "<th style='padding:3px 8px'>样本外</th>"
            "<th style='padding:3px 8px'>胜率</th>"
            "<th style='padding:3px 8px'>平均收益</th>"
            "<th style='padding:3px 8px'>最差</th></tr>"
            f"{trs}</table>")

    parts.append(
        f"<p style='font-size:11px;color:{CSS_FLAT};margin-top:18px'>"
        f"本邮件由个人研究系统自动生成，不构成任何投资建议</p>")
    parts.append("</div>")
    return "".join(parts)


# ────────────────────────────────────────────────── 落库
def save_weekly_report(ctx: dict, stability: dict) -> str:
    """→ 落一份 JSON 到 `data/weekly_report.json`（可复核，覆盖式）。"""
    out = _root() / "data" / "weekly_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"context": ctx, "stability": stability},
        ensure_ascii=False, indent=2, default=str), "utf-8")
    return str(out)


def _root() -> Path:
    return Path(__file__).resolve().parent.parent.parent
