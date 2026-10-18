# -*- coding: utf-8 -*-
"""★ 轻量 Web API —— 规格第八章桌面端的最小可用替代。

★★ 为什么是「轻量」而不是 Electron
----------------------------------
规格 M11 要求 Electron + React 桌面端。但实测当前仓库
**没有任何前端**（无 `frontend/`、无 `package.json`、无 `.tsx`），
`release/Hawk-Windows-Portable-*` 里也只有 `backend/ runtime/ scripts/`。

本模块走**轻量路线**：FastAPI 提供只读 JSON API + 静态 HTML/JS。
理由（不是偷懒，是权衡）：
  · 用户明确要求「先用轻量最小可用版」
  · 后端数据/引擎已经很完整，缺的是**展示层**而非业务能力
  · 零构建链（不需要 npm install / electron-builder）⇒ 改完即生效、
    不会因为构建失败拿不到界面
  ⚠ 但规格第八章的部分可视化（板块热力图/轮动图矩形树图）
    在轻量版里**降级**为表格 + 条形图，未完全达标。

★★★ 四条纪律在本模块的落法
1. **接口 + 注册表**：本模块只做「编排 + 序列化」，
   业务算法全部委托给 `backend.domain.*` / `scripts/*` 的既有实现，
   **不复制任何计算逻辑**（避免两套口径必然对不上）。
2. **纯函数 / 时间注入**：所有端点都必须带 `as_of` 参数，
   **禁止 `datetime.now()`**（铁律二）。前端不传时用「库内最新交易日」。
   写操作（加自选）也不例外——`as_of` 是**必填** query 参数。
3. **配色**：涨=红、跌=绿（`COLOR_UP` / `COLOR_DOWN` 与全局一致，不得反转）。
4. **残缺日必须显式拦截**：复用 `data_gate`，不让前端在
   2026-10-09（仅 108 只）这种日子上做横截面判断。

★★★ D13 铁律：本模块**不得**返回任何「建议卖出 / 应立即 / 必须卖出 /
   立即减仓 / 清仓」类文案。卖点只以 `HOLD` + note 的提醒形式出现。
   `tests/test_webapi.py::test_no_sell_instruction_wording` 守护这条。

读 DuckDB 一律用 `duck.get_ro_conn()`（与写入分离，避免抢锁）。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.data import duck                     # noqa: E402
from backend.domain import chan, strength         # noqa: E402
from backend.domain import portfolio              # noqa: E402
# ★ 两江算力平台适配层（只读）。放在最后导入是因为它依赖**另一个工作区**
#   的客户端模块，缺了也不该让整个 API 起不来 ⇒ 导入失败要降级而非崩。
try:
    from backend.services import liangjiang       # noqa: E402
except Exception as _lj_err:                      # noqa: BLE001
    class _LJFallback:
        PlatformUnavailable = RuntimeError

        @staticmethod
        def probe() -> dict:
            return {"state": "unavailable",
                    "message": f"两江适配层未加载：{_lj_err}"}
    liangjiang = _LJFallback()          # type: ignore[assignment]

CST = timezone(timedelta(hours=8))
STATIC_DIR = _ROOT / "backend" / "web"
SQLITE_PATH = _ROOT / "data" / "hawk.sqlite"

# ★ 配色铁律：涨=红、跌=绿（A股习惯，与全局一致，不得反转）
COLOR_UP = "#e0392e"        # 红
COLOR_DOWN = "#1a9e5f"      # 绿
COLOR_FLAT = "#8a8f99"

# ★ D13 禁用文案清单。测试按此清单反查源码与 API 返回。
FORBIDDEN_SELL_WORDS = ("建议卖出", "应立即", "必须卖出", "立即减仓", "清仓")

from backend.services.news_collection import NewsCollector


@asynccontextmanager
async def _lifespan(application):
    collector = NewsCollector(SQLITE_PATH)
    application.state.news_collector = collector
    collector.start()
    from backend.services.mail_notifications import MailNotifications
    notifications = MailNotifications(SQLITE_PATH)
    application.state.mail_notifications = notifications
    notifications.start()
    from backend.services.intraday import IntradayService
    intraday = IntradayService(SQLITE_PATH)
    application.state.intraday = intraday
    intraday.start()
    try:
        yield
    finally:
        intraday.stop()
        notifications.stop()
        collector.stop()


app = FastAPI(lifespan=_lifespan, title="Hawk 轻量端", version="0.1.1",
              description="决策副驾 · 只读视图 · 不接券商下单接口")


@app.get('/api/intraday/{code}')
def intraday_chart(code: str, period: str = 'm1'):
    from backend.services.intraday import IntradayService, PERIODS, wallclock
    if len(code) != 6 or not code.isascii() or not code.isdigit() or period not in PERIODS:
        raise HTTPException(422, '需 6 位股票代码，周期支持 m1/m5/m30/m60')
    service = getattr(app.state, 'intraday', None) or IntradayService(SQLITE_PATH)
    return service.read(code, period, wallclock())


@app.get("/api/mail/status")
def mail_status():
    from backend.services.mail_notifications import notification_status
    service = getattr(app.state, "mail_notifications", None)
    return service.status() if service else notification_status(SQLITE_PATH)


@app.get("/api/news/sources")
def news_sources():
    """当前采集运行状态（基础设施墙钟），与历史交易日查询分离。"""
    from backend.services.news_collection import source_status
    collector = getattr(app.state, "news_collector", None)
    return collector.status() if collector else source_status(SQLITE_PATH)


@app.get("/api/news/tracked")
def tracked_news(code: str | None = None, limit: int = Query(50, ge=1, le=200)):
    """当前自选/持仓最近七天的逐股新闻判断，不混入历史行情日。"""
    from backend.services.news_collection import wallclock
    from backend.services.tracked_news import read_tracked
    result = read_tracked(wallclock(), SQLITE_PATH, code, limit)
    collector = getattr(app.state, "news_collector", None)
    result["analysis"] = collector.analysis_status if collector else {"state": "not_running"}
    return result


# ═══════════════════════════════════════════════════════ 通用工具
def _latest_day() -> str:
    con = duck.get_ro_conn()
    row = con.execute("SELECT CAST(max(trade_date) AS VARCHAR) "
                      "FROM kline_daily").fetchone()
    if not row or not row[0]:
        raise HTTPException(503, "库内无行情数据，请先运行 sync_daily")
    return str(row[0])


def _resolve_day(as_of: str | None) -> tuple[str, dict]:
    """返回 `(实际使用的交易日, 说明 dict)`。

    ★★ 为什么默认不是「库内最新交易日」
    ------------------------------------
    实测`max(trade_date) = 2026-10-09` 只有 **108 只**（残缺日），
    而 `data_gate` 判定横截面可用需要 ≥2779 只。若默认落到残缺日，
    个股详情/推荐两个页面会**一打开就409**，用户看到的是「什么都没」。

    所以规则是：
      · 前端**显式传**as_of ⇒ 照用（残缺日由 `_require_usable` 报 409，不静默改写）
      · 前端**不传** ⇒ 自动落到`data_gate` 判定的最近可用交易日，
        并把「requested vs used」如实回传，让页面能显示「已回退」
    ⚠ 关键：**回退必须显式告知**，否则就成了「静默给错数据」。
    """
    usable, _ = _usable_days()
    latest_raw = _latest_day()
    if as_of:
        day = str(as_of)[:10]
        return day, {"requested": day, "used": day, "fallback": False}
    if usable and usable[-1] > latest_raw:
        # 理论上不该发生（usable 必为 max 之前的子集），但显式防御
        latest_raw = usable[-1]
    day = usable[-1] if usable else latest_raw
    note = {"requested": latest_raw, "used": day, "fallback": day != latest_raw,
            "usable_days": len(usable)}
    return day, note


def _as_of_or_latest(as_of: str | None) -> str:
    """把前端传来的 as_of 归一到**可用交易日**（见 `_resolve_day`）。

    ★ 为什么必须归一：前端不传时会用浏览器本地时间，
      而那是**非交易时刻**（周末/收盘后）⇒ 查到空数据 ⇒ 界面「什么都没」。
      项目的铁律是「时间必须注入」，但注入的应当是**业务时点**
      （交易日），不是墙钟。
    """
    return _resolve_day(as_of)[0]


def _dt(day: str) -> datetime:
    return datetime.fromisoformat(f"{day} 15:00:00").replace(tzinfo=CST)


def _f(v: Any) -> float | None:
    """None 安全的 float。★ 缺失一律返回 None，绝不当 0（否则拉低均值）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN 也视为缺失


