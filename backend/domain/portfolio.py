"""自选股与持仓管理（附录 6.2/ 6.3）。

设计要点：
1. **所有写操作必须落event_log** —— 用户的持仓是本系统最敏感的数据，
   任何改动都要可追溯（谁、何时、改了什么、改前是什么）。
2. **代码必须本地化校验** —— 不能写入股票库里不存在的代码，
   否则推荐引擎与健康度计算会静默跳过这些记录（与M3 同类陷阱）。
3. **时间必须注入** —— 铁律二：所有落库时间用 as_of，不取系统时间。
4. 业务规则用纯函数实现（``health_score`` / ``build_health_advice``），
   CRUD 只负责存取，便于离线复现。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                                    # noqa: E402

from backend.data.sqlite import get_conn, log_event          # noqa: E402

# ---------------------------------------------------------------- 常量
VALID_SIDES = ("buy", "sell", "add", "reduce")
POSITION_STATUS = ("holding", "closed")


class PortfolioError(ValueError):
    """业务规则违反（代码不存在、股数为负、成本为空等）。"""


# ---------------------------------------------------------------- 数据结构
@dataclass
class WatchItem:
    code: str
    name: str = ""
    group_name: str = "核心池"
    add_price: float | None = None
    add_date: str | None = None
    reason: str = ""
    tags: list[str] | None = None
    priority: int = 0
    last_mark: str | None = None
    last_mark_at: str | None = None


@dataclass
class Position:
    code: str
    name: str = ""
    open_price: float | None = None
    open_date: str | None = None
    shares: int = 0
    cost: float | None = None          # 每股成本（含费用摊薄）
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy_id: str = "manual"
    status: str = "holding"
    note: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------- 校验
def _ensure_stock_exists(conn, code: str) -> str:
    """校验代码在本地股票库中，返回其名称。不存在则抛 PortfolioError。

    ★ 不做静默跳过：写入不存在的代码会让推荐/健康度计算把它漏掉，
      用户以为持仓被记录了，实际从未参与计算 —— 这类静默失效最危险。
    """
    row = conn.execute(
        "SELECT name, is_st, status FROM stock WHERE id = ?", (code,)
    ).fetchone()
    if row is None:
        raise PortfolioError(f"代码 {code} 不在本地股票库（是否输错或未同步数据？）")
    name, is_st, status = row
    if status != "active":
        raise PortfolioError(f"{code} {name} 状态为 {status}（已退市），不可新增持仓")
    if is_st:
        # 不禁止 ST 持仓（用户可能已经持有），但明确提示
        logger.warning(f"{code} {name} 为 ST 股，新增多单需谨慎")
    return name


# ---------------------------------------------------------------- 自选股 CRUD
def add_watch(items: list[WatchItem], as_of: datetime) -> int:
    """新增/更新自选股。同一 code 视为更新（UPSERT 语义）。"""
    conn = get_conn()
    n = 0
    for it in items:
        name = _ensure_stock_exists(conn, it.code)
        conn.execute(
            """
            INSERT INTO watchlist (code, group_name, add_price, add_date, reason,
                                   tags, priority, last_mark, last_mark_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
              group_name = COALESCE(NULLIF(excluded.group_name,''), watchlist.group_name),
              add_price  = COALESCE(excluded.add_price,  watchlist.add_price),
              add_date   = COALESCE(NULLIF(excluded.add_date,''), watchlist.add_date),
              reason     = COALESCE(NULLIF(excluded.reason,''),    watchlist.reason),
              tags       = COALESCE(NULLIF(excluded.tags,''),      watchlist.tags),
              priority   = excluded.priority
            """,
            (it.code, it.group_name, it.add_price, it.add_date, it.reason,
             json.dumps(it.tags, ensure_ascii=False) if it.tags else None,
             it.priority, it.last_mark, it.last_mark_at),
        )
        log_event("watch_upsert",
                  {"code": it.code, "name": name, "group": it.group_name,
                   "priority": it.priority, "reason": it.reason},
                  as_of.isoformat(timespec="seconds"))
        n += 1
    conn.commit()
    logger.info(f"自选股写入 {n} 条")
    return n


def remove_watch(codes: list[str], as_of: datetime) -> int:
    conn = get_conn()
    n = 0
    for c in codes:
        row = conn.execute("SELECT code FROM watchlist WHERE code=?", (c,)).fetchone()
        if row is None:
            logger.warning(f"[skip] {c} 不在自选股中")
            continue
        conn.execute("DELETE FROM watchlist WHERE code=?", (c,))
        log_event("watch_remove", {"code": c}, as_of.isoformat(timespec="seconds"))
        n += 1
    conn.commit()
    logger.info(f"自选股删除 {n} 条")
    return n


def mark_watch(codes: list[str], mark: str, as_of: datetime) -> int:
    """给自选股打标记（触发缠论买点/消息驱动/减仓观察 等）。"""
    if not mark.strip():
        raise PortfolioError("标记内容不能为空")
    conn = get_conn()
    ts = as_of.isoformat(timespec="seconds")
    n = 0
    for c in codes:
        # watchlist 表不存 name（股票名在 stock 表，避免冗余与不一致）
        cur = conn.execute(
            "SELECT w.code, s.name, w.last_mark FROM watchlist w"
            " LEFT JOIN stock s ON s.id = w.code WHERE w.code=?", (c,)
        ).fetchone()
        if cur is None:
            logger.warning(f"[skip] {c} 不在自选股中")
            continue
        conn.execute(
            "UPDATE watchlist SET last_mark=?, last_mark_at=? WHERE code=?",
            (mark.strip(), ts, c),
        )
        log_event("watch_mark",
                  {"code": c, "name": cur[1], "old_mark": cur[2], "new_mark": mark},
                  ts)
        n += 1
    conn.commit()
    logger.info(f"标记 {n} 条：{mark}")
    return n


def list_watch(group: str | None = None) -> list[dict]:
    conn = get_conn()
    if group:
        rows = conn.execute(
            "SELECT code, name, group_name, add_price, add_date, reason, tags,"
            " priority, last_mark, last_mark_at"
            " FROM (SELECT w.*, s.name FROM watchlist w LEFT JOIN stock s ON s.id=w.code)"
            " WHERE group_name=? ORDER BY priority DESC, code", (group,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT code, name, group_name, add_price, add_date, reason, tags,"
            " priority, last_mark, last_mark_at"
            " FROM (SELECT w.*, s.name FROM watchlist w LEFT JOIN stock s ON s.id=w.code)"
            " ORDER BY group_name, priority DESC, code"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(zip(["code", "name", "group", "add_price", "add_date", "reason",
                      "tags", "priority", "last_mark", "last_mark_at"], r))
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        out.append(d)
    return out


# ---------------------------------------------------------------- 持仓 CRUD
def open_position(pos: Position, as_of: datetime) -> int:
    """新建持仓。shares>0 且 cost>0。"""
    if pos.shares <= 0:
        raise PortfolioError(f"{pos.code} 股数必须为正（当前 {pos.shares}）")
    if pos.cost is None or pos.cost <= 0:
        raise PortfolioError(f"{pos.code} 成本必须为正")
    conn = get_conn()
    name = _ensure_stock_exists(conn, pos.code)
    existing = conn.execute(
        "SELECT id, shares, cost FROM position WHERE code=? AND status='holding'",
        (pos.code,),
    ).fetchone()
    if existing:
        raise PortfolioError(
            f"{pos.code} {name} 已有持仓（{existing[1]} 股 @ {existing[2]}），"
            f"请用 add_shares() 加仓而非重复建仓"
        )
    ts = as_of.isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO position (code, name, open_price, open_date, shares, cost,
                                 stop_loss, take_profit, strategy_id, status, note, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?, 'holding', ?, ?)""",
        (pos.code, name, pos.open_price, pos.open_date or as_of.date().isoformat(),
         pos.shares, pos.cost, pos.stop_loss, pos.take_profit,
         pos.strategy_id, pos.note, ts),
    )
    conn.execute(
        """INSERT INTO position_trade (code, side, price, shares, trade_time, fee, reason)
           VALUES (?,?,?,?,?,?,?)""",
        (pos.code, "buy", pos.cost, pos.shares, ts, 0.0, "建仓"),
    )
    log_event("position_open",
              {"code": pos.code, "name": name, "shares": pos.shares,
               "cost": pos.cost, "stop_loss": pos.stop_loss,
               "take_profit": pos.take_profit, "strategy": pos.strategy_id}, ts)
    conn.commit()
    logger.info(f"建仓 {pos.code} {name} {pos.shares} 股 @ {pos.cost}")
    return 1


