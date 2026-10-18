"""持仓健康度实跑（附录 4.3）：接真实缠论结构 + 消息面 + 量能 + 大盘。

同时把M4 的缠论信号落库到 signal 表（M6-5）。

★ 板块因子无源（M5 结论：同花顺成分股被 Nginx IP 限制），
  按铁律记为 None（缺失）而非 0，由 health_score 重新归一化权重。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                                   # noqa: E402

from backend.data import duck                              # noqa: E402
from backend.data.sqlite import get_conn, log_event        # noqa: E402
from backend.domain.portfolio import (                     # noqa: E402
    build_health_advice, health_score, list_positions,
)
from backend.domain.regime import REGIME_META                # noqa: E402

CST = timezone(timedelta(hours=8))


# ------------------------------------------------------------缠论结构分
def _structure_score(code: str, con, as_of: str) -> tuple[float, list[str]]:
    """缠论结构分 0-100。

    规则（附录 4.3 「结构分」）：
      ·站上最后一个中枢上方 +40（结构最强）
      · 最后一个中枢内 +25
      · 最后一个中枢下方 +10
      · 有背驰（一买/一卖信号）+20
      · 近20日新高 +20
    每个依据都要写进 reasons，供用户复核。
    """
    from backend.domain.chan import (
        build_bi, build_zhongshu, clean_bars, find_fractals,
        merge_klines, macd_series, detect_beichi, scan_signals,
    )

    rows = duck.get_daily(code, 250)
    if len(rows) < 60:
        return 50.0, [f"日线仅{len(rows)}根，结构分按中性50处理"]
    bars = clean_bars(rows)
    merged = merge_klines(bars)
    fr = find_fractals(merged)
    bis = build_bi(merged, fr, strict="new", bars=bars)
    if not bis:
        return 50.0, ["无有效笔，结构分按中性50处理"]
    zs = build_zhongshu(bis, merged)
    _, _, hist = macd_series([b.c for b in bars])
    bcs = detect_beichi(bis, hist, bars)
    sigs = scan_signals(bis, zs, bcs, bars, hist, as_of=as_of)

    score = 0.0
    reasons: list[str] = []
    price = bars[-1].c

    # ① 中枢位置（ZhongShu 用 high/low 命名，对应缠论的 zg/zd 上沿/下沿）
    if zs:
        last = zs[-1]
        if price > last.high:
            score += 40
            reasons.append(f"站上最后中枢上方（上沿 {last.high:.2f}）")
        elif price >= last.low:
            score += 25
            reasons.append(f"位于最后中枢内（{last.low:.2f}~{last.high:.2f}）")
        else:
            score += 10
            reasons.append(f"位于最后中枢下方（下沿 {last.low:.2f}）")
    else:
        score += 20
        reasons.append("未形成中枢，结构未定")

    # ② 背驰 / 买卖点
    buy_pts = [s for s in sigs if "B" in s.kind]
    sell_pts = [s for s in sigs if "S" in s.kind]
    if buy_pts and not sell_pts:
        score += 20
        reasons.append(f"最近信号为买点（{buy_pts[-1].kind}）")
    elif sell_pts and not buy_pts:
        reasons.append(f"最近信号为卖点（{sell_pts[-1].kind}），减分")
    elif buy_pts and sell_pts:
        score += 10
        reasons.append(f"买卖信号并存（买{buy_pts[-1].kind}/卖{sell_pts[-1].kind}）")

    # ③ 近20 日新高
    win = bars[-21:-1] if len(bars) > 21 else bars[:-1]
    if win:
        hi = max(b.h for b in win)
        if price >= hi:
            score += 20
            reasons.append(f"近20日新高（前高{hi:.2f}）")
    return min(100.0, round(score, 1)), reasons


# ------------------------------------------------------------ 量能分
def _volume_score(code: str, con) -> tuple[float, list[str]]:
    """量能分 0-100：当前量 / 5日均量。"""
    rows = con.execute(
        """SELECT trade_date, close, volume FROM kline_daily
           WHERE code = ? ORDER BY trade_date DESC LIMIT 6""", [code]).fetchall()
    if len(rows) < 6 or not rows[0][2]:
        return 50.0, ["量能数据不足，按中性50"]
    cur = rows[0][2]
    ma5 = sum(r[2] or 0 for r in rows[1:6]) / 5
    if ma5 <= 0:
        return 50.0, ["5日均量为0"]
    ratio = cur / ma5
    if ratio >= 1.5:
        s = 85.0
    elif ratio >= 1.2:
        s = 70.0
    elif ratio >= 0.8:
        s = 55.0
    elif ratio >= 0.5:
        s = 40.0
    else:
        s = 25.0
    return s, [f"量比 {ratio:.2f}（今 {cur:,.0f} / 5日均 {ma5:,.0f}）"]


# ------------------------------------------------------------ 消息面分
def _news_score(code: str, conn, as_of: str) -> tuple[float, list[str], dict]:
    """消息面分：取最近3 日与该股相关的消息方向与强度。"""
    since = (datetime.fromisoformat(as_of) - timedelta(days=3)).date().isoformat()
    rows = conn.execute(
        """SELECT direction, strength, content, ts FROM news
           WHERE related_codes LIKE ? AND ts >= ?
           ORDER BY ts DESC LIMIT 10""",
        (f'%"{code}"%', since)).fetchall()
    if not rows:
        return 50.0, ["近3日无关联消息，按中性50"], {"direction": "neutral",
                                                    "count": 0}
    # ★★ 修复：news.direction 的实际取值域是 bull/bear/neutral
    #   （见 backend/domain/llm.py 的 Direction 与库内实测分布），
    #   原实现比对 "positive"/"negative"，两个条件恒为假 →
    #   持仓健康度的消息面分**恒为中性 50 分**且不报错（静默失效）。
    #   同时兼容 bull/bear 与 positive/negative 两种写法，避免上游换词再踩一次。
    pos = sum(1 for r in rows if r[0] in ("bull", "positive"))
    neg = sum(1 for r in rows if r[0] in ("bear", "negative"))
    avg_str = sum(r[1] or 0 for r in rows) / len(rows)
    net = pos - neg
    score = 50.0 + net * 12.0 + (avg_str - 2) * 4.0
    score = max(0.0, min(100.0, score))
    direction = "bull" if pos > neg else ("bear" if neg > pos else "neutral")
    return score, [f"近3日{len(rows)}条关联消息（利好{pos}/利空{neg}，均强度{avg_str:.1f}）"], {
        "direction": direction, "count": len(rows), "pos": pos, "neg": neg,
        "avg_strength": round(avg_str, 2),
        "samples": [{"ts": r[3], "direction": r[0], "strength": r[1],
                     "content": (r[2] or "")[:80]} for r in rows[:3]],
    }


# ------------------------------------------------------------ 信号落库
def save_signals(as_of: datetime, limit: int = 300, sample_seed: int | None = None) -> int:
    """把缠论信号写入 signal 表（M6-5）。