def _pct(v: Any) -> float | None:
    """把库里的**百分数**归一成**比例**（0.01 表示 1%）。

    ★★★ 为什么要归一，而不是在前端乘/除
    ------------------------------------
    实测`kline_daily.pct_chg` 与 `board_strength.pct_chg` 存的都是**百分数**
    （茅台2026-10-08 `pct_chg = -0.2248` ⇒ 实际是**跌 0.22%**，
      因为 `(close/pre_close-1)*100 = -0.2248` 在 5204/5204 行上全部成立）；
    而 `up_ratio` / `broken_rate` 是 0~1 的**比例**。
    两套单位混在同一个响应里，前端一旦统一乘 100就会把 -0.22% 显示成 -22%。

    ⇒ 铁律：**跨层边界必须归一**。前端只接受 0~1 比例，
      展示时再乘 100，格式化逻辑只有一处。
    ⚠ 判据不是「字段名叫 pct」——`winrate` 之类也是 0~1，
      靠名字猜单位正是项目已踩过的坑。这里逐字段显式转换。
    """
    f = _f(v)
    return None if f is None else f / 100.0


def _sqlite() -> sqlite3.Connection:
    return sqlite3.connect(str(SQLITE_PATH))


def _usable_days() -> tuple[list[str], int]:
    """返回 (可用于横截面统计的交易日列表, 阈值)。

    ★ 复用 `data_gate` 而不是自己数股票 —— 否则会出现两套完备度口径，
      而项目已经吃过「两套口径必然对不上」的亏。
    """
    from backend.domain import data_gate as G
    cov = G.coverage_from_duck(duck.get_ro_conn())
    return list(cov.usable), int(cov.threshold)