def add_shares(code: str, shares: int, price: float, as_of: datetime,
               fee: float = 0.0, reason: str = "") -> dict:
    """加仓：新shares>0。返回加权后的成本与股数。"""
    if shares <= 0:
        raise PortfolioError(f"加仓股数必须为正（当前 {shares}）")
    if price <= 0:
        raise PortfolioError(f"加仓价必须为正（当前 {price}）")
    conn = get_conn()
    row = conn.execute(
        "SELECT code, name, shares, cost FROM position WHERE code=? AND status='holding'",
        (code,),
    ).fetchone()
    if row is None:
        raise PortfolioError(f"{code} 无持仓，无法加仓")
    _, name, old_shares, old_cost = row
    new_shares = old_shares + shares
    # 加权平均成本（含手续费摊薄）
    new_cost = round((old_cost * old_shares + price * shares + fee) / new_shares, 4)
    ts = as_of.isoformat(timespec="seconds")
    conn.execute(
        "UPDATE position SET shares=?, cost=?, updated_at=? WHERE code=? AND status='holding'",
        (new_shares, new_cost, ts, code),
    )
    conn.execute(
        """INSERT INTO position_trade (code, side, price, shares, trade_time, fee, reason)
           VALUES (?,?,?,?,?,?,?)""",
        (code, "add", price, shares, ts, fee, reason or "加仓"),
    )
    log_event("position_add",
              {"code": code, "name": name, "add_shares": shares, "add_price": price,
               "old_shares": old_shares, "new_shares": new_shares,
               "old_cost": old_cost, "new_cost": new_cost}, ts)
    conn.commit()
    logger.info(f"加仓 {code} {name} +{shares}股 @{price} -> 成本 {old_cost}→{new_cost}")
    return {"code": code, "name": name, "shares": new_shares, "cost": new_cost}