★ 落库策略：**全量落库，不按日期窗口过滤**。
  实测教训：缠论信号本身稀疏（5 只股票 250 根日线只有 50 条信号，
  平均每月 1 条），若只存「最近 5 个交易日」，实测落库 0 条。
  信号的价值在于回测与复盘（附录 M8 要求复盘能重放当时判定），
  「信号是否还新鲜」应由**查询方**按 ts 判断，而不是在写入时丢弃。
  写入端过滤会不可逆地丢数据 —— 这类过滤必须在读取端做。
"""
    conn = get_conn()
    con = duck.get_ro_conn()
    codes = [r[0] for r in conn.execute(
        "SELECT id FROM stock WHERE is_st=0 AND is_suspend=0"
        " AND float_mv BETWEEN 2e9 AND 2e11").fetchall()]
    if not codes:
        logger.warning("股票库为空")
        return 0
    if sample_seed:
        import random
        random.seed(sample_seed)
        codes = random.sample(codes, min(limit, len(codes)))
    else:
        codes = codes[:limit]

    as_of_s = as_of.isoformat(timespec="seconds")
    cutoff = (as_of.date() - timedelta(days=5)).isoformat()
    # 只清「早于 5 个交易日且仍待人工确认」的信号防表膨胀；
    # 已确认/已归档的一律保留（回测要复现它们）
    conn.execute("DELETE FROM signal WHERE ts < ? AND status = 'pending'", [cutoff])
    total = 0
    scanned = 0
    from backend.domain.chan import (
        build_bi, build_zhongshu, clean_bars, detect_beichi, find_fractals,
        macd_series, merge_klines, scan_signals,
    )
    from backend.domain.stock_index import get_index
    idx = get_index()

    for code in codes:
        rows = duck.get_daily(code, 250)
        if len(rows) < 60:
            continue
        name = idx.name_of(code) or code
        try:
            bars = clean_bars(rows)
            merged = merge_klines(bars)
            fr = find_fractals(merged)
            bis = build_bi(merged, fr, strict="new", bars=bars)
            if not bis:
                continue
            zs = build_zhongshu(bis, merged)
            _, _, hist = macd_series([b.c for b in bars])
            bcs = detect_beichi(bis, hist, bars)
            sigs = scan_signals(bis, zs, bcs, bars, hist,
                                as_of=as_of.isoformat(timespec="seconds"))
            for s in sigs:
                # ★ 不按日期过滤：信号稀疏，全量落库供回测复现。
                #   新鲜度由查询方按 ts 自行判断。
                # chan_snapshot 存完整结构快照 —— 附录C.3要求信号可复现：
                # 日后缠论算法升级时，能用当时的结构重新判定这个信号。
                snap = json.dumps({
                    "bi_count": len(bis),
                    "zhongshu_count": len(zs),
                    "last_zhongshu": ({"low": zs[-1].low, "high": zs[-1].high,
                                  "start_date": zs[-1].start_date,
                                  "end_date": zs[-1].end_date} if zs else None),
                    "beichi_count": sum(1 for b in bcs if b.confirmed),
                    "bars_used": len(bars),
                    "days_ago": (as_of.date() - date.fromisoformat(s.date)).days,
                    "engine": "chan-v1",
                }, ensure_ascii=False)
                # INSERT OR IGNORE 配合唯一索引实现幂等：
                # 每日全量重扫时同一条信号不会重复累积
                # ★ level 必须取引擎的真实档位（s.level），不能按 confidence 推断。
                #   旧实现用 `"strong" if s.confidence >= 0.75 else "normal"` 硬猜，
                #   而弱三买 confidence=0.5 → 被存成 "normal"，
                #   于是「弱三买」在库里伪装成了普通三买，
                #   导致按 level 分档统计时找不到真正的 weak 样本，
                #   误以为「三买没有弱档」而得出错误结论。
                cur = conn.execute(
                    """INSERT OR IGNORE INTO signal
                       (ts, code, name, kind, level, price,
                        strategy_id, confidence, reason,
                        chan_snapshot, market_state, status,
                        rule_version, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s.date, code, name, s.kind,
                     s.level, s.price, "chan", s.confidence, s.reason, snap,
                     REGIME_META.get("修复", {}).get("label", ""),
                     "pending", "chan-v1", as_of_s),
                )
                scanned += 1          # 引擎产出的信号数
                if cur.rowcount:       # INSERT OR IGNORE 实际写入的行数
                    total += cur.rowcount
        except Exception as exc:
            logger.warning(f"{code} 信号计算失败: {type(exc).__name__}: {str(exc)[:60]}")
    conn.commit()
    n_now = conn.execute("SELECT count(*) FROM signal").fetchone()[0]
    logger.info(
        f"信号：扫描 {len(codes)} 只 → 引擎产出 {scanned} 条 → "
        f"实际新增 {total} 条（其余已存在）→ 库内共 {n_now} 条"
    )
    log_event("signals_saved", {"count": total, "scanned": len(codes),
                               "as_of": as_of_s}, as_of_s)
    return total