def _require_usable(day: str) -> None:
    """残缺日 ⇒ 409。不静默返回错数据（错数据比报错更贵）。"""
    usable, th = _usable_days()
    if day not in set(usable):
        raise HTTPException(
            409, f"{day} 是残缺日（当日标的数不足 {th}），"
                 f"不可用于横截面判断；请改用可用交易日。")


# ═══════════════════════════════════════════════════════ 页面
@app.get("/")
def index() -> FileResponse:
    if not (STATIC_DIR / "index.html").exists():
        raise HTTPException(404, "静态页未安装：backend/web/index.html 缺失")
    return FileResponse(STATIC_DIR / "index.html")


# ═══════════════════════════════════════════════════════ 概览
@app.get("/api/overview")
def api_overview(as_of: str | None = Query(None)) -> dict:
    """首页概览：最新交易日、全市场成交额、涨跌家数、市场状态。

    ★ 成交额一律 `volume × close` 自己算——`kline_daily.amount` 全库为 NULL，
      而 `kline_min.amount` 已废弃（逐股漂移，实测跨 4 个数量级）。
    """
    from backend.domain import regime

    day, note = _resolve_day(as_of)
    con = duck.get_ro_conn()
    usable, th = _usable_days()
    day_usable = day in set(usable)
    latest_raw = _latest_day()

    amt, n_stock = con.execute(
        "SELECT sum(volume * close) / 1e8, count(DISTINCT code) "
        "FROM kline_daily WHERE trade_date = ?", [day]).fetchone()

    # ★ 涨跌家数必须用「同一 code 的上一根K 线收盘」作分母。
    #   库内有 `pre_close` 列，但实测只有 15748/16800 ≈ 93.7% 与 lag(close) 一致
    #   ⇒ 直接信它会在 6.3% 的样本上算错，故一律用窗口函数自己取 lag。
    up = down = flat = None
    base = None
    if day_usable:
        r = con.execute(
            """
            SELECT sum(CASE WHEN close >  prev THEN 1 ELSE 0 END),
                   sum(CASE WHEN close <  prev THEN 1 ELSE 0 END),
                   sum(CASE WHEN close =  prev THEN 1 ELSE 0 END),
                   count(prev)
            FROM (
              SELECT code, trade_date, close,
                     lag(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev
              FROM kline_daily WHERE trade_date <= ?
            ) WHERE trade_date = ? AND prev IS NOT NULL AND prev > 0
            """, [day, day]).fetchone()
        if r:
            up, down, flat, base = (int(r[0] or 0), int(r[1] or 0),
                                    int(r[2] or 0), int(r[3] or 0))

    # 市场状态：直接读 market_regime（regime 模块**没有** query_states，
    #  它只有纯函数 analyze_market/score_market，历史结果存在表里）
    mkt = None
    row = con.execute(
        "SELECT CAST(ts AS VARCHAR), state, score, position_cap, "
        "       limit_up_cnt, limit_down_cnt, broken_rate, up_ratio "
        "FROM market_regime WHERE cast(ts AS date) <= ? "
        "ORDER BY ts DESC LIMIT 1", [day]).fetchone()
    if row:
        mkt = {"ts": row[0], "state": row[1], "score": _f(row[2]),
               "position_cap": _f(row[3]), "limit_up_cnt": row[4],
               "limit_down_cnt": row[5], "broken_rate": _f(row[6]),
               "up_ratio": _f(row[7]),
               # 状态中文标签/仓位上限来自 REGIME_META（唯一真源，不在前端硬编码）
               "state_label": (regime.REGIME_META.get(row[1]) or {}).get("label"),
               "cap_hint": (regime.REGIME_META.get(row[1]) or {}).get("cap")}
    if mkt and mkt["state"] and mkt["ts"][:10] != day:
        mkt["stale_days"] = True
        mkt["note"] = f"market_regime 最新只到 {mkt['ts'][:10]}，非 {day}"

    return {
        "trade_date": day,
        "data_usable": day_usable,
        "latest_raw_day": latest_raw,
        "day_resolution": note,
        "universe_threshold": th,
        "n_stock": int(n_stock or 0),
        "total_amount_yi": _f(amt),
        "up": up, "down": down, "flat": flat, "breadth_base": base,
        "market": mkt,
        "colors": {"up": COLOR_UP, "down": COLOR_DOWN, "flat": COLOR_FLAT},
        "ts_note": "as_of 归一到库内交易日，不使用墙钟（铁律二）",
    }


