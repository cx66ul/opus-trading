# -*- coding: utf-8 -*-
"""
Hawk · 邮件发送（附录 G 目录 · M7 调度用）

对应提示词：
  - 邮件通知：盘前 09:00 报告 / 午间 12:00 应对/ 夜间汇总 / 异常推送
  - 第九章 M7：非交易时段邮件不即时发，攒到下一交易时段
  - 附录 M：密钥本地加密（凭据从 backend.common.secrets 取，不硬编码）

设计要点：
  1. **凭据不落代码**：全部从加密存储读取，代码里没有任何明文
  2. **发送即记录**：每次发送写mail_log，可查可追溯（附录 B.2）
  3. **失败重试**：网络抖动是常态，重试 3 次 + 指数退避
  4. **免打扰判定**：非交易时段不即时发，交由调度层决策（第九章）
  5. **纯函数渲染**：邮件正文生成是纯函数 f(report, as_of) -> html，便于回放与测试
"""
from __future__ import annotations

import hashlib
import html
import sys
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any

from loguru import logger

# 支持 `python backend/common/mailer.py` 直接运行
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.common import secrets
from backend.data.sqlite import get_conn

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 配置
@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str
    auth_code: str
    sender: str
    to: list[str]
    use_ssl: bool = True
    timeout: float = 20.0

    @classmethod
    def from_secrets(cls) -> "MailConfig":
        """从加密存储读取配置。

        约定密钥键：
            smtp.host / smtp.port / smtp.user / smtp.auth_code / smtp.sender
            mail.to      收件人，多个用分号分隔
        """
        host = secrets.get_secret("smtp.host")
        user = secrets.get_secret("smtp.user")
        code = secrets.get_secret("smtp.auth_code")
        if not all([host, user, code]):
            raise RuntimeError(
                "SMTP 凭据未配置。请先运行 scripts/setup_secrets.py写入加密存储。"
            )
        to_raw = secrets.get_secret("mail.to") or user
        port = int(secrets.get_secret("smtp.port") or 465)
        return cls(
            host=host,
            port=port,
            user=user,
            auth_code=code,
            sender=secrets.get_secret("smtp.sender") or user,
            to=[x.strip() for x in to_raw.split(";") if x.strip()],
            # 465 走 SMTPS隐式 TLS；587 走明文 STARTTLS
            use_ssl=(port == 465),
        )


# ---------------------------------------------------------------- 免打扰
# D12 已确认：默认 23:00–07:30 免打扰
QUIET_HOURS = (dtime(23, 0), dtime(7, 30))

# 发送失败重试退避梯度（秒）。规格 §5.3 要求 30s / 120s / 300s。
RETRY_DELAYS = (30, 120, 300)


def in_quiet_hours(ts: datetime) -> bool:
    """判断是否处于免打扰时段（跨零点）。时间由调用方注入，不取系统时间。"""
    t = ts.time()
    start, end = QUIET_HOURS
    if start <= end:
        return start <= t < end
    return t >= start or t < end       # 跨零点：23:00~24:00 或 00:00~07:30


