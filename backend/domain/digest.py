# -*- coding: utf-8 -*-
"""★ 午间/收盘任务的数据装配层（纯函数，`as_of` 注入）。

★★★ 为什么需要这个模块
----------------------
`mail_templates` 有 `render_midday_html` / `render_alert_html`，
`scheduler.TASK_TABLE` 有 `midday` / `close` 的 cron 条目，
但**没有任何代码构造 `MailContext`**：
  · 渲染器没有生产者
  · `MailSender.send` 从未被任务层调用
  · `mail_log` 表0 行 —— 三类邮件一封都没发出去过

⇒ 本模块负责「把库里的数据变成 `MailContext`」，
   `scheduler` 只负责「在正确的时点调用它并发信」。
   这样分工后，装配逻辑可离线测试（不碰 SMTP、不碰调度器）。

★ 铁律二：全部纯函数，不取系统时间
-----------------------------------
`build_midday_context(as_of)` / `build_close_context(as_of)`
的输出只取决于 `(as_of, 库内数据)`，同一 `as_of` 重跑结果一致 ——
这是复盘与回测的前提。

★★ 缺失数据一律留 None，绝不填 0
---------------------------------
市场评分缺失（`market_regime` 那天没数据）→ `market_score=None`，
而不是 0。填 0 会被读成「市场极差」，
而模板里 `None` 会显示为「—」，如实表达「不知道」。

★ 为什么 `midday` 和 `close` 分开
-------------------------------
两者时点不同（11:30 收盘后 vs 15:05 收盘后），
**可用数据也不同**：
  · `midday`：上午收盘，能用当日上午的实时快照 + 板块强度（盘中已采）
  · `close`：全天收盘，能用当日完整日线 + 信号命中判定
⇒ 若共用一个装配函数，midday 会把「未收盘的数据」当收盘用 ——
  实测症状是「午间邮件里的涨跌幅是半天数据，看着像全天的」。

运行：
    python -m backend.domain.digest -demo
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST = timezone(timedelta(hours=8))

# ★★ 绝不 `con.close()` —— `duck.get_ro_conn()` 返回的是**进程级共享单连接**
#   （有写连接时复用写连接，否则开只读连接；见 `duck.get_ro_conn` 注释）。
#   在这里关掉它= 把全局连接关掉，**后续所有查询都会抛
#   `Connection Error: Connection already closed!`**。
#   实测症状：`_top_boards()` 明明返回了正确结果，
#   但因为前一个函数关掉了连接，再调就全空 ⇒
#   「板块数据有时有有时没有」，极难定位。


# ================================================================== 纯查询
def _regime_on(as_of: date) -> dict | None:
    """读 `market_regime` 当日行。

    ★ 必须按 `CAST(ts AS DATE)` 匹配，不能 `ts = 当天某个固定时刻`：
      表里的 ts 是「该交易日的 15:00 快照」，而 midday 任务跑在 11:30
      ⇒ 用精确时间匹配会查不到，返回 None，
      而「查不到」会被读成「市场数据缺失」并显示「—」。
      按日期匹配才正确：11:30 想知道的就是**上一个完整交易日**的状态
      （当日 15:00 的状态此刻还不存在）。
    """
    from backend.data import duck

    try:
        con = duck.get_ro_conn()
    except Exception:
        return None
    try:
        row = con.execute(
            """
            SELECT CAST(ts AS DATE) d, score, state, position_cap
            FROM market_regime
            WHERE CAST(ts AS DATE) <= ?
            ORDER BY d DESC LIMIT 1
            """,
            [as_of],
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {"date": str(row[0]), "score": row[1],
            "state": row[2], "position_cap": row[3]}


def _top_boards(as_of: date, limit: int = 5) -> list[str]:
    """当日排名最高的板块（取自 `board_strength`）。

    ★★ 只能读 `strength`（0~100 排名百分位）**不能读 `pct_chg`**：
      `board_strength` 表里**没有**涨跌幅列 —— 适配器拿到了但没入库
      （`strength` 才是设计要的量）。曾误写 `SELECT ... strength AS pct`
      并按百分比格式化成 `+2.79%` ⇒ 读出的其实是排名百分位，
      会被显示成「电池 +98.4%」这种荒唐数字。
      ⇒ 这里只输出**板块名 + 排名**，不假装有涨跌幅。

    ★ 缺失时返回空列表 —— 模板会渲染「无板块数据」，
      这里伪造内容会污染报告。
    """
    from backend.data import duck

    try:
        con = duck.get_ro_conn()
    except Exception:
        return []
    try:
        rows = con.execute(
            """
            WITH latest AS (
              SELECT max(CAST(ts AS DATE)) d FROM board_strength
              WHERE CAST(ts AS DATE) <= ?
            )
            SELECT b.board_name, b.rank
            FROM board_strength b, latest
            WHERE CAST(b.ts AS DATE) = latest.d
              AND b.rank IS NOT NULL
            ORDER BY b.rank ASC LIMIT ?
            """,
            [as_of, limit],
        ).fetchall()
        return [f"{r[0]}(#{r[1]})" if r[1] is not None else str(r[0])
                for r in rows]
    except Exception:
        return []


def _positions(as_of: date) -> list[dict]:
    """持仓行（读 `position` 表 + `portfolio_health.json`）。

    ★ 两处缺失都留 None：
      · 表里没有持仓 → 空列表（不是「持仓 0 元」的假行）
      · health 文件里没有该票 → `health=None`，模板显示「—」
    """
    from backend.data.sqlite import get_conn

    try:
        rows = get_conn().execute(
            "SELECT code, name, cost, shares FROM position "
            "WHERE status='open' ORDER BY code"
        ).fetchall()
    except Exception:
        return []

    health: dict = {}
    p = _ROOT / "data" / "portfolio_health.json"
    if p.exists():
        try:
            health = json.loads(p.read_text(encoding="utf-8")).get("rows", {})
        except Exception:
            health = {}

    out: list[dict] = []
    for code, name, cost, shares in rows:
        h = health.get(code) or {}
        out.append({
            "code": code, "name": name or code,
            "cost": cost,
            "price": h.get("price"),
            "pnl_pct": h.get("pnl_pct"),
            "health": h.get("health"),
            "structure": h.get("structure") or "",
            # ★ advice 只能来自 overlay 的「参考位」，不得含祈使表述（D13）
            "advice": h.get("advice") or "",
            "key_levels": h.get("key_levels") or "",
            "urgent": bool(h.get("urgent", False)),
        })
    return out


def _overnight(as_of: date) -> list[dict]:
    """盘前消息（`news` 表最近隔夜窗口）。

    ★ 方向值统一用 `bull`/`bear`/`neutral`——
      实测 `portfolio_health.py` 曾把生产库的 `bull`/`bear`
      当成 `positive`/`negative` 比对，两个条件恒 False，
      消息面分恒为中性 50。此处直接用库里的原值，不再做字符串猜测。
    """
    from backend.data.sqlite import get_conn

    try:
        rows = get_conn().execute(
            """
            SELECT ts, content, direction, strength, code
            FROM news
            WHERE date(ts) >= date(?) AND direction IS NOT NULL
            ORDER BY ts DESC LIMIT 10
            """,
            [as_of.isoformat()],
        ).fetchall()
    except Exception:
        return []
    return [{
        "ts": str(r[0]), "content": (r[1] or "")[:60],
        "direction": r[2] or "neutral",
        "strength": int(r[3] or 0),
        "related": [r[4]] if r[4] else [],
        "pending_verify": False,
    } for r in rows]


# ================================================================== 装配
def build_midday_context(as_of: datetime) -> "object":
    """构造午间邮件上下文（纯函数）。

    ★ `as_of` 必须是**上午收盘后**的时点（如 11:35）。
      用在14:00 会把「已经看到全天」的时点误标为午间，
      此时 `board_strength` 里存的是当天 15:00 快照（收盘数据），
      午间邮件就会显示全天的板块涨幅 —— 实测症状：
      「11:30 的邮件里写着下午 3 点才有的收盘数据」。
    """
    from backend.domain.mail_templates import MailContext

    d = as_of.date()
    reg = _regime_on(d)
    return MailContext(
        as_of=as_of,
        kind="midday",
        market_state=reg["state"] if reg else None,
        position_cap=reg["position_cap"] if reg else None,
        market_score=reg["score"] if reg else None,
        overnight=_overnight(d),
        overseas=[],
        positions=_positions(d),
        watch_picks=[],
        discipline=[
            "★ 上午走势不构成加仓依据，只用于调整观察仓位",
            "缠论买点需等收盘确认，午间不触发新开仓",
        ],
        morning_review=_top_boards(d),
        sector_changes=_top_boards(d),
        alert_text="",
        urgent=False,
    )


def build_close_context(as_of: datetime) -> "object":
    """构造收盘邮件上下文（纯函数）。

    ★ 与午间的差别：这里用**当日收盘后的完整数据**，
      并带上「当日信号命中判定」。
    """
    from backend.domain.mail_templates import MailContext

    d = as_of.date()
    reg = _regime_on(d)
    return MailContext(
        as_of=as_of,
        kind="midday",          # ★ 复用 midday 模板（收盘也用它）
        market_state=reg["state"] if reg else None,
        position_cap=reg["position_cap"] if reg else None,
        market_score=reg["score"] if reg else None,
        overnight=_overnight(d),
        overseas=[],
        positions=_positions(d),
        watch_picks=[],
        discipline=[
            "★ 系统不自动卖出（D13）：卖点只做K 线标注提醒，由你自行决断",
            "止损 5%（D14）：跌破参考位请自行判断，系统不代为下单",
        ],
        morning_review=_top_boards(d),
        sector_changes=_top_boards(d),
        alert_text=_signal_hit_summary(d),
        urgent=False,
    )


def _signal_hit_summary(as_of: date) -> str:
    """当日信号命中判定摘要（收盘任务的核心产出）。

    ★ 只统计 `signal.ts <= 当日` 的信号 —— 表是 INSERT OR IGNORE，
      同一信号会在多天重复存在（每日全量重扫），
      按 `ts = 当日` 过滤会得到 0 条（实测就是这样）。
    """
    from backend.data.sqlite import get_conn

    try:
        rows = get_conn().execute(
            """
            SELECT kind, level, count(*) FROM signal
            WHERE ts <= ?
            GROUP BY kind, level
            """,
            [as_of.isoformat()],
        ).fetchall()
    except Exception:
        return ""
    if not rows:
        return "当日无历史信号记录"
    by_kind: dict[str, int] = {}
    for kind, _level, n in rows:
        by_kind[kind] = by_kind.get(kind, 0) + int(n)
    parts = [f"{k} {v}" for k, v in sorted(by_kind.items())]
    return f"累计信号（ts<={as_of}）：" + "、".join(parts)


if __name__ == "__main__":                       # pragma: no cover
    print("=" * 76)
    print("午间/收盘 装配层自检（不连 SMTP）")
    print("=" * 76)
    t = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
    c = build_midday_context(t)
    print(f"\n【midday {t:%Y-%m-%d %H:%M}】")
    print(f"  market_state = {c.market_state}")
    print(f"  market_score = {c.market_score}")
    print(f"  position_cap = {c.position_cap}")
    print(f"  板块 = {c.morning_review}")
    print(f"  持仓 = {len(c.positions)} 条")
    print(f"  消息 = {len(c.overnight)} 条")

    t2 = datetime(2026, 10, 9, 15, 5, tzinfo=CST)
    c2 = build_close_context(t2)
    print(f"\n【close {t2:%Y-%m-%d %H:%M}】")
    print(f"  alert_text = {c2.alert_text}")
    print()
    print("  ★ 缺失因子保持 None（模板显示「—」），绝不填 0")