# ═══════════════════════════════════════════════════════ 板块强度
@app.get("/api/boards")
def api_boards(as_of: str | None = Query(None), limit: int = 40) -> dict:
    """板块强度榜。读已算好的 `board_strength` 快照，不在前端重算因子。

    ★★ 为什么这里的日期**不能**跟其它端点用同一个 `_as_of_or_latest`
    -----------------------------------------------------------
    实测`board_strength` 里的 `ts` 是**采集时刻**，不是交易日：
      · 2026-10-09 有 383 行，但 `pct_chg` / `limit_up_cnt` **全为 NULL**
        （那一轮只抓到板块列表，没抓到行情因子）
      · 2026-10-10 有 50 行，是唯一带 `score_method=partial_factor_v1`
        的完整快照
    而 `kline_daily` 的可用日是 2026-10-08。
    ⇒ 若统一用「最近可用交易日 10-08」去取板块，**页面会是空的**
      （实测 n_total=0），而接口**不会报错** —— 这类「静默给空」最费时间。

    所以：前端显式传 as_of 就照用；不传则取**板块表自身最新的一天**，
    并把两个日期都回传，让页面能显示「板块快照日 ≠ 行情日」。

    ★★ 第二条回退规则（实测踩到）：**前端会把行情日当 as_of 传进来**
    （概览页把它存成全局 `S.day`，每个请求都带上）。
    而板块快照日 ≠ 行情日（实测行情可用日 10-08、板块快照 10-10），
    于是「照用」会取到 0 行 ⇒ **页面空白但接口不报错**。
    ⇒ 若 as_of 当天在板块表里**没有任何行**，自动回退到快照日，
      并在响应里写明 `fallback_reason`。静默给空是最贵的失败模式。
    """
    con = duck.get_ro_conn()
    snap_day = con.execute(
        "SELECT CAST(max(cast(ts AS DATE)) AS VARCHAR) FROM board_strength"
    ).fetchone()[0]
    if not snap_day:
        return {"trade_date": None, "n_total": 0, "rows": [],
                "n_history_days": 0, "weights": strength.WEIGHTS,
                "note": "board_strength 为空，先跑 sync_boards"}

    fallback_reason = None
    if as_of:
        day = str(as_of)[:10]
        has = con.execute(
            "SELECT count(*) FROM board_strength WHERE cast(ts AS DATE) = ?",
            [day]).fetchone()[0]
        if not has:
            fallback_reason = (f"板块表在 {day} 无任何行"
                               f"（前端传入的是行情日，板块快照是采集日）")
            day = str(snap_day)
    else:
        day = str(snap_day)

    rows = con.execute(
        "SELECT board_code, board_name, type, strength, rank, net_inflow, "
        "       up_ratio, pct_chg, factor_coverage, score_method, "
        "       limit_up_cnt, max_streak, leader_code "
        "FROM board_strength WHERE cast(ts as date) = ? "
        #★★★ 排序必须「覆盖度优先、强度其次」
        #   `score_boards` 对缺失因子做的是**按可得权重归一化**
        #   （`strength = total / coverage`），于是 coverage 0.15、
        #   只有 d5 一项的 concept 也能拿到 100.0 分——
        #   **分数高低由「缺了多少因子」决定，不由板块强弱决定**。
        #   实测：5 连板的「电池」（coverage 0.92）strength 85.4，
        #   排在一个只有资金流的 concept（coverage 0.15）之后。
        #   ⇒ 榜首先按 factor_coverage 降序，让「可信的强板块」在前；
        #   同覆盖度内再按强度。**不改变 strength 的语义**
        #   （那属于策略口径，是 D 系列决策），只改呈现顺序。
        "ORDER BY factor_coverage DESC NULLS LAST, strength DESC NULLS LAST",
        [day]).fetchall()
    cols = ("code", "name", "type", "strength", "rank", "net_inflow",
            "up_ratio", "pct_chg", "factor_coverage", "score_method",
            "limit_up_cnt", "max_streak", "leader_code")
    out = []
    for r in rows[:max(1, limit)]:
        d = dict(zip(cols, r))
        for k in ("strength", "net_inflow", "up_ratio", "factor_coverage"):
            d[k] = _f(d[k])
        #★ pct_chg 是百分数 ⇒ 归一成比例（见 _pct 注释）
        d["pct_chg"] = _pct(d["pct_chg"])
        out.append(d)
    # ★ 必须显式报告历史深度：板块强度只回填 2 天，
    #   前端不能把单日快照当成趋势。
    n_days = con.execute("SELECT count(DISTINCT cast(ts AS DATE)) "
                         "FROM board_strength").fetchone()[0]
    n_factor = con.execute(
        "SELECT count(*) FROM board_strength WHERE cast(ts AS DATE) = ? "
        "AND pct_chg IS NOT NULL", [day]).fetchone()[0]
    #★★★ coverage 必须**现算**，不能硬编码
    #   原note 写死「仅 0.28」，而回填后库内实测均值 0.195、
    #   有成分的板块达0.82 ⇒ 页面长期挂着一句**已经与事实脱节**的话。
    #   同类风险：文案里出现的任何数字都应来自查询，否则改一次数据
    #   就留一句假话在页面上（用户无从判断哪个是真的）。
    #★★★ 「有成分的板块数」必须按**成分数本身**判，不能按 coverage 阈值
    #   拍脑袋：只拿到 d1+d5 的 concept 也有 0.30 > 0.2，
    #   实测报 50 个而真正取到成分的只有 17 个 ⇒ 页面再次挂上假数字。
    #   正解是读 `factors_json._meta.members_known`（回填脚本如实写的样本量）。
    n_rich = int(con.execute(
        "SELECT count(*) FROM board_strength WHERE cast(ts AS DATE) = ? "
        "AND TRY_CAST(json_extract_string(factors_json, "
        "           '$._meta.members_known') AS INTEGER) > 0",
        [day]).fetchone()[0] or 0)
    cov_stat = con.execute(
        "SELECT avg(factor_coverage), max(factor_coverage) "
        "FROM board_strength WHERE cast(ts AS DATE) = ?", [day]).fetchone()
    cov_avg = float(cov_stat[0]) if cov_stat and cov_stat[0] is not None else None
    cov_max = float(cov_stat[1]) if cov_stat and cov_stat[1] is not None else None
    return {"trade_date": day, "snapshot_latest": str(snap_day),
            "fallback_reason": fallback_reason,
            "n_total": len(rows), "rows": out,
            "n_history_days": int(n_days or 0),
            "n_with_factor": int(n_factor or 0),
            "coverage_avg": cov_avg,
            "coverage_max": cov_max,
            "n_with_members": n_rich,
            "weights": strength.WEIGHTS,
            "note": (f"factor_coverage 均值 {cov_avg:.3f}、最高 {cov_max:.2f}"
                     if cov_avg is not None else "factor_coverage 全空")
                    + f"；仅 {n_rich}/{len(rows)} 个板块取到成分股"
                      "（同花顺翻页 401，子集口径），其余基本只有 d1+d5。"
                      "（d4 最高连板由本地 K 线推出；d8 rank 斜率因"
                      "板块历史不足 3 天仍为空），"
                      "score_method 为 partial_factor_v1（缺失记 None，不当 0）。"
                      f"板块强度历史仅 {n_days} 天，暂不能看趋势。"}