# ------------------------------------------------------------ 健康度实跑
def run_health(as_of: datetime) -> dict:
    conn = get_conn()
    con = duck.get_ro_conn()
    as_of_s = as_of.isoformat(timespec="seconds")
    positions = list_positions()
    if not positions:
        logger.info("当前无持仓，先建仓再评估")
        return {"positions": 0, "items": []}

    # 大盘分：全市场等权收益（用本地日线算，不用东财指数）
    idx_rows = con.execute(
        """SELECT avg(pct_chg) FROM kline_daily
           WHERE trade_date = (SELECT max(trade_date) FROM kline_daily)
             AND code NOT LIKE '8%' AND code NOT LIKE '4%'
             AND pct_chg IS NOT NULL""").fetchone()
    mkt_pct = float(idx_rows[0] or 0.0)
    market_score = max(0.0, min(100.0, 50.0 + mkt_pct * 8))
    logger.info(f"大盘分 {market_score:.1f}（全市场等权 {mkt_pct:+.2f}%）")

    # 最新价：优先实时接口，失败回落最近收盘
    prices: dict[str, float] = {}
    try:
        from backend.data.adapter import bootstrap, get_adapter
        bootstrap()
        tx = get_adapter("tencent")
        rt = tx.fetch_realtime([p.code for p in positions], as_of)
        prices = {r["code"]: r["price"] for r in rt if r.get("price")}
        logger.info(f"实时行情取到 {len(prices)}/{len(positions)} 只")
    except Exception as exc:
        logger.warning(f"实时行情不可用，回落最近收盘价: {exc}")
    if not prices:
        for p in positions:
            r = duck.get_daily(p.code, 2)
            if len(r) >= 1:
                prices[p.code] = r[-1]["close"]

    items = []
    for p in positions:
        s_struct, r_struct = _structure_score(p.code, con, as_of_s)
        s_vol, r_vol = _volume_score(p.code, con)
        s_news, r_news, news_detail = _news_score(p.code, conn, as_of_s)
        s_board = None# ★ 无源：记 None 而非 0
        r_board = ["板块强度因子无数据源（M5 结论），按缺失处理并重新归一化"]

        hs = health_score({"structure": s_struct, "board": s_board,
                           "news": s_news, "volume": s_vol, "market": market_score})
        price = prices.get(p.code)
        adv = build_health_advice(hs["score"], hs["missing"], p.code, p.name or p.code,
                                  p.stop_loss, price)
        item = {
            "code": p.code, "name": p.name, "shares": p.shares, "cost": p.cost,
            "price": price,
            "pnl_pct": round((price / p.cost - 1) * 100, 2) if price and p.cost else None,
            "market_value": round(price * p.shares, 2) if price else None,
            "score": hs["score"], "missing": hs["missing"], "weight_used": hs["weight_used"],
            "components": {"structure": s_struct, "board": None, "news": s_news,
                           "volume": s_vol, "market": round(market_score, 1)},
            "reasons": {"structure": r_struct, "board": r_board,
                        "news": r_news, "volume": r_vol},
            "news_detail": news_detail,
            "advice": adv,
            "stop_loss": p.stop_loss, "take_profit": p.take_profit,
            "strategy_id": p.strategy_id,
        }
        items.append(item)
        logger.info(
            f"{p.code} {p.name} 健康度 {hs['score']:>5.1f} [{adv['level']}] "
            f"结构{s_struct:.0f}/消息{s_news:.0f}/量能{s_vol:.0f}/大盘{market_score:.0f}"
            + (f"缺{hs['missing']}" if hs["missing"] else "")
        )
        # ★ 不复用 mail_log：那是邮件日志表（body_hash/smtp 状态），
        #   塞健康度快照会污染语义且列不匹配。评估快照走 event_log。
        log_event("portfolio_health",
                  {"code": p.code, "name": p.name, "score": hs["score"],
                   "level": adv["level"], "missing": hs["missing"],
                   "pnl_pct": item["pnl_pct"]}, as_of_s)

    conn.commit()
    summary = {
        "as_of": as_of_s,
        "regime_hint": REGIME_META,
        "market_score": round(market_score, 1),
        "positions": len(items),
        "items": items,
    }
    out = _ROOT / "data" / "portfolio_health.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"健康度报告已写出: {out}")
    return summary


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="持仓健康度 + 信号落库")
    ap.add_argument("--date", help="as_of 日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--signals", action="store_true", help="落库缠论信号")
    ap.add_argument("--signal-limit", type=int, default=300)
    ap.add_argument("--health", action="store_true", help="评估持仓健康度")
    args = ap.parse_args()

    if not args.signals and not args.health:
        args.signals = args.health = True

    as_of = (datetime.fromisoformat(args.date).replace(tzinfo=CST)
             if args.date else datetime.now(CST))
    logger.info(f"as_of = {as_of.isoformat(timespec='seconds')}")

    if args.signals:
        n = save_signals(as_of, limit=args.signal_limit,
                         sample_seed=int(as_of.timestamp()) % 100000)
        logger.info(f"★ 信号落库完成：{n} 条")

    if args.health:
        s = run_health(as_of)
        logger.info(f"★ 健康度评估完成：{s['positions']} 只持仓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())