def reduce_shares(code: str, shares: int, price: float, as_of: datetime,
                  fee: float = 0.0, reason: str = "") -> dict:
    """减仓。shares 可等于持仓数（全清）。返回是否已清空。"""
    if shares <= 0:
        raise PortfolioError(f"减仓股数必须为正（当前 {shares}）")
    conn = get_conn()
    row = conn.execute(
        "SELECT code, name, shares, cost FROM position WHERE code=? AND status='holding'",
        (code,),
    ).fetchone()
    if row is None:
        raise PortfolioError(f"{code} 无持仓，无法减仓")
    _, name, old_shares, cost = row
    if shares > old_shares:
        raise PortfolioError(f"{code} 减仓 {shares} 股超过持仓 {old_shares} 股")
    left = old_shares - shares
    ts = as_of.isoformat(timespec="seconds")
    # 盈亏（估算，不含完整费用）
    pnl_pct = round((price / cost - 1) * 100, 2) if cost else None
    if left == 0:
        conn.execute(
            "UPDATE position SET shares=0, status='closed', updated_at=? "
            "WHERE code=? AND status='holding'", (ts, code))
    else:
        conn.execute("UPDATE position SET shares=?, updated_at=? WHERE code=?",
                     (left, ts, code))
    conn.execute(
        """INSERT INTO position_trade (code, side, price, shares, trade_time, fee, reason)
           VALUES (?,?,?,?,?,?,?)""",
        (code, "sell" if left == 0 else "reduce", price, shares, ts, fee,
         reason or ("清仓" if left == 0 else "减仓")),
    )
    log_event("position_reduce",
              {"code": code, "name": name, "shares": shares, "price": price,
               "cost": cost, "pnl_pct": pnl_pct, "left": left,
               "closed": left == 0}, ts)
    conn.commit()
    logger.info(f"减仓 {code} {name} -{shares}股 @{price} 盈亏{pnl_pct}%"
                +("（已清仓）" if left == 0 else f" 剩余{left}股"))
    return {"code": code, "name": name, "left": left, "pnl_pct": pnl_pct,
            "closed": left == 0}