# ═══════════════════════════════════════════════════════ 个股详情
@app.get("/api/stock/{code}")
def api_stock(code: str, as_of: str | None = Query(None),
              limit: int = 250) -> dict:
    """个股详情：K 线 + 缠论买卖点 + 中枢位置。

    ★ 复用 `chan.analyze`（纯函数，as_of 注入）—— 不在前端重算缠论。
    ★ `clean_bars` 需要 key `high/low/open/close/volume/trade_date`，
      少一个就整根被静默剔除 ⇒ 字段名必须与库列名一致。
    """
    code = code.strip()
    if not (len(code) == 6 and code.isdigit()):
        raise HTTPException(400, f"非法股票代码：{code}")

    day = _as_of_or_latest(as_of)
    _require_usable(day)

    # duck.get_daily 已是升序 + 正确列名，直接复用（不重复造查询）
    rows = [r for r in duck.get_daily(code, limit=limit)
            if str(r["trade_date"])[:10] <= day]
    if not rows:
        raise HTTPException(404, f"{code} 在 {day} 及之前无日线数据")
    bars = [{**r, "trade_date": str(r["trade_date"])[:10]} for r in rows]

    res = chan.analyze(code, bars, _dt(day))

    name = code
    try:
        db = _sqlite()
        r = db.execute("SELECT name FROM stock WHERE id = ?", [code]).fetchone()
        db.close()
        if r and r[0]:
            name = r[0]
    except sqlite3.Error:
        pass

    last_zs = res.last_zhongshu()
    return {
        "code": code, "name": name, "trade_date": day,
        "bars": [{"d": b["trade_date"], "o": _f(b["open"]), "h": _f(b["high"]),
                   "l": _f(b["low"]), "c": _f(b["close"]), "v": _f(b["volume"]),
                   "pct": _pct(b.get("pct_chg"))} for b in bars],
        "position": res.position(),
        "bi_direction": res.bi_direction(),
        "params": res.params,
        "signals": [{"kind": s.kind, "level": s.level, "price": _f(s.price),
                     "date": s.date, "reason": s.reason,
                     "confidence": _f(s.confidence)} for s in res.signals],
        "zhongshus": [{"idx": z.idx, "high": _f(z.high), "low": _f(z.low),
                       "start": z.start_date, "end": z.end_date,
                       "bi_count": z.bi_count,
                       "is_last": bool(getattr(z, "is_last", False))}
                      for z in res.zhongshus],
        "last_zhongshu": ({"high": _f(last_zs.high), "low": _f(last_zs.low),
                           "start": last_zs.start_date, "end": last_zs.end_date}
                          if last_zs else None),
        "note": "卖点仅作 K 线提醒标注；系统不自动卖出、不给卖出指令（决策 D13）",
    }


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1), limit: int = 20) -> dict:
    """代码/名称搜索（sqlite 侧，用于选股）。"""
    try:
        db = _sqlite()
        rows = db.execute(
            "SELECT id, name FROM stock WHERE id LIKE ? OR name LIKE ? "
            "ORDER BY id LIMIT ?", [f"%{q}%", f"%{q}%", limit]).fetchall()
        db.close()
    except sqlite3.Error as exc:
        raise HTTPException(503, f"读股票基础库失败：{exc}") from exc
    return {"rows": [{"code": r[0], "name": r[1]} for r in rows]}