def content_hash(html_body: str) -> str:
    """邮件正文的**真实**内容哈希（sha256 前 16 位十六进制）。

    ★★ 原实现写的是 ``f"{len(html_body)}b"`` —— 一个**长度字符串**，不是哈希：
      · 同长度的不同正文 ⇒ 撞车（误判为重复，漏发）
      · 不同长度的同一正文 ⇒ 判不出重复（重复照发）
      ⇒ 即使下游补上查重逻辑，用它当键也**必然出错**。
      实测 2026-10-09 11:35 那封 midday 邮件发35 次，
      就是因为发送链路**根本不查**（旧 body_hash 也不足以支撑查重）。

    纯函数（铁律二）：同样输入必得同样输出，不取系统时间、不含随机盐。
    ⚠ 不含盐是刻意的 —— 去重要求「同内容跨进程可比」，
      加盐会让每次哈希都不同，去重永久失效。
    """
    return hashlib.sha256(html_body.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 渲染（纯函数）
def _cell(v: Any) -> str:
    """单元格可能是纯值，也可能是 (值, 色类) 元组。

    色类约定："up" 涨红 / "down" 跌绿 / "flat" 中性（全局涨红跌绿）。
    纯文本渲染时丢掉色类，HTML 渲染时用它上色。
    """
    return str(v[0]) if isinstance(v, tuple) else str(v)


def _color(v: Any) -> str | None:
    return v[1] if isinstance(v, tuple) and len(v) > 1 and v[1] in ("up", "down", "flat") else None


def render_text_mail(title: str, sections: list[tuple[str, list]],
                     as_of: datetime) -> str:
    """生成纯文本邮件正文。纯函数：同样的输入必得同样的输出，便于复盘比对。

    sections: [(小标题, 行列表), ...]

    ★ 每行支持两种形态（实测踩过：两种都支持才不会出错）：
      1. 字符串 —— 整行直接使用
      2. 单元格列表 —— 各单元格用两个空格连接（表格的纯文本退化形态）
      3. (值, 色类) 元组 —— 走 _cell
      若把所有行都当单元格列表处理，传字符串会被逐字符拆开
      （中文之间会插入空格，非常难看），故按类型分派。
    """
    lines = [
        f"【Hawk 交易决策辅助】{title}",
        f"生成时间：{as_of.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for head, body in sections:
        lines.append(f"── {head} " + "─" * max(2, 46 - _display_width(head)))
        for row in body:
            if isinstance(row, str):
                lines.append(row)
            elif isinstance(row, (list, tuple)):
                lines.append("  ".join(_cell(c) for c in row))
            else:
                lines.append(str(row))
        lines.append("")
    lines.append("─" * 56)
    lines.append("本邮件由系统自动生成，仅供个人决策参考，不构成投资建议。")
    lines.append("数据来源：腾讯行情 / 新浪财经 / 东方财富（免费接口，可能存在延迟）。")
    return "\n".join(lines)


def _display_width(s: str) -> int:
    """估算显示宽度：CJK 字符按 2 列计（用于分隔线对齐）。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def escape(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def render_html_mail(title: str, sections: list[tuple[str, list]],
                     as_of: datetime) -> str:
    """生成 HTML 邮件正文。表格化展示，便于手机阅读。

    配色遵循本项目约定：涨红跌绿（中国股市习惯）。

    ★★ 修复记录：原实现有一个「行级标记」分支——
      若某行最后一个单元格带色类，就认为它是整行标记而 ``cells[:-1]`` 丢弃它，
      随后又用同一个色类去渲染 ``<tr>``。后果是**最后一个单元格被静默删除**
      （实测 ('600519','flat'),('贵州茅台','flat'),('-0.22%','down')
      渲染后 -0.22% 直接消失），而单格整行的场景本就需要保留该单元格。

      现与 render_text_mail 采用完全相同的分派语义，逐类型处理：
      1. 字符串        —— 整行作为单个单元格（不可逐字符拆开）
      2. 序列          —— 每个元素是一个单元格
      3. (值, 色类)    —— 单元格带色（此时整行即一个带色单元格，不丢）
    """
    e = escape
    body = []
    for head, rows in sections:
        trs = []
        for r in rows:
            if isinstance(r, str):
                cells: list = [r]
            elif isinstance(r, (list, tuple)):
                cells = list(r)
            else:
                cells = [r]
            tds = "".join(
                f"<td class='{_color(c) or ''}'>{e(_cell(c))}</td>" if _color(c)
                else f"<td>{e(_cell(c))}</td>"
                for c in cells
            )
            trs.append(f"<tr>{tds}</tr>")
        body.append(f"<h3>{e(head)}</h3><table>{''.join(trs)}</table>")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<style>
body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;font-size:14px;
  line-height:1.6;color:#2C2C2A;margin:0;padding:16px;background:#F7F7F5}}
.wrap{{max-width:720px;margin:0 auto;background:#fff;border-radius:12px;padding:20px}}
h1{{font-size:17px;font-weight:500;margin:0 0 4px}}
h3{{font-size:14px;font-weight:500;margin:20px 0 8px;color:#5F5E5A}}
.meta{{font-size:12px;color:#888780;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:7px 8px;border-bottom:1px solid #F1EFE8;text-align:left}}
tr:last-child td{{border-bottom:none}}
.up{{color:#C62828;font-weight:500}}
.down{{color:#2E7D32;font-weight:500}}
.flat{{color:#5F5E5A}}
.foot{{margin-top:24px;padding-top:12px;border-top:1px solid #F1EFE8;
  font-size:11px;color:#B4B2A9}}
</style></head><body><div class="wrap">
<h1>{e(title)}</h1>
<div class="meta">生成时间 {e(as_of.strftime('%Y-%m-%d %H:%M:%S'))}</div>
{''.join(body)}
<div class="foot">本邮件由系统自动生成，仅供个人决策参考，不构成投资建议。<br>
数据来源：腾讯行情 / 新浪财经 / 东方财富（免费接口，可能存在延迟）。</div>
</div></body></html>"""


# ---------------------------------------------------------------- 发送
class MailSender:
    """SMTP 发送器。每次发送写 mail_log（附录 B.2）。"""

    def __init__(self, cfg: MailConfig | None = None) -> None:
        self.cfg = cfg or MailConfig.from_secrets()

    def _connect(self) -> smtplib.SMTP:
        if self.cfg.use_ssl:
            ctx = ssl.create_default_context()
            return smtplib.SMTP_SSL(self.cfg.host, self.cfg.port,
                                    context=ctx, timeout=self.cfg.timeout)
        s = smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)
        s.starttls(context=ssl.create_default_context())
        return s

    def send(self, subject: str, html_body: str, text_body: str,
             as_of: datetime, kind: str = "general", retry: int = 3,
             urgent: bool = False, force: bool = False) -> dict:
        """发送邮件。as_of 由调用方注入（铁律二）。

        ★ 免打扰：23:00–07:30 不发送普通邮件（决策 D12），
          但 ``urgent=True``（重大利空等）可穿透免打扰。
          原实现里 ``in_quiet_hours`` 只被自测调用，发送链路从不判定，
          等于免打扰形同虚设 —— 现接入 send 入口。

        ★★ 去重（本轮修复的真 bug）：
          实测 `mail_log` 里 **2026-10-09 11:35 的 midday 邮件被发了 35 次**，
          全部 status=sent。根因是本方法**只写 mail_log、从不查**是否已发过
          —— 全文没有任何 dedup 判据；而 `scheduler.catch_up()` 在重启/补跑时
          会重新触发同一封邮件 ⇒ 重复放大。

          ★★ 注意 `body_hash` 原来是 `f"{len(html_body)}b"`（**长度字符串**，
            不是哈希）。同长度的不同正文会撞在一起、不同长度的同一正文反而
            判不出来 —— 即使下游想查重也用不了。现改为**真实 sha256**。

          判据：`(kind, body_hash, 交易日)` 已有 `status='sent'` 的记录 ⇒ 跳过。
          ⚠ `force=True` 用于**用户主动重发**（显式意图），必须能穿透去重；
            但 `catch_up()` 这种**自动补跑**不得传 force，否则守卫形同虚设。

        返回 {"ok": bool, "id": int|None, "err": str, "skipped": bool,
              "deduped": bool}
        """
        conn = get_conn()
        if not urgent and in_quiet_hours(as_of):
            logger.info(f"[mail] 免打扰时段（{as_of:%H:%M}），跳过 kind={kind}")
            self._log_skip(conn, as_of, kind, subject)
            return {"ok": False, "id": None, "skipped": True,
                    "deduped": False, "err": "免打扰时段，未发送"}
        body_hash = content_hash(html_body)
        if not force and self._already_sent(conn, kind, body_hash, as_of):
            logger.info(f"[mail] 判重跳过 kind={kind} subject={subject} "
                        f"（当日同内容已发过）")
            return {"ok": True, "id": None, "skipped": True,
                    "deduped": True, "err": "当日已发送过相同内容，跳过"}
        for attempt in range(1, retry + 1):
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = formataddr(("Hawk 交易辅助", self.cfg.sender))
                msg["To"] = ", ".join(self.cfg.to)
                msg["Date"] = formatdate(as_of.timestamp(), localtime=True)
                msg.attach(MIMEText(text_body, "plain", "utf-8"))
                msg.attach(MIMEText(html_body, "html", "utf-8"))

                with self._connect() as s:
                    s.login(self.cfg.user, self.cfg.auth_code)
                    s.sendmail(self.cfg.sender, self.cfg.to, msg.as_string())

                cur = conn.execute(
                    "INSERT INTO mail_log (ts, kind, subject, body_hash, status) "
                    "VALUES (?,?,?,?,?)",
                    [as_of.isoformat(timespec="seconds"), kind, subject,
                     body_hash, "sent"],
                )
                conn.commit()
                logger.info(f"[mail] 已发送 kind={kind} subject={subject}")
                return {"ok": True, "id": cur.lastrowid, "err": "",
                        "skipped": False, "deduped": False}

            except smtplib.SMTPAuthenticationError as exc:
                # 认证失败重试无意义，直接记录
                self._log_fail(conn, as_of, kind, subject, f"认证失败: {exc}",
                                 0, body_hash)
                return {"ok": False, "id": None, "deduped": False,
                        "err": f"SMTP 认证失败: {exc}"}
            except Exception as exc:
                self._log_fail(conn, as_of, kind, subject, str(exc), attempt, body_hash)
                if attempt < retry:
                    # ★ 退避梯度按规格 §5.3 的 30s/120s/300s，
                    #   原为 2**attempt（2s/4s），比规格小 75 倍，
                    #   SMTP 临时故障几乎必然在重试窗口内全部撞死。
                    delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                    logger.warning(f"[mail] 第 {attempt} 次失败({exc})，{delay}s 后重试")
                    time.sleep(delay)
        return {"ok": False, "id": None, "deduped": False,
                "err": f"重试 {retry} 次仍失败"}

    @staticmethod
    def _already_sent(conn, kind: str, body_hash: str, as_of: datetime) -> bool:
        """当日同`(kind, body_hash)` 是否已成功发送过。

        ★ 按**交易日**而非自然日归一：`as_of` 由调度器注入的是交易日的运行时刻，
          而补跑（catch_up）可能把时间推到当天任意钟点。
          用`date(as_of)` 归一才不会因为「同一封邮件被 11:35 和 16:20 各跑一次」
          而漏判。⚠ 用真实时间戳做等值比较是最常见的错 —— 补跑必漏。
        """
        row = conn.execute(
            "SELECT 1 FROM mail_log WHERE kind = ? AND body_hash = ? "
            "AND status = 'sent' AND substr(ts, 1, 10) = ? LIMIT 1",
            [kind, body_hash, as_of.date().isoformat()],
        ).fetchone()
        return row is not None

    @staticmethod
    def _log_fail(conn, as_of: datetime, kind: str, subject: str,
                  err: str, retry: int = 0, body_hash: str | None = None) -> None:
        """失败也要落 `body_hash`。

        ★★ 本轮实测补的坑：原来**不写 body_hash**（列留NULL）。
          后果是「判重会不会被失败记录挡住」这件事**无法从数据判断** ——
          实际是靠 NULL 撞不上哈希而侥幸正确（等价变异测不出来）。
          一旦将来有人「顺手」把失败行的 hash 也填上，
          没有 `status='sent'` 这道过滤就会把失败当成已发⇒ **当日永久丢信**。
          ⇒ 写入 hash + 保留 status 过滤 =两道独立防线，不再依赖巧合。
          填上 hash 也让事后能回答「失败的是哪一封内容」。
        """
        conn.execute(
            "INSERT INTO mail_log (ts, kind, subject, body_hash, status, err, retry) "
            "VALUES (?,?,?,?,?,?,?)",
            [as_of.isoformat(timespec="seconds"), kind, subject, body_hash,
             "failed", err[:200], retry],
        )
        conn.commit()

    @staticmethod
    def _log_skip(conn, as_of: datetime, kind: str, subject: str) -> None:
        """免打扰跳过的邮件也要留痕，否则事后无法区分「没发」与「不该发」。"""
        conn.execute(
            "INSERT INTO mail_log (ts, kind, subject, status, err) "
            "VALUES (?,?,?,?,?)",
            [as_of.isoformat(timespec="seconds"), kind, subject,
             "skipped", "免打扰时段"],
        )
        conn.commit()


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("=== 免打扰时段判定（23:00–07:30，跨零点）===")
    for t in ["22:59", "23:00", "23:30", "00:30", "07:29", "07:30", "09:00", "12:00"]:
        hh, mm = map(int, t.split(":"))
        ts = datetime(2026, 10, 8, hh, mm, tzinfo=CST)
        print(f"  {t}  免打扰={'是' if in_quiet_hours(ts) else '否'}")

    print()
    print("=== 纯函数渲染（同样输入必得同样输出）===")
    secs = [
        ("持仓应对", [
            (("600519", "flat"), ("贵州茅台", "flat"), ("1255.79", "flat"),
             ("-0.22%", "down"), ("中枢上方，回抽不破", "flat")),
        ]),
    ]
    txt = render_text_mail("盘前报告", secs, datetime(2026, 10, 9, 9, 0, tzinfo=CST))
    print(txt[:300])
    print()
    # 纯函数性验证
    a = render_text_mail("盘前报告", secs, datetime(2026, 10, 9, 9, 0, tzinfo=CST))
    b = render_text_mail("盘前报告", secs, datetime(2026, 10, 9, 9, 0, tzinfo=CST))
    print("两次渲染结果一致（纯函数）:", "是" if a == b else "否")

    h = render_html_mail("盘前报告", secs, datetime(2026, 10, 9, 9, 0, tzinfo=CST))
    print(f"HTML 正文 {len(h)} 字节，含涨红跌绿样式:", ".up{" in h and ".down{" in h)

    print()
    print("=== 连通性验证 ===")
    try:
        cfg = MailConfig.from_secrets()
        print(f"  配置读取成功: {cfg.host}:{cfg.port}发件={cfg.sender} 收件={cfg.to}")
        print("  授权码: 已从DPAPI 解密（长度 "
              f"{len(cfg.auth_code)}，值不回显）")
    except Exception as exc:
        print(f"  配置读取失败: {exc}")
        sys.exit(1)