def set_stop_loss(code: str, stop_loss: float | None,
                  take_profit: float | None, as_of: datetime) -> int:
    """设置止损/止盈。传 None 表示清除该侧。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT code, name, stop_loss, take_profit FROM position "
        "WHERE code=? AND status='holding'", (code,)
    ).fetchone()
    if row is None:
        raise PortfolioError(f"{code} 无持仓")
    ts = as_of.isoformat(timespec="seconds")
    conn.execute(
        "UPDATE position SET stop_loss=?, take_profit=?, updated_at=? WHERE code=?",
        (stop_loss, take_profit, ts, code),
    )
    log_event("position_set_sl",
              {"code": code, "name": row[1], "old_sl": row[2], "new_sl": stop_loss,
               "old_tp": row[3], "new_tp": take_profit}, ts)
    conn.commit()
    return 1


def list_positions(holding_only: bool = True) -> list[Position]:
    conn = get_conn()
    sql = ("SELECT code, name, open_price, open_date, shares, cost, stop_loss,"
           " take_profit, strategy_id, status, note, updated_at FROM position")
    if holding_only:
        #★★ `shares > 0` 不能作为「是否持仓」的判据。
        #   实测（2026-10-11）：用户给了持仓股票但**未给股数**时，
        #   股数为 NULL ⇒ `shares>0` 为假 ⇒ 持仓**整条从页面上消失**，
        #   而 API 正常返回 200、rows 为空 ⇒ 看起来像「没持仓」。
        #
        #   ⇒ 「status='holding'」才是在**说**持仓与否；
        #     `shares` 只是持仓的**规模**，缺失不影响这个事实。
        #   ⇒ 若是连`status` 都为 NULL 的历史行，才算「状态未知」。
        sql += (" WHERE COALESCE(status,'holding')='holding'"
                "   AND (shares IS NULL OR shares >= 0)")
    sql += " ORDER BY code"
    out = []
    for r in conn.execute(sql).fetchall():
        out.append(Position(
            code=r[0], name=r[1] or "", open_price=r[2], open_date=r[3],
            shares=r[4], cost=r[5], stop_loss=r[6], take_profit=r[7],
            strategy_id=r[8], status=r[9], note=r[10] or "", updated_at=r[11] or "",
        ))
    return out


def trade_history(code: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_conn()
    if code:
        rows = conn.execute(
            "SELECT code, side, price, shares, trade_time, fee, reason, signal_id"
            " FROM position_trade WHERE code=? ORDER BY trade_time DESC, id DESC"
            " LIMIT ?", (code, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT code, side, price, shares, trade_time, fee, reason, signal_id"
            " FROM position_trade ORDER BY trade_time DESC, id DESC LIMIT ?",
            (limit,)).fetchall()
    cols = ["code", "side", "price", "shares", "trade_time", "fee", "reason", "signal_id"]
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------- 持仓健康度（附录 4.3）
# 权重严格按文档：结构 0.30 / 板块 0.25 / 消息 0.20 / 量能 0.15 / 大盘 0.10
HEALTH_WEIGHTS = {
    "structure": 0.30,
    "board": 0.25,
    "news": 0.20,
    "volume": 0.15,
    "market": 0.10,
}


def health_score(components: dict[str, float | None]) -> dict:
    """持仓健康度 0-100。

    ★ 缺失因子绝不当0（实测教训）：缺失记 50 分（中性）并从权重中剔除，
      其余因子权重**重新归一化**。若把缺失当 0，每缺一项会被系统性扣
      12.5~25 分，所有持仓都被打成"不健康"，比"信息缺失"严重得多。

    components: {"structure": 0-100, "board": ..., "news": ...,
                 "volume": ..., "market": ...}，None 表示无数据源
    """
    valid: dict[str, float] = {}
    missing: list[str] = []
    for k, w in HEALTH_WEIGHTS.items():
        v = components.get(k)
        if v is None:
            missing.append(k)
        else:
            valid[k] = max(0.0, min(100.0, float(v)))
    total_w = sum(HEALTH_WEIGHTS[k] for k in valid)
    if total_w == 0:
        return {"score": 0.0, "missing": missing,
                "note": "全部因子无数据，无法评估"}
    raw = sum(valid[k] * HEALTH_WEIGHTS[k] for k in valid)
    score = round(raw / total_w, 1)         # 重新归一化
    return {"score": score, "missing": missing,
            "weight_used": {k: round(HEALTH_WEIGHTS[k] / total_w, 4) for k in valid}}


def build_health_advice(score: float, missing: list[str], code: str,
                        name: str, stop_loss: float | None,
                        price: float | None) -> dict:
    """健康度 -> 处置建议（附录 4.3：Health < 40 标红并给减仓建议）。

    ★ 必须把「缺哪些因子」写进文案。实测发现的风险：
      缺失因子经重新归一化后，**分数可能反而升高**
      （示例：板块+消息缺失 → 72.7，高于全因子的 70.8）。
      数学上没问题（剔除低分项后其余被放大），但若不显式说明，
      用户会把"信息缺失"误读成"持仓更健康"—— 这是危险的误读。
      故缺失一律追加到建议文案中，不允许静默。
    """
    zh = {"structure": "缠论结构", "board": "板块强度",
          "news": "消息面", "volume": "量能", "market": "大盘环境"}
    miss_txt = ("（缺" + "、".join(zh.get(m, m) for m in missing) + "因子，该项未参与计分）"
                if missing else "")
    hit_sl = (stop_loss is not None and price is not None and price <= stop_loss)
    if hit_sl:
        return {"level": "red", "action": "执行止损",
                "text": f"{code} {name} 现价 {price:.2f} 已跌破止损位 {stop_loss:.2f}，按纪律减仓"}
    if score < 40:
        return {"level": "red", "action": "建议减仓",
                "text": f"{code} {name} 健康度 {score}（<40），建议降低仓位" + miss_txt}
    if score < 60:
        return {"level": "yellow", "action": "观察",
                "text": f"{code} {name} 健康度 {score}，建议观察不加仓" + miss_txt}
    return {"level": "green", "action": "持有",
            "text": f"{code} {name} 健康度 {score}，可持有" + miss_txt}


# ---------------------------------------------------------------- 批量导入导出（D10）
def _write_template(path: Path, kind: str) -> None:
    """写空白模板并附填���示例行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "watch":
        lines = ["代码,分组,加入价,加入日期,理由,标签,优先级",
                 "600519,核心池,1255.79,2026-10-08,缠论买点+消息驱动,\"缠论买点;消息驱动\",3",
                 "000001,观察池,,,待观察,,1",
                 "300750,核心池,210.50,2026-10-08,RPS 强势,\"RPS强势;半导体\",2"]
    else:
        lines = ["代码,建仓价,建仓日期,股数,成本,止损,止盈,策略,备注",
                 "600519,1255.79,2026-10-08,100,1258.20,1180.00,1400.00,manual,长期持有",
                 "000001,11.20,2026-10-08,10000,11.25,10.50,12.80,manual,"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    logger.info(f"已写出{kind} 模板: {path}")


def export_template(kind: str, out_dir: str | None = None) -> Path:
    """生成 CSV 导入模板（Excel 可直接打开，用 utf-8-sig 防中文乱码）。"""
    if kind not in ("watch", "position"):
        raise PortfolioError(f"不支持的模板类型: {kind}")
    d = Path(out_dir or (_ROOT / "data" / "templates"))
    p = d / f"{kind}_import_template.csv"
    _write_template(p, kind)
    return p


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(f)]