# ═══════════════════════════════════════════════════════ 自选股
@app.get("/api/watchlist")
def api_watchlist(as_of: str | None = Query(None)) -> dict:
    """自选股列表 + 最新收盘价（价格走 DuckDB 行情）。"""
    day = _as_of_or_latest(as_of)
    items = portfolio.list_watch()
    if not items:
        return {"trade_date": day, "rows": [],
                "note": "自选股为空，可在「自选股」页搜索添加"}
    con = duck.get_ro_conn()
    codes = [i["code"] for i in items]
    marks = ",".join("?" * len(codes))
    px = con.execute(
        f"SELECT code, close, pct_chg, volume FROM kline_daily "
        f"WHERE trade_date = ? AND code IN ({marks})", [day, *codes]).fetchall()
    px_map = {r[0]: {"close": _f(r[1]), "pct_chg": _pct(r[2]), "volume": _f(r[3])}
              for r in px}
    rows = [{**it, **(px_map.get(it["code"])
                     or {"close": None, "pct_chg": None, "volume": None})}
            for it in items]
    return {"trade_date": day, "rows": rows}


@app.post("/api/watchlist")
def api_watch_add(payload: dict,
                  as_of: str = Query(..., description="交易日 YYYY-MM-DD，"
                                                 "必填：时间注入不可省")) -> dict:
    """加入自选。

    ★★ `as_of` 是**必填 query 参数**，不是可选。
      曾经写成 `datetime.now(CST)` —— 直接违反铁律二，
      且后果是「同一天重复添加的记录时间戳不同、无法按日幂等」。
    """
    code = str(payload.get("code", "")).strip()
    if not (len(code) == 6 and code.isdigit()):
        raise HTTPException(400, f"非法股票代码：{code!r}")
    name = str(payload.get("name", "")).strip()
    group = payload.get("group") or "核心池"
    reason = payload.get("reason") or "手动添加"
    try:
        n = portfolio.add_watch(
            [portfolio.WatchItem(code=code, name=name or code,
                                 group_name=group, reason=reason)],
            _dt(as_of))
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(400, f"加入自选失败：{exc}") from exc
    return {"ok": True, "added": n, "as_of": as_of}


@app.delete("/api/watchlist/{code}")
def api_watch_del(code: str,
                  as_of: str = Query(..., description="交易日 YYYY-MM-DD，必填")) -> dict:
    try:
        n = portfolio.remove_watch([code], _dt(as_of))
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(400, f"删除失败：{exc}") from exc
    return {"ok": True, "removed": n, "as_of": as_of}


# ═══════════════════════════════════════════════════════ 推荐
# ★ 轻量缓存：推荐一次要遍历 5359 只做缠论+RPS（实测 12s），
#   页面每次刷新都重算既慢又会让「同一页多次刷新得到不同结果」变得难以排查。
#   键是 **(交易日, limit)** —— 换日自然失效；⚠ 不是墙钟TTL，
#   因为「缓存过期」在这里等价于「换了业务时点」，而那必须由 as_of 决定。
_RECO_CACHE: dict[tuple, dict] = {}


def _cache_key(day: str, limit: int) -> tuple:
    from backend.domain.image_strategy import load_policy
    return (day, max(1, limit), load_policy()["policy_hash"])


@app.get("/api/image-strategy")
def api_image_strategy() -> dict:
    import json
    from backend.domain.image_strategy import load_policy
    catalog = _ROOT / "data/image_distillation/20261011/distilled.json"
    return {"policy": load_policy(), "catalog": json.loads(catalog.read_text(encoding="utf-8"))}


