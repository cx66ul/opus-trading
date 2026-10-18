"""消息—行情联动回看（附录 7.1③）。

「某条消息发布后，相关个股 3 分钟 / 30 分钟 / 当日怎么走」——
这是优化 LLM 方向判断质量的**关键反馈闭环**：只有把判断结果与
实际走势对齐，才知道 prompt 该往哪个方向改。

★ 数据可得性边界（实测 2026-10-09，必须如实告知用户）：
  | 周期 | 覆盖交易日 | 能回看多久 |
  |------|-----------|-----------|
  | m1   | **2 天**   | 2 天      |
  | m5   | **11 天**  | 11 天     |
  | m30  | 61 天      | 61 天     |
  所以「3 分钟」只能用 m5 的第一根近似（m1 只 2 天），
  且只能回看最近 2 天。UI 必须标注实际可回溯范围，不能假装能看全年。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                       # noqa: E402

from backend.data import duck                  # noqa: E402
from backend.data.sqlite import get_conn       # noqa: E402

# 观察窗口。★★ 值是**该窗口期望的分钟数**，不是根数 ——
#   根数由周期换算（`bars_for`），否则「m5 的 48 根」会被套到 m30 上
#   ⇒ 48 × 30 分钟 = 24 小时，把「1 天」窗口变成「1 天但取 24 小时数据」。
#   实测覆盖：m1/m5 各 3 只，m30/m60 各 4182 只 ⇒ 主口径是 m30。
WINDOWS = {
    "3m": 3,        # 分钟数。需m1/m5；无细周期时报 unavailable（不冒充）
    "30m": 30,      # 分钟数。m30 一根即可
    "1d": 240,      # 分钟数（4 小时交易）
}

BARS_MINUTES = {"m1": 1, "m5": 5, "m30": 30, "m60": 60}


def bars_for(window: str, period: str) -> int:
    """窗口分钟数 → 该周期需要多少根。"""
    need = WINDOWS.get(window, 0)
    per = BARS_MINUTES.get(period, 0)
    if per <= 0 or need <= 0:
        return 0
    return max(1, -(-need // per))      # 向上取整（3 分钟用 m5 ⇒ 1 根）


@dataclass
class WindowOutcome:
    label: str
    ret_pct: float | None = None
    max_gain: float | None = None
    max_loss: float | None = None
    bars_used: int = 0
    available: bool = True
    note: str = ""


@dataclass
class MessageImpact:
    """单条消息对单只股票的影响。"""
    news_id: int
    code: str
    name: str
    msg_ts: str
    msg_direction: str
    msg_strength: int
    llm_reason: str
    base_price: float | None = None
    windows: dict[str, WindowOutcome] = field(default_factory=dict)
    verdict: str = ""            # 判断对错：correct / wrong / unclear
    verdict_note: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "news_id": self.news_id,
            "code": self.code,
            "name": self.name,
            "msg_ts": self.msg_ts,
            "msg_direction": self.msg_direction,
            "msg_strength": self.msg_strength,
            "llm_reason": self.llm_reason,
            "base_price": self.base_price,
            "windows": {k: {"ret_pct": v.ret_pct, "max_gain": v.max_gain,
                            "max_loss": v.max_loss, "bars_used": v.bars_used,
                            "available": v.available, "note": v.note}
                        for k, v in self.windows.items()},
            "verdict": self.verdict,
            "verdict_note": self.verdict_note,
        }, ensure_ascii=False)


def judge(direction: str, ret_pct: float | None) -> tuple[str, str]:
    """判定 LLM 方向判断的对错。**纯函数**。

    ★ 判定逻辑必须独立成函数，不能散落在调用方——
      否则不同调用点会用不同标准，准确率就不可比了。
    """
    if ret_pct is None:
        return "unclear", "窗口无数据，无法判定"
    if direction == "bull":
        return ("correct", f"利好消息，30分钟后 {ret_pct:+.2f}%") \
            if ret_pct > 0 else ("wrong", f"利好消息但30分钟后 {ret_pct:+.2f}%")
    if direction == "bear":
        return ("correct", f"利空消息，30分钟后 {ret_pct:+.2f}%") \
            if ret_pct < 0 else ("wrong", f"利空消息但30分钟后 {ret_pct:+.2f}%")
    return "unclear", "中性消息，不计入对错"


def period_coverage(con) -> dict[str, int]:
    """各周期的标的覆盖数（读库一次，供所有窗口判定用）。

    ★ 为什么要查而不是硬编码：实测 m1/m5 只覆盖 **3 只**（关注池），
      m30/m60 覆盖 4182 只。硬编码 m5 会让 97% 的消息「无数据」，
      而 `judge()` 会把「无数据」判成 `unclear` ——
      看起来像「LLM 判不准」，真相是「根本没数据」。这两种必须能区分。
    """
    try:
        rows = con.execute(
            "SELECT period, count(DISTINCT code) FROM kline_min "
            "GROUP BY period").fetchall()
    except Exception as exc:
        logger.warning(f"读分钟线覆盖失败: {exc}")
        return {}
    return {r[0]: int(r[1]) for r in rows}


def _period_for(window: str, cov: dict[str, int] | None = None) -> str:
    """窗口 -> 使用的周期。**按覆盖度选**，不硬编码。

    ★★★ 这里改过一次（原实现硬编码 `"m5" if window in ("3m","30m")`），
       原因是它会造成**口径不符**而非「合理近似」：

       实测m1/m5 各只覆盖 **3 只**标的（只同步了关注池+持仓+自选），
       m30/m60 覆盖 **4182 只**。⇒ 硬编码 m5 会让约 97% 的消息
       三个窗口全部返回「无数据」，而 `judge()` 会把它们判`unclear`
       ——看起来是「LLM 判不出对错」，真相是「根本没数据」。

    ★★ 更严重的是**口径冒充**：`3m` 窗口取 m5 的第 1 根，
       实际是**前 5 分钟**的涨跌，却会以「3m」的名义进报告与统计。
       口径不符会让「LLM 3 分钟判断准确率」这个数字失去意义。
       ⇒ 现在按覆盖度降级：优先 m5/m1（真能算 3 分钟），
         没有则 30m/1d 仍可用 m30 计算，**而 3m 窗口直接标 unavailable**。

    `cov`: {period: 覆盖标的数}，缺省时假设 m5 可用（保持旧行为）。
    """
    cov = cov or {}
    fine_first = ["m1", "m5"]
    wide = ["m30", "m60"]

    # ★★ `cov` 为空 ⇒ 不猜。传了 cov 就严格按它判断：
    #   早期用 `cov.get(p, 1)`，默认值 1 让「不存在于 cov 的周期」也被判可用
    #   ⇒ 实测只有 m30/m60 时，3m 窗口仍返回 m1（假覆盖）。
    strict = bool(cov)

    # ★★ 覆盖率门槛：覆盖数 >0 只是「技术上能算」，
    #   若**标的占比**过低，该窗口对绝大多数消息都不可用 ⇒ 形同虚设，
    #   且报告里会出现「少数消息能算、多数不能」的误导性统计。
    #
    #   ★ 基准用「各周期覆盖的**最大值**」（≈ 全市场标的数），
    #     **不是各周期之和** —— 后者会让「m5:100 + m30:4182」里
    #     m5 只占 2.3% 而被判不可用，明显过严。
    universe = max(cov.values()) if strict and cov else 0
    min_cov = max(1, int(universe * 0.5)) if strict else 1

    def usable(p: str) -> bool:
        if strict:
            return cov.get(p, 0) >= min_cov
        return True                      # 未提供覆盖信息 ⇒ 保持旧行为

    if window == "3m":
        # ★ 必须真能算 3 分钟 —— m1 三根≈3 分钟，m5 一根≈5 分钟
        for p in fine_first:
            if usable(p):
                return p
        # 没有任何细周期 ⇒ 宁可不报，也不冒充（返回空串由调用方判 unavailable）
        return ""

    for p in wide + fine_first:
        if usable(p):
            return p
    return wide[0] if wide else ""


def eval_message_price_impact(
    news_id: int, code: str, name: str, msg_ts: str,
    msg_direction: str, msg_strength: int, llm_reason: str,
    as_of: datetime,
    con=None,
) -> MessageImpact:
    """评估一条消息发布后各窗口的走势。"""
    con = con or duck.get_ro_conn()
    imp = MessageImpact(news_id=news_id, code=code, name=name,
                        msg_ts=msg_ts, msg_direction=msg_direction,
                        msg_strength=msg_strength, llm_reason=llm_reason)

    # 消息时刻应落到「消息当天（或之后第一个交易日）」的分钟线里
    cov = period_coverage(con)
    for label in WINDOWS:
        per = _period_for(label, cov)
        if not per:
            # ★ 无可用细周期 ⇒ 3 分钟窗口标 unavailable，**不用粗周期冒充**
            imp.windows[label] = WindowOutcome(
                label=label, available=False,
                note=("无 m1/m5 覆盖（实测各 3 只），无法计算 3 分钟窗口；"
                      "用 m30 冒充会把 30 分钟涨跌标成 3 分钟"))
            continue
        need = bars_for(label, per)
        rows = con.execute(
            """SELECT ts, open, close, high, low FROM kline_min
               WHERE code = ? AND period = ?
                 AND cast(ts AS VARCHAR) >= ?
               ORDER BY ts ASC LIMIT ?""",
            [code, per, msg_ts.replace("T", " ")[:19], need]).fetchall()
        if not rows:
            imp.windows[label] = WindowOutcome(
                label=label, available=False,
                note=(f"无 {per} 数据（该周期实测覆盖 "
                      f"{'2 天' if per == 'm1' else '11 天' if per == 'm5' else '61 天'}）"
                      if per == "m1" or per == "m5" else "消息时间之后无分钟线"))
            continue
        # ★★ 基准必须是**第一根的 open**（开盘价），不能是它的 close。
        #
        #   实测（2026-10-09 601298，m30）：
        #     第一根 ts=10:00（区间 09:30~10:00）open=10.06 close=10.15
        #     用 close 当基准 → 30m 窗口得 +0.00%
        #     用 open  当基准 → 30m 窗口得 **-0.89%**（含开盘半小时的涨跌）
        #
        #   原因：分钟K 线的 ts 是**区间结束时刻**（市场惯例），
        #   所以「消息后第一根」的 close 已经是**消息后半小时**的价格
        #   ⇒ 用它当基准会把消息后的**第一个窗口的涨跌整个吞掉**。
        #   那样任何消息的 30m 窗口都恒等于「半小时后的价 vs 半小时后的价」
        #   ⇒ 30m 窗口恒为 0 或接近 0，**看起来平静实则失效**。
        base = float(rows[0][1]) or float(rows[0][2])
        imp.base_price = imp.base_price or base
        closes = [float(r[2]) for r in rows]
        highs = [float(r[3]) for r in rows]
        lows = [float(r[4]) for r in rows]
        imp.windows[label] = WindowOutcome(
            label=label,
            ret_pct=round((closes[-1] / base - 1) * 100, 3),
            max_gain=round((max(highs) / base - 1) * 100, 3),
            max_loss=round((min(lows) / base - 1) * 100, 3),
            bars_used=len(rows),
            available=True,
        )

    # 判定 LLM 对错（用纯函数，保证各处标准一致）
    d30 = imp.windows.get("30m")
    ret = d30.ret_pct if (d30 and d30.available) else None
    imp.verdict, imp.verdict_note = judge(msg_direction, ret)
    return imp


def build_feedback_loop(as_of: datetime, limit: int = 200) -> dict:
    """构建 LLM 打标的反馈闭环：把所有带关联个股的消息跑一遍。"""
    conn = get_conn()
    con = duck.get_ro_conn()
    rows = conn.execute(
        """SELECT id, ts, direction, strength, related_codes, llm_reason
           FROM news
           WHERE related_codes IS NOT NULL AND related_codes NOT IN ('[]','')
           ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return {"evaluated": 0, "note": "无带关联个股的消息"}

    impacts: list[MessageImpact] = []
    for nid, ts, direction, strength, codes_json, reason in rows:
        try:
            names = json.loads(codes_json)
        except Exception:
            names = []
        for code in names:
            if not isinstance(code, str) or len(code) != 6:
                continue
            nm = conn.execute("SELECT name FROM stock WHERE id=?",
                              (code,)).fetchone()
            impacts.append(eval_message_price_impact(
                nid, code, (nm[0] if nm else code), ts, direction,
                strength or 0, reason or "", as_of, con=con))

    if not impacts:
        return {"evaluated": 0, "note": "关联代码均无有效分钟线数据"}

    # ---- 汇总：LLM 判断准确率
    stats = {"correct": 0, "wrong": 0, "unclear": 0}
    for im in impacts:
        stats[im.verdict] = stats.get(im.verdict, 0) + 1
    judged = stats["correct"] + stats["wrong"]
    acc = round(stats["correct"] / judged, 4) if judged else None

    print()
    print("=" * 74)
    print(f"消息—行情联动回看（{len(impacts)} 条，{as_of.date()}）")
    print("=" * 74)
    print(f"  {'消息':22s}{'代码':>8s}{'判断':>8s}{'3分钟':>9s}{'30分钟':>9s}{'1日':>9s}{'结论':>9s}")
    for im in impacts[:15]:
        w = im.windows
        def fmt(k: str) -> str:
            x = w.get(k)
            if not x or not x.available or x.ret_pct is None:
                return "—"
            return f"{x.ret_pct:+.2f}%"
        vmap = {"correct": "✓ 对", "wrong": "✗ 错", "unclear": "? 不明"}
        print(f"  {(im.llm_reason or im.msg_ts)[:20]:22s}{im.code:>8s}"
              f"{im.msg_direction:>8s}{fmt('3m'):>9s}{fmt('30m'):>9s}"
              f"{fmt('1d'):>9s}{vmap[im.verdict]:>9s}")

    print()
    print("=" * 74)
    print("LLM 方向判断准确率（反馈闭环核心指标）")
    print("=" * 74)
    print(f"  可判定: {judged} 条（中性消息不计入）")
    print(f"  判断正确: {stats['correct']} 条")
    print(f"  判断错误: {stats['wrong']} 条")
    if acc is None:
        print("  准确率: 数据不足（样本 < 10 不显示，避免「1单1对=100%」）")
    else:
        print(f"  准确率: {acc:.1%}")
    print(f"  无法判定: {stats['unclear']} 条（缺分钟线数据）")

    out = {
        "as_of": as_of.isoformat(timespec="seconds"),
        "evaluated": len(impacts),
        "stats": stats,
        "accuracy": acc if judged >= 10 else None,
        "accuracy_note": "" if judged >= 10 else
        f"样本仅 {judged} 条，不足 10 条，按规则不显示准确率",
        "coverage_note": "m1 实测仅 2 天、m5 仅 11 天，"
                         "故「3 分钟」用 m5 第一根近似，且历史消息无法回看",
        "impacts": [im.to_json() for im in impacts],
    }
    p = _ROOT / "data" / "news_impact.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"联动报告已写出: {p}")
    return out


if __name__ == "__main__":
    from datetime import timezone, timedelta
    CST = timezone(timedelta(hours=8))
    as_of = datetime(2026, 10, 9, 0, 55, tzinfo=CST)
    build_feedback_loop(as_of, limit=200)