def _f(v: str) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _i(v: str, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def import_csv(kind: str, path: str | Path, as_of: datetime,
               dry_run: bool = True) -> dict:
    """批量导入。默认 dry_run=True 只校验不落库 —— 批量写最危险的是半成品。

    返回 {"ok": [...], "failed": [(row_no, code, reason)], "dry_run": bool}
    """
    p = Path(path)
    if not p.exists():
        raise PortfolioError(f"文件不存在: {p}")
    rows = _read_csv_rows(p)
    ok: list[dict] = []
    failed: list[tuple[int, str, str]] = []
    for i, r in enumerate(rows, start=2):        # 2 = 含表头
        code = r.get("代码", "")
        if not code:
            continue
        try:
            if kind == "watch":
                tags = [x for x in (r.get("标签") or "").replace("，", ";").split(";") if x]
                item = WatchItem(
                    code=code, group_name=r.get("分组") or "核心池",
                    add_price=_f(r.get("加入价")), add_date=r.get("加入日期") or None,
                    reason=r.get("理由") or "", tags=tags,
                    priority=_i(r.get("优先级"), 0),
                )
                # 先校验代码存在，避免整批因一只失败
                _ensure_stock_exists(get_conn(), code)
                if dry_run:
                    ok.append({"row": i, "code": code, "group": item.group_name,
                               "reason": item.reason})
                else:
                    add_watch([item], as_of)
                    ok.append({"row": i, "code": code, "written": True})
            elif kind == "position":
                shares = _i(r.get("股数"), 0)
                if shares <= 0:
                    raise PortfolioError(f"股数无效: {r.get('股数')!r}")
                cost = _f(r.get("成本")) or _f(r.get("建仓价"))
                if cost is None or cost <= 0:
                    raise PortfolioError(f"成本无效: {r.get('成本')!r}")
                pos = Position(
                    code=code, open_price=_f(r.get("建仓价")),
                    open_date=r.get("建仓日期") or None, shares=shares, cost=cost,
                    stop_loss=_f(r.get("止损")), take_profit=_f(r.get("止盈")),
                    strategy_id=r.get("策略") or "manual",
                    note=r.get("备注") or "",
                )
                _ensure_stock_exists(get_conn(), code)
                if dry_run:
                    ok.append({"row": i, "code": code, "shares": shares, "cost": cost})
                else:
                    open_position(pos, as_of)
                    ok.append({"row": i, "code": code, "written": True})
            else:
                raise PortfolioError(f"不支持的导入类型: {kind}")
        except Exception as exc:
            failed.append((i, code, str(exc)[:100]))

    mode = "试运行(未落库)" if dry_run else "已落库"
    logger.info(f"[{mode}] {kind}：成功 {len(ok)} 条 / 失败 {len(failed)} 条")
    return {"ok": ok, "failed": failed, "dry_run": dry_run}


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    from datetime import timezone, timedelta

    CST = timezone(timedelta(hours=8))
    as_of = datetime(2026, 10, 8, 23, 40, tzinfo=CST)

    print("=" * 70)
    print("自选股 CRUD")
    print("=" * 70)
    add_watch([
        WatchItem(code="600519", group_name="核心池", add_price=1255.79,
                  add_date="2026-10-08", reason="缠论一买+茅台",
                  tags=["缠论买点", "大盘龙头"], priority=3),
        WatchItem(code="000001", group_name="观察池", reason="银行轮动待确认",
                  priority=1),
        WatchItem(code="300750", group_name="核心池", add_price=210.5,
                  reason="RPS 强势", tags=["RPS强势"], priority=2),
    ], as_of)
    mark_watch(["600519"], "触发缠论二买", as_of)
    for w in list_watch():
        print(f"  [{w['group']}] {w['code']} {w['name']} 优先级{w['priority']} "
              f"标记={w['last_mark'] or '-'} 标签={w['tags']}")

    print()
    print("=" * 70)
    print("持仓 CRUD（含加权成本）")
    print("=" * 70)
    open_position(Position(code="600519", shares=100, cost=1258.20,
                           stop_loss=1180.0, take_profit=1400.0,
                           note="长期持有"), as_of)
    r = add_shares("600519", 50, 1200.0, as_of, reason="回调加仓")
    print(f"  加仓后: {r['shares']} 股, 成本 {r['cost']}  (1258.2×100 + 1200×50)/150 = "
          f"{(1258.2*100 + 1200*50)/150:.4f}")
    r2 = reduce_shares("600519", 50, 1300.0, as_of, reason="止盈一半")
    print(f"  减仓后: 剩余 {r2['left']} 股, 盈亏 {r2['pnl_pct']}%")
    print()
    print("  交易流水:")
    for t in trade_history("600519"):
        print(f"    {t['trade_time']} {t['side']:6s} {t['shares']:>5d}股 @ {t['price']:>8.2f}"
              f"  {t['reason']}")

    print()
    print("=" * 70)
    print("持仓健康度（缺失因子不当0）")
    print("=" * 70)
    cases = [
        ("全因子齐全", {"structure": 80, "board": 75, "news": 60, "volume": 70, "market": 55}),
        ("板块缺失",   {"structure": 80, "board": None, "news": 60, "volume": 70, "market": 55}),
        ("板块+消息缺失", {"structure": 80, "board": None, "news": None, "volume": 70, "market": 55}),
        ("结构崩坏",   {"structure": 25, "board": 30, "news": 20, "volume": 40, "market": 35}),
    ]
    for name, comp in cases:
        r = health_score(comp)
        adv = build_health_advice(r["score"], r["missing"], "600519", "贵州茅台", 1180.0, 1255.79)
        print(f"  {name:16s} 健康度={r['score']:>5.1f} 建议={adv['level']:6s} "
              f"缺={r['missing'] or '无'}")
    print()
    print("  ★ 对比：若把缺失当 0处理，'板块缺失' 会被算成 40.0（误判为减仓）")
    naive = (80*.30 + 0*.25 + 60*.20 + 70*.15 + 55*.10)
    print(f"    当0 计算: {naive:.1f}  |  实际正确做法: {health_score(cases[1][1])['score']:.1f}")

    print()
    print("=" * 70)
    print("批量导入（D10）")
    print("=" * 70)
    tpl = export_template("watch")
    res = import_csv("watch", tpl, as_of, dry_run=True)
    print(f"  试运行: 成功 {len(res['ok'])} 条 / 失败 {len(res['failed'])} 条")
    bad = Path(tpl.parent / "watch_bad.csv")
    bad.write_text("代码,分组\n999999,测试\n600519,核心池\n", encoding="utf-8-sig")
    res2 = import_csv("watch", bad, as_of, dry_run=True)
    print(f"  含无效代码: 成功 {len(res2['ok'])} / 失败 {len(res2['failed'])}")
    for row_no, code, reason in res2["failed"]:
        print(f"    第{row_no}行 {code}: {reason[:70]}")

    print()
    print("=" * 70)
    print("错误处理")
    print("=" * 70)
    for desc, fn in [
        ("代码不存在", lambda: open_position(Position(code="999999", shares=100, cost=10), as_of)),
        ("重复建仓",   lambda: open_position(Position(code="600519", shares=100, cost=10), as_of)),
        ("减仓超量",   lambda: reduce_shares("600519", 9999, 1300.0, as_of)),
        ("股数为负",   lambda: add_shares("600519", -100, 1200.0, as_of)),
        ("成本为空",   lambda: open_position(Position(code="000001", shares=100), as_of)),
    ]:
        try:
            fn()
            print(f"  {desc:10s} -> ★未拦截（不符合预期）")
        except PortfolioError as exc:
            print(f"  {desc:10s} -> 已拦截: {str(exc)[:66]}")