@app.get("/api/recommend")
def api_recommend(as_of: str | None = Query(None), limit: int = 20) -> dict:
    """每日推荐。

    ★ 直接调用 `scripts/daily_recommend.build_rows` + `recommend.recommend`
      —— 与邮件/命令行走**同一套**口径，避免出现「网页与邮件结论不一致」。
    """
    import importlib.util

    day = _as_of_or_latest(as_of)
    _require_usable(day)
    ck = _cache_key(day, limit)
    if ck in _RECO_CACHE:
        return {**_RECO_CACHE[ck], "cached": True}

    spec = importlib.util.spec_from_file_location(
        "_daily_recommend", _ROOT / "scripts" / "daily_recommend.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    try:
        rows = mod.build_rows(_dt(day))
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(500, f"推荐数据组装失败：{exc}") from exc
    if not rows:
        return {"trade_date": day, "rows": [], "n": 0,
                "note": "当日无满足硬性过滤（非ST/非停牌/有K 线）的标的"}

    from backend.domain import recommend as R
    # ★ `recommend()` 返回的是 **dict**（含 recommendations / rejections / stats），
    #   不是 tuple —— 早期写成 `res[0] if isinstance(res, tuple)` 会静默拿到
    #   dict 的第 0 个 key（字符串）⇒ 前端拿到「不可迭代」才报错，定位成本极高。
    #market_state 用 market_regime 实际状态，不用默认 "repair" 硬编码
    #★ 推荐本身必须按**当日真实市场状态**打分 —— 写死 "repair" 会让
    #   震荡市里也按修复市给仓位建议（D16：买点 alpha 只在上涨市有效）。
    st_row = duck.get_ro_conn().execute(
        "SELECT state, position_cap FROM market_regime "
        "WHERE cast(ts AS DATE) <= ? ORDER BY ts DESC LIMIT 1", [day]).fetchone()
    m_state = st_row[0] if st_row else "repair"
    m_cap = _f(st_row[1]) if st_row else 0.40
    res = R.recommend(rows, _dt(day), market_state=m_state,
                      position_cap=m_cap if m_cap is not None else 0.40)

    # 补收盘价/涨跌幅（to_dict 里没有，前端表格需要）
    codes = [r["code"] for r in res["recommendations"]]
    px_map: dict[str, dict] = {}
    if codes:
        marks = ",".join("?" * len(codes))
        px = duck.get_ro_conn().execute(
            f"SELECT code, close, pct_chg FROM kline_daily "
            f"WHERE trade_date = ? AND code IN ({marks})", [day, *codes]).fetchall()
        px_map = {r[0]: {"close": _f(r[1]), "pct_chg": _pct(r[2])} for r in px}

    out = []
    for r in res["recommendations"][:max(1, limit)]:
        d = dict(r)
        d.update(px_map.get(d["code"]) or {"close": None, "pct_chg": None})
        out.append(d)

    payload = {
        "trade_date": day, "rows": out,
        "n": len(res["recommendations"]),
        "market_state": res["market_state"],
        "position_cap": res["position_cap"],
        "total_input": res["total_input"],
        "after_filter": res["after_filter"],
        "rejected": res["rejected"],
        "rejections": res["rejections"][:20],
        "stats": res["stats"],
        "image_policy": res.get("image_policy"),
        "cached": False,
        "note": "每条推荐必带 risk_note；系统不自动交易、不给卖出指令（D13）",
    }
    # 只缓存「完整结果」，不同 limit 各自算：缓存整份再切片会串味
    _RECO_CACHE[ck] = {**payload, "rows": out}
    return payload


# ═══════════════════════════════════════════════════════ 持仓
@app.get("/api/positions")
def api_positions() -> dict:
    """持仓 + 交易历史。**只读视图**：本系统不接券商下单接口。"""
    try:
        pos = portfolio.list_positions(holding_only=True)
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(500, f"读持仓失败：{exc}") from exc
    return {
        "rows": [{"code": p.code, "name": p.name, "shares": p.shares,
                  "cost": _f(p.cost), "stop_loss": _f(p.stop_loss),
                  "take_profit": _f(p.take_profit),
                  "status": p.status, "note": p.note,
                  # ★ 明确标出「规模未知」——否则前端会把 None 渲染成 0，
                  #   页面看起来像「持有 0 股」，而事实是「持有但没录股数」。
                  "size_unknown": p.shares is None,
                  # ★ 止损位是**参考**，不是指令；D13 不代选
                  "stop_note": "止损位为参考档位，需自行判断，系统不代为决策"}
                 for p in pos],
        "history": portfolio.trade_history(limit=20),
        "n_size_unknown": sum(1 for p in pos if p.shares is None),
        "note": "持仓数据需在 M6 手工/记账入库；本端只读，不下单",
    }


# ═══════════════════════════════════════════════════════ LLM 自检与成本
@app.get("/api/llm/status")
def api_llm_status(as_of: str | None = Query(None)) -> dict:
    """LLM 用量与成本统计（规格 2.4 要求 4/ M3「成本统计」）。

    ★ 成本口径必须说清：本地 7B 的 cost_cny 恒为 0，这是**真实成本**不是占位。
      真正有用的是 token 与耗时 —— 它们决定「本地够不够快」。
    ★ 统计窗口以 as_of 为锚，不取系统日期 ⇒ 回放历史时结果同样可复现。
    """
    from backend.data.sqlite import llm_cost_summary

    day = _resolve_day(as_of)[0]
    days = 30
    try:
        s = llm_cost_summary(day, days)
    except sqlite3.Error as exc:
        raise HTTPException(500, f"读llm_audit 失败：{exc}") from exc

    rows = s["rows"]
    tot_calls = sum(r["n_calls"] for r in rows)
    tot_tok = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows)
    tot_ms = sum(r["elapsed_ms"] for r in rows)
    tot_cost = sum(r["cost_cny"] for r in rows)
    n_fallback = sum(r.get("n_fallback", 0) for r in rows)
    n_fail = sum(r.get("n_fail", 0) for r in rows)
    return {
        **s,
        "totals": {
            "n_calls": tot_calls, "n_tokens": tot_tok,
            "elapsed_ms": round(tot_ms, 1),
            "cost_cny": round(tot_cost, 6),
            "avg_ms_per_call": round(tot_ms / tot_calls, 1) if tot_calls else 0.0,
            "n_fallback": n_fallback, "n_fail": n_fail,
        },
        "cost_note": (
            "本地 7B 的调用成本按 0 计（真实开销是电费与硬件折旧，"
            "不摊到单次调用）。切换云端 provider 后本表即自动出现真实金额。"),
        "speed_note": (
            f"单次平均 {tot_ms / tot_calls:.0f}ms。"
            if tot_calls else "窗口内无调用记录。")
            + ("超过 2000ms 说明本地推理已接近交互上限，批量打标会拖慢调度。"
               if tot_calls and tot_ms / tot_calls > 2000 else ""),
    }


