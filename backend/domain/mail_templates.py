"""邮件模板（附录 5.1）。

三类邮件：
  ① 盘前策略 09:00（隔夜外围 / 夜间消息 / 大盘仓位 / 持仓应对表/ 自选Top5 / 纪律提醒）
  ② 午间策略 12:00（上午回顾 / 消息影响 / 板块变化 / 下午持仓应对 /【紧急】前缀）
  ③ 实时告警（事件驱动：重大利空 / 缠论卖点 / 触及止损 / 数据源故障）

★ 三条硬性要求（来自 5.2/ 5.3，不是我的偏好）：
1. **只有持仓股的消息触发邮件**，自选股只做 UI 标记 —— 用户明确需求。
2. 免打扰 23:00–07:30，时段内消息聚合不即时发；**strength=5 重大利空穿透**。
3. HTML 单列布局、表格 ≤600px、涨红跌绿、底部免责声明。

★ 渲染是纯函数：同样的输入必得同样的输出（便于复盘比对）。
"""
from __future__ import annotations

import html
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                        # noqa: E402

from backend.common.mailer import MailSender, render_text_mail                # noqa: E402

DISCLAIMER = "本邮件由个人研究系统自动生成，不构成任何投资建议"

# A 股习惯：涨红跌绿（与欧美相反）
CSS_UP = "#c62828"      # 涨
CSS_DOWN = "#2e7d32"    # 跌
CSS_FLAT = "#616161"


def _cls(v: float | None) -> str:
    if v is None:
        return "flat"
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def _pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def _color(v: float | None) -> str:
    if v is None:
        return CSS_FLAT
    return CSS_UP if v > 0 else (CSS_DOWN if v < 0 else CSS_FLAT)


# ---------------------------------------------------------------- 数据结构
@dataclass
class PositionRow:
    """持仓行。字段与附录 5.1① 的表格列一一对应。"""
    code: str
    name: str
    cost: float | None
    price: float | None
    pnl_pct: float | None
    health: float | None
    structure: str                      # 结构状态，如 "30m 中枢上沿"
    advice: str                         # 今日建议
    key_levels: str = ""                # 关键价位，如 "支撑 12.90 / 压力 13.60"
    urgent: bool = False                # 触发卖点 → 主题加【紧急】前缀


@dataclass
class WatchPick:
    code: str
    name: str
    reason: str
    trigger: str                        # 触发条件


@dataclass
class OvernightItem:
    ts: str
    content: str
    direction: str                      # positive | negative | neutral
    strength: int                       # 1-5
    related: list[str] = field(default_factory=list)
    pending_verify: bool = True         # 待验证标记


@dataclass
class MailContext:
    as_of: datetime
    kind: str                           # premarket | midday | alert
    market_state: str = ""              # 市场状态，如"修复"
    position_cap: float | None = None   # 建议仓位上限，如 0.40
    market_score: float | None = None
    overnight: list[OvernightItem] = field(default_factory=list)
    overseas: list[str] = field(default_factory=list)      # 隔夜外围
    positions: list[PositionRow] = field(default_factory=list)
    watch_picks: list[WatchPick] = field(default_factory=list)
    discipline: list[str] = field(default_factory=list)    # 今日纪律
    morning_review: list[str] = field(default_factory=list)
    sector_changes: list[str] = field(default_factory=list)
    alert_text: str = ""
    urgent: bool = False