@app.get("/api/llm/health")
def api_llm_health(as_of: str = Query(..., description="业务时点，必填（铁律二）"),
                   probe: bool = Query(False, description="是否真发一次推理请求")) -> dict:
    """LLM 连通性自检（规格 2.4 硬性要求 5「一键测试两个 provider」）。

    ★ 默认只查 `/api/tags`（秒回）；`probe=true` 才**真发一次推理**
      ——「服务在线但模型加载失败/显存不足」只有真推理才暴露。
      真推理要 1~2 秒，所以做成显式开关而不是默认动作。

    ★ 返回结构化结果而非 bool：自检页要显示耗时与 token，
      布尔值让「慢但可用」和「快但可用」无法区分。
    """
    from backend.domain.llm import OllamaProvider

    day = _resolve_day(as_of)[0]
    p = OllamaProvider()
    if probe:
        out = p.connectivity_check(_dt(day))
        out["probed"] = True
        return out
    loaded = p.available()
    # ★ reachable 直接由 available() 推出，**不单独再探一次**：
    #   available() 内部成功本身就证明服务可达，分开探必然出现
    #   「不可达 但 模型已就绪」这种自相矛盾的输出（实测踩过）。
    return {"provider": p.name, "model": p.model, "reachable": loaded,
            "model_loaded": loaded, "infer_ok": False, "elapsed_ms": 0.0,
            "err": "" if loaded else "模型未就绪（或 Ollama 服务未启动）",
            "probed": False,
            "hint": "加 &probe=true 可真发一次推理（约 1~2 秒）"}


# ═══════════════════════════════════════════════════════ 系统状态
@app.get("/api/ready")
def api_ready() -> dict:
    """Process readiness only; data quality belongs to /api/health."""
    return {"service": "hawk-webapi", "ready": True, "root": str(_ROOT)}


@app.get("/api/health")
def api_health() -> dict:
    """调度任务与数据健康度。"""
    from backend.scheduler import TASK_TABLE

    con = duck.get_ro_conn()
    usable, th = _usable_days()
    latest = _latest_day()
    news = 0
    try:
        db = _sqlite()
        news = db.execute("SELECT count(*) FROM news").fetchone()[0]
        db.close()
    except sqlite3.Error:
        pass
    return {
        "latest_day": latest,
        "tasks": [{"name": t[0], "cron": t[1], "desc": t[-1]} for t in TASK_TABLE],
        "n_tasks": len(TASK_TABLE),
        "usable_days": len(usable),
        "universe_threshold": th,
        "latest_usable_day": usable[-1] if usable else None,
        "n_news": news,
        "role": "决策副驾 · 不接券商下单接口 · 涨红跌绿",
        "version": app.version,
    }


# 挂静态资源（必须放最后，避免覆盖 /api 路由）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ★★ 两江算力平台状态（只读探测，不建任务、不下单）
@app.get("/api/platform")
def api_platform() -> dict:
    """两江算力平台的连通性与配额现状。

    ★★ 三态必须分开返回，**不能一律 500**：
      · `ok` —— 可达且登录态有效
      · `unauthorized` —— 能连但 token 失效（提示去重新登录）
      · `unreachable` —— 网络不通（内网/VPN 未连）
    混成一个「失败」会让前端无从提示，而这两者的处置完全不同。
    ★ 探测**可能慢**（内网不通时会等到超时），故必须走只读连接
      并给出明确的 `state`，而不是让前端等一个 HTTP 500。
    """
    try:
        return liangjiang.probe()
    except liangjiang.PlatformUnavailable as e:
        return {"state": "unavailable", "message": str(e)}
    except Exception as exc:                       # noqa: BLE001
        #★ 探测失败也返回 200 + state，而不是抛 500：
        #   500 会让前端把它当成「接口崩了」，而实际只是平台连不上。
        return {"state": "error",
                "message": f"{type(exc).__name__}: {exc}"}


# ★ favicon：浏览器会自动请求 /favicon.ico，缺它每次刷新都产生一个 404。
#   无功能影响，但它会让「网络零非200」这条验收判据永远不干净 ⇒ 明确给一个空图标。
#   选 204而不是 200 空文件：语义更准（明确表示"没有资源"），且不污染缓存。
@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


def main() -> None:                                # pragma: no cover
    import uvicorn
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8848
    print(f"Hawk 轻量端： http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":                         # pragma: no cover
    main()