# ---------------------------------------------------------------- HTML 渲染
def _h(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def render_premarket_html(ctx: MailContext) -> str:
    """盘前策略邮件（附录 5.1 ①）。"""
    sections: list[tuple[str, str]] = []

    # ① 隔夜外围
    if ctx.overseas:
        rows = "".join(f"<tr><td style='padding:4px 8px'>{_h(x)}</td></tr>"
                       for x in ctx.overseas)
        sections.append(("隔夜外围市场", f"<table width='100%'>{rows}</table>"))

    # ② 非交易时段累积消息（按利好/利空分组）
    if ctx.overnight:
        for direction, label, color in (
            ("positive", "夜间利好消息", CSS_UP),
            ("negative", "夜间利空消息", CSS_DOWN),
        ):
            items = [x for x in ctx.overnight if x.direction == direction]
            if not items:
                continue
            rows = "".join(
                f"<tr><td style='padding:4px 8px;color:#888;white-space:nowrap'>{_h(x.ts[5:16])}</td>"
                f"<td style='padding:4px 8px'>{_h(x.content)}</td>"
                f"<td style='padding:4px 8px;color:{CSS_FLAT}'>强度{x.strength}</td>"
                f"<td style='padding:4px 8px;color:#666'>{_h('、'.join(x.related) or '—')}</td>"
                f"<td style='padding:4px 8px;color:#e07b00'>"
                f"{'待验证' if x.pending_verify else ''}</td></tr>"
                for x in items)
            sections.append((
                f"{label}（{len(items)} 条）",
                f"<table width='100%' style='border-collapse:collapse'>"
                f"<tr style='background:#f5f5f5'><th align='left' style='padding:4px 8px'>时间</th>"
                f"<th align='left' style='padding:4px 8px'>内容</th>"
                f"<th align='left' style='padding:4px 8px'>强度</th>"
                f"<th align='left' style='padding:4px 8px'>关联标的</th>"
                f"<th align='left' style='padding:4px 8px'>状态</th></tr>"
                f"{rows}</table>"))
        neutral = [x for x in ctx.overnight if x.direction == "neutral"]
        if neutral:
            rows = "".join(
                f"<tr><td style='padding:4px 8px;color:#888;white-space:nowrap'>{_h(x.ts[5:16])}</td>"
                f"<td style='padding:4px 8px'>{_h(x.content)}</td></tr>"
                for x in neutral[:8])
            sections.append((f"其他消息（{len(neutral)} 条，仅列前 8）",
                             f"<table width='100%'>{rows}</table>"))

    # ③ 大盘环境 + 仓位上限
    cap_txt = (f"{ctx.position_cap:.0%}" if ctx.position_cap is not None else "—")
    # ★ 修复：market_score 可能为 None（大盘数据缺失/降级时），
    #   原实现 `{ctx.market_score:.1f}` 直接抛
    #   TypeError: unsupported format string passed to NoneType。
    #   这会让整封盘前邮件渲染失败 —— 数据缺失时正是最需要收到邮件的时候。
    score_txt = f"{ctx.market_score:.1f}" if ctx.market_score is not None else "—"
    env_rows = (f"<tr><td style='padding:4px 8px'>市场状态</td>"
                f"<td style='padding:4px 8px;font-weight:bold'>{_h(ctx.market_state or '—')}</td>"
                f"<td style='padding:4px 8px'>综合评分</td>"
                f"<td style='padding:4px 8px'>{score_txt}</td>"
                f"<td style='padding:4px 8px'>建议仓位上限</td>"
                f"<td style='padding:4px 8px;color:#c62828;font-weight:bold'>{cap_txt}</td></tr>")
    sections.append(("大盘环境判定", f"<table width='100%'>{env_rows}</table>"))

    # ④ 持仓逐只应对（附录 5.1 ① 表格）
    if ctx.positions:
        rows = ""
        for p in ctx.positions:
            hcolor = (CSS_DOWN if (p.health or 0) < 40 else
                      "#f57c00" if (p.health or 0) < 60 else CSS_UP)
            # ★ 修复：cost/price 为 None 时（新建仓未成交、行情缺失）
            #   原 `{p.cost:.2f}` 抛 NoneType 格式化异常，整封邮件发不出。
            cost_txt = f"{p.cost:.2f}" if p.cost is not None else "—"
            price_txt = f"{p.price:.2f}" if p.price is not None else "—"
            # ★ 修复：原来的 f-string 里内嵌了带反斜杠转义的引号
            #   （``f"{' <span style=\"...\">紧急</span>' if ...}"``），
            #   这是 Python 3.12 才允许的写法，而 pyproject.toml 声明
            #   target-version = "py311" —— 在 3.11 上直接 SyntaxError。
            #   提取为常量后与版本无关。
            urgent_tag = (' <span style="color:#c62828">紧急</span>'
                          if p.urgent else '')
            rows += (
                f"<tr>"
                f"<td style='padding:5px 6px;font-weight:bold'>{_h(p.code)}"
                f"{urgent_tag}</td>"
                f"<td style='padding:5px 6px'>{_h(p.name)}</td>"
                f"<td style='padding:5px 6px;text-align:right'>{cost_txt}</td>"
                f"<td style='padding:5px 6px;text-align:right'>{price_txt}</td>"
                f"<td style='padding:5px 6px;text-align:right;color:{_color(p.pnl_pct)}'>"
                f"<b>{_pct(p.pnl_pct)}</b></td>"
                f"<td style='padding:5px 6px;text-align:right;color:{hcolor}'>"
                f"<b>{p.health if p.health is not None else '—'}</b></td>"
                f"<td style='padding:5px 6px'>{_h(p.structure)}</td>"
                f"<td style='padding:5px 6px'>{_h(p.advice)}</td>"
                f"<td style='padding:5px 6px;color:#666'>{_h(p.key_levels)}</td>"
                f"</tr>")
        sections.append((
            f"持仓逐只应对（{len(ctx.positions)} 只）",
            f"<table width='100%' style='border-collapse:collapse;font-size:12px'>"
            f"<tr style='background:#f5f5f5'>"
            f"<th align='left' style='padding:4px 6px'>代码</th>"
            f"<th align='left' style='padding:4px 6px'>名称</th>"
            f"<th align='right' style='padding:4px 6px'>成本</th>"
            f"<th align='right' style='padding:4px 6px'>现价</th>"
            f"<th align='right' style='padding:4px 6px'>盈亏</th>"
            f"<th align='right' style='padding:4px 6px'>健康度</th>"
            f"<th align='left' style='padding:4px 6px'>结构状态</th>"
            f"<th align='left' style='padding:4px 6px'>今日建议</th>"
            f"<th align='left' style='padding:4px 6px'>关键价位</th>"
            f"</tr>{rows}</table>"))

    # ⑤ 自选股 Top 5
    if ctx.watch_picks:
        rows = "".join(
            f"<tr><td style='padding:4px 8px;font-weight:bold'>{_h(w.code)}</td>"
            f"<td style='padding:4px 8px'>{_h(w.name)}</td>"
            f"<td style='padding:4px 8px'>{_h(w.reason)}</td>"
            f"<td style='padding:4px 8px;color:#1565c0'>{_h(w.trigger)}</td></tr>"
            for w in ctx.watch_picks[:5])
        sections.append((
            "自选股今日重点关注 Top 5",
            f"<table width='100%'>{rows}</table>"))

    # ⑥ 今日操作纪律
    if ctx.discipline:
        rows = "".join(f"<tr><td style='padding:4px 8px'>· {_h(x)}</td></tr>"
                       for x in ctx.discipline)
        sections.append(("今日操作纪律", f"<table width='100%'>{rows}</table>"))

    return _wrap_html(ctx, sections)


def render_midday_html(ctx: MailContext) -> str:
    """午间策略邮件（附录 5.1 ②）。"""
    sections: list[tuple[str, str]] = []

    if ctx.morning_review:
        rows = "".join(f"<tr><td style='padding:4px 8px'>· {_h(x)}</td></tr>"
                       for x in ctx.morning_review)
        sections.append(("上午盘面回顾", f"<table width='100%'>{rows}</table>"))

    if ctx.sector_changes:
        rows = "".join(f"<tr><td style='padding:4px 8px'>· {_h(x)}</td></tr>"
                       for x in ctx.sector_changes)
        sections.append(("板块强度半日变化", f"<table width='100%'>{rows}</table>"))

    urgent = [p for p in ctx.positions if p.urgent]
    normal = [p for p in ctx.positions if not p.urgent]
    if ctx.positions:
        # 紧急项单独放前面并标红
        if urgent:
            rows = "".join(_pos_row(p) for p in urgent)
            sections.append((
                f"【紧急】持仓出现卖点（{len(urgent)} 只）",
                f"<table width='100%' style='border-collapse:collapse;font-size:12px'>"
                f"<tr style='background:#ffebee'><th align='left' style='padding:4px 6px'>"
                f"代码</th><th align='left' style='padding:4px 6px'>名称</th>"
                f"<th align='right' style='padding:4px 6px'>盈亏</th>"
                f"<th align='left' style='padding:4px 6px'>下午应对</th></tr>{rows}</table>"))
        if normal:
            rows = "".join(_pos_row(p) for p in normal)
            sections.append((
                "下午持仓应对",
                f"<table width='100%' style='border-collapse:collapse;font-size:12px'>"
                f"<tr style='background:#f5f5f5'><th align='left' style='padding:4px 6px'>"
                f"代码</th><th align='left' style='padding:4px 6px'>名称</th>"
                f"<th align='right' style='padding:4px 6px'>现价</th>"
                f"<th align='right' style='padding:4px 6px'>盈亏</th>"
                f"<th align='right' style='padding:4px 6px'>健康度</th>"
                f"<th align='left' style='padding:4px 6px'>下午要不要动</th></tr>{rows}</table>"))
    return _wrap_html(ctx, sections)


def _pos_row(p: PositionRow) -> str:
    hcolor = (CSS_DOWN if (p.health or 0) < 40 else
              "#f57c00" if (p.health or 0) < 60 else CSS_UP)
    # ★ 修复：price 为 None（行情缺失/停牌）时原 `{p.price:.2f}` 抛
    #   NoneType 格式化异常，导致整封午间邮件渲染失败。
    price_txt = f"{p.price:.2f}" if p.price is not None else "—"
    return (f"<tr>"
            f"<td style='padding:5px 6px;font-weight:bold'>{_h(p.code)}</td>"
            f"<td style='padding:5px 6px'>{_h(p.name)}</td>"
            f"<td style='padding:5px 6px;text-align:right'>{price_txt}</td>"
            f"<td style='padding:5px 6px;text-align:right;color:{_color(p.pnl_pct)}'>"
            f"<b>{_pct(p.pnl_pct)}</b></td>"
            f"<td style='padding:5px 6px;text-align:right;color:{hcolor}'>"
            f"{p.health if p.health is not None else '—'}</td>"
            f"<td style='padding:5px 6px'>{_h(p.advice)}</td>"
            f"</tr>")


def render_alert_html(ctx: MailContext) -> str:
    """实时告警邮件（附录 5.1 ③）。"""
    color = "#c62828" if ctx.urgent else "#f57c00"
    label = "紧急告警" if ctx.urgent else "告警"
    body = (f"<div style='background:#fff3f3;border-left:4px solid {color};"
            f"padding:12px 14px;font-size:14px;line-height:1.7'>{_h(ctx.alert_text)}</div>")
    sections: list[tuple[str, str]] = [(label, body)]
    if ctx.positions:
        rows = "".join(_pos_row(p) for p in ctx.positions)
        sections.append((
            "相关持仓",
            f"<table width='100%' style='border-collapse:collapse;font-size:12px'>"
            f"<tr style='background:#f5f5f5'><th align='left' style='padding:4px 6px'>代码</th>"
            f"<th align='left' style='padding:4px 6px'>名称</th>"
            f"<th align='right' style='padding:4px 6px'>现价</th>"
            f"<th align='right' style='padding:4px 6px'>盈亏</th>"
            f"<th align='left' style='padding:4px 6px'>建议</th></tr>{rows}</table>"))
    return _wrap_html(ctx, sections)


def _wrap_html(ctx: MailContext, sections: list[tuple[str, str]]) -> str:
    title = {"premarket": "盘前策略", "midday": "午间策略",
             "alert": "实时告警"}.get(ctx.kind, "通知")
    prefix = "【紧急】" if ctx.urgent else ""
    body = "".join(
        f"<h3 style='color:#1565c0;font-size:15px;margin:14px 0 6px'>{_h(head)}</h3>{html_body}"
        for head, html_body in sections)
    # 单列布局，表格 ≤600px（附录 5.3）
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:12px;background:#fafafa;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif">
<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden">
  <div style="background:{'#c62828' if ctx.urgent else '#1565c0'};color:#fff;padding:14px 16px">
    <div style="font-size:17px;font-weight:bold">{prefix}Hawk 交易决策辅助 · {_h(title)}</div>
    <div style="font-size:12px;opacity:.85;margin-top:3px">
      {ctx.as_of:%Y-%m-%d %H:%M:%S} · 市场状态 {_h(ctx.market_state or '—')}
      {f' · 仓位上限 {ctx.position_cap:.0%}' if ctx.position_cap is not None else ''}
    </div>
  </div>
  <div style="padding:14px 16px">
    {body}
    <div style="margin-top:18px;padding-top:10px;border-top:1px solid #eee;
                font-size:11px;color:#999;line-height:1.6">
      {DISCLAIMER}<br>
      数据来源：腾讯行情 / 新浪财经 / 东方财富（免费接口，可能存在延迟）。<br>
      板块强度因子与资金流数据缺失，推荐与健康度已按缺失处理并重新归一化权重。
    </div>
  </div>
</div></body></html>"""


# ---------------------------------------------------------------- 纯文本
def render_text(ctx: MailContext) -> str:
    """纯文本版（邮件客户端不支持 HTML 时兜底）。"""
    title = {"premarket": "盘前策略", "midday": "午间策略",
             "alert": "实时告警"}.get(ctx.kind, "通知")
    sections: list[tuple[str, list[str]]] = []

    if ctx.overseas:
        sections.append(("隔夜外围", [f"· {x}" for x in ctx.overseas]))
    if ctx.overnight:
        for d, label in (("positive", "夜间利好"), ("negative", "夜间利空")):
            items = [x for x in ctx.overnight if x.direction == d]
            if items:
                sections.append((f"{label}（{len(items)} 条）", [
                    f"· [{x.ts[5:16]}] {x.content}（强度{x.strength}）"
                    + (f" 关联：{'、'.join(x.related)}" if x.related else "")
                    for x in items]))
    sections.append(("大盘环境", [
        f"状态：{ctx.market_state}   评分：{ctx.market_score}",
        f"建议仓位上限：{ctx.position_cap:.0%}" if ctx.position_cap is not None else "",
    ]))
    if ctx.positions:
        sections.append((f"持仓逐只应对（{len(ctx.positions)} 只）", [
            f"{'【紧急】' if p.urgent else ''}{p.code} {p.name}"
            f"  成本{p.cost:.2f} 现价{p.price:.2f}"
            f" 盈亏{_pct(p.pnl_pct)} 健康度{p.health}"
            f" | {p.structure} | {p.advice} | {p.key_levels}"
            for p in ctx.positions]))
    if ctx.watch_picks:
        sections.append(("自选股重点关注 Top 5", [
            f"{w.code} {w.name}：{w.reason}（触发条件：{w.trigger}）"
            for w in ctx.watch_picks[:5]]))
    if ctx.discipline:
        sections.append(("今日操作纪律", [f"· {x}" for x in ctx.discipline]))
    if ctx.morning_review:
        sections.append(("上午回顾", [f"· {x}" for x in ctx.morning_review]))
    if ctx.sector_changes:
        sections.append(("板块变化", [f"· {x}" for x in ctx.sector_changes]))
    if ctx.alert_text:
        sections.append(("告警内容", [ctx.alert_text]))
    sections.append(("免责声明", [DISCLAIMER]))
    return render_text_mail(("【紧急】" if ctx.urgent else "") + title,
                            sections, ctx.as_of)


# ---------------------------------------------------------------- 发送
def send(ctx: MailContext, to: list[str] | None = None,
         retry: int | None = None, force: bool = False) -> dict:
    """渲染并发送。免打扰判定在 MailSender.send 内完成。

    ★★ 修复记录：原实现调用 ``m.send(subject, html_body, text_body, to=to)``，
      但 ``MailSender.send`` 的签名是
      ``(subject, html_body, text_body, as_of, kind, retry)`` —— 没有 ``to`` 参数，
      且缺少必填的 ``as_of``。实跑必抛
      ``TypeError: MailSender.send() got an unexpected keyword argument 'to'``，
      即盘前/午间/告警三类邮件**一封都发不出去**。
      现按真实签名传 as_of 与 kind；``to`` 参数保留用于显式覆盖收件人
      （收件人由 MailConfig 从密钥库读取，此处仅在有覆盖时生效）。

    ★★ `retry` 必须能由调用方控制（2026-10-10 新增）。
      此前本函数**硬编码**走 `MailSender.send` 的默认 `retry=3`，
      而 `MailSender.send` 的重试延迟是 30s/120s/300s（规格 §5.3）
      ⇒ 一次失败的投递会拖**450 秒**（7.5 分钟）才放弃。
      问题在于告警邮件（SMTP 故障时最需要它）会被卡住 7.5 分钟，
      而调用方**没有任何办法**缩短它。
      现在 `retry=None` 表示沿用默认 3次，显式传值可覆盖。

    ★★ `force` 透传（2026-10-10 新增）：默认为 False，即**当日同内容只发一次**
      （实测曾有 midday 邮件连发 35 次，见 ``MailSender.send`` 的去重说明）。
      仅供**用户显式要求重发**时传 True 穿透去重；
      ⚠ 自动补跑（``scheduler.catch_up``）**不得**传 force，否则守卫形同虚设。
    """
    title_map = {"premarket": "盘前策略", "midday": "午间策略", "alert": "告警"}
    html_body = {"premarket": render_premarket_html,
                 "midday": render_midday_html,
                 "alert": render_alert_html}[ctx.kind](ctx)
    text_body = render_text(ctx)
    subject = f"{'【紧急】' if ctx.urgent else ''}Hawk {title_map.get(ctx.kind,'通知')} " \
              f"{ctx.as_of:%m-%d %H:%M}"
    m = MailSender()
    if to:
        # 显式覆盖收件人：仅在调用方指定时改写配置（不改密钥库）
        m.cfg = replace(m.cfg, to=list(to))
    kw = {} if retry is None else {"retry": retry}
    if force:
        kw["force"] = True
    kw["urgent"] = ctx.urgent
    return m.send(subject, html_body, text_body, ctx.as_of, kind=ctx.kind, **kw)


if __name__ == "__main__":
    from datetime import timezone, timedelta
    CST = timezone(timedelta(hours=8))
    as_of = datetime(2026, 10, 9, 8, 30, tzinfo=CST)
    ctx = MailContext(
        as_of=as_of, kind="premarket",
        market_state="修复", position_cap=0.40, market_score=47.5,
        overseas=["美股：纳指 +0.82%，标普500 +0.61%", "欧股：德国DAX +0.34%",
                  "商品：COMEX 黄金 2412.5 美元/盎司（+0.4%）",
                  "中概：纳斯达克金龙指数 +1.12%"],
        overnight=[
            OvernightItem("2026-10-09 07:12", "央行开展 5000 亿元 MLF 操作，中标利率维持 2.00% 不变",
                          "positive", 4, ["银行", "地产"], True),
            OvernightItem("2026-10-09 06:40", "工信部：推动新能源汽车下乡活动，配套充电设施建设",
                          "positive", 3, ["汽车", "电池"], True),
            OvernightItem("2026-10-09 06:05", "某白酒龙头三季度营收同比下滑 12%，业绩不及预期",
                          "negative", 5, ["贵州茅台"], True),
            OvernightItem("2026-10-09 05:30", "美国 9 月非农就业 +21.5 万人，超市场预期",
                          "positive", 4, [], True),
            OvernightItem("2026-10-09 05:02", "国际油价上涨 2.3%，OPEC+ 维持减产决议",
                          "neutral", 2, ["石油石化"], True),
        ],
        positions=[
            PositionRow("600519", "贵州茅台", 1258.20, 1255.79, -0.19, 33.9,
                        "日线最后中枢下方（1270.33）",
                        "健康度偏低，建议降低仓位", "支撑 1180 / 压力 1270", False),
            PositionRow("000001", "平安银行", 11.25, 11.48, +2.04, 68.5,
                        "30m 中枢上沿", "持有，跌破 11.00 减半", "支撑 11.00 / 压力 11.80", False),
        ],
        watch_picks=[
            WatchPick("300750", "宁德时代", "缠论 30m 一买 + 板块 RPS 97", "站上 15 分钟中枢上沿"),
            WatchPick("002594", "比亚迪", "底背驰确认（面积比 0.316）", "MACD 柱面积继续缩小"),
            WatchPick("601318", "中国平安", "保险板块轮动", "突破前高 60.00"),
        ],
        discipline=["修复期仓位上限 40%，禁止打板与追高",
                    "个股健康度 <40 一律列入减仓观察",
                    "缠论卖点触发立即执行，不等收盘"],
    )
    out = _ROOT / "data" / "sample_mail_premarket.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_premarket_html(ctx), encoding="utf-8")
    txt = _ROOT / "data" / "sample_mail_premarket.txt"
    txt.write_text(render_text(ctx), encoding="utf-8")
    logger.info(f"已生成样例: {out} / {txt}")

    print("=" * 78)
    print("盘前邮件 · 纯文本预览")
    print("=" * 78)
    print(render_text(ctx))
