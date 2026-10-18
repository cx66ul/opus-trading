# -*- coding: utf-8 -*-
"""
Hawk · 全量数据同步（附录 C 增量同步与回填策略）

分四个子任务，各自独立水位、互不阻塞：
  sync_stock_list()   全市场股票列表（新浪 hs_a 单节点，实测覆盖沪深京）
  sync_daily()        日线历史（腾讯 qfq，分批 + 断点续传）
  sync_minutes()      跟踪池分钟线（仅跟踪池，裁剪窗口）
  export_snapshot()   收盘 Parquet 快照（复盘与回测一律读快照）

★ 断点续传设计（附录 C.1）：
    每批成功才推进 watermark，中断后从 watermark 继续；
    upsert 幂等，因此重跑同一批不会产生重复行。

用法：
    python scripts/sync.py stock_list
    python scripts/sync.py daily --codes data/codes_sample.txt --count 250
    python scripts/sync.py minutes --codes data/codes_sample.txt --period 30m
    python scripts/sync.py snapshot
    python scripts/sync.py all --codes data/codes_sample.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data import duck
from backend.data.adapter import bootstrap, get_adapter
from backend.data.base import SourceError
from backend.data.sqlite import get_conn as get_sqlite, upsert_stocks

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")


def now_str() -> str:
    """统一时间格式。写入 DB 的时间戳都用北京时间 ISO 串。"""
    return datetime.now(CST).isoformat(timespec="seconds")


# ================================================================== 股票列表
def sync_stock_list(max_pages: int = 70) -> int:
    """全市场股票列表。实测约 5400 只，54 页，约 40~60 秒。"""
    bootstrap()
    sn = get_adapter("sina")
    logger.info("开始同步全市场股票列表 ...")
    rows = sn.fetch_stock_list(datetime.now(CST), max_pages=max_pages)
    if not rows:
        logger.error("股票列表为空，未写入")
        return 0

    # 只保留 A 股主板/创业板/科创板/北交所，剔除指数与基金
    valid = [r for r in rows if r.get("id") and len(str(r["id"])) == 6
             and str(r["id"]).isdigit() and r.get("market")]
    logger.info(f"原始 {len(rows)} 条，合法 A 股 {len(valid)} 条")

    n = upsert_stocks(valid)
    duck.set_watermark("stock_list", now_str(), note=f"count={n}")
    counts = get_sqlite().execute(
        "SELECT market, count(*) FROM stock GROUP BY market ORDER BY count(*) DESC"
    ).fetchall()
    logger.info("市场分布: " + " ".join(f"{m}={c}" for m, c in counts))
    st = get_sqlite().execute("SELECT count(*) FROM stock WHERE is_st=1").fetchone()[0]
    logger.info(f"写入 stock 表 {n} 条，其中 ST/退市风险股 {st} 只")
    logger.warning("★遗留：industry 与 list_date 接口不返回，当前为 NULL，"
                   "推荐模块的『上市>60日』过滤需等补齐后才能启用")
    return n


# ================================================================== 日线
def fetch_daily_robust(code: str, count: int = 250) -> tuple[list[dict], str]:
    """日线获取（多源自动降级）。

    实测发现的源差异（2026-10-08）：
      腾讯 qfq  —— 沪深科创完整，但**北交所 920xxx 只返回当天 1 根**（无历史）
      新浪 不复权 —— 北交所可返回完整 250 根，沪深科创同样完整

    因此策略是：腾讯为主源；若返回根数明显不足（< MIN_USABLE 根，
    视为该源无历史覆盖），自动切新浪补齐。复权口径差异由 adj_factor
    字段后续统一处理，此处如实记录数据来自哪个源，不做静默替换。

    ★★★ 主源**抛异常**时也必须降级（实测 2026-10-10 踩过）
      腾讯 `web.ifzq.gtimg.cn` 整源返回 **HTTP 501**（未授权），
      原实现只处理「返回根数不足」，异常直接往上抛⇒
      5051 只全部失败，而**新浪明明能取到 12 根**（我手工验证过）。
      这是最贵的形态：**主源整体挂掉时，降级代码一行都没执行**，
      而错误信息（`tencent HTTP 501`）看着像「这只票没数据」。
      ⇒ 改成每个源独立 try，异常记下来继续试下一个源。
    """
    bootstrap()
    MIN_USABLE = 60
    errors: list[str] = []

    def _try(src_name: str) -> list[dict]:
        try:
            return get_adapter(src_name).fetch_daily(code, count=count)
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"{src_name}:{type(exc).__name__}:{str(exc)[:60]}")
            return []

    bars = _try("tencent")
    if len(bars) >= MIN_USABLE:
        for b in bars:
            b.setdefault("_source", "tencent_qfq")
        return bars, "tencent"

    bars2 = _try("sina")
    if len(bars2) > len(bars):
        for b in bars2:
            b.setdefault("_source", "sina_none")
        return bars2, "sina"

    if not bars and not bars2:
        # ★ 复用标准失败语义，不要自己拼 SourceError：
        #   `SourceError(code, msg)` 的**第一个参数是 code 不是 msg**
        #   （实测踩过：`SourceError(f"...")` 直接报
        #     "missing 1 required positional argument: 'msg'"），
        #   而 code 段由 `ERR_SOURCE_ALL_DOWN` 统一约定。
        #   报错必须含**两源**原因，否则会把「主源挂了」误导成「这只票没数据」。
        from backend.data.base import ERR_SOURCE_ALL_DOWN

        raise SourceError(
            ERR_SOURCE_ALL_DOWN,
            f"{code} 全源失败：{' | '.join(errors) or '两源均返回空'}")
    for b in bars:
        b.setdefault("_source", "tencent_qfq")
    return bars, "tencent"


def sync_daily(codes: list[str], count: int = 250, batch: int = 40) -> dict:
    """日线历史同步。分批 + 断点续传 + 限流抖动 + 多源降级。

    ★★ `count` 必须 ≥ MIN_USABLE（实测 2026-10-10踩过）：
       我为了「快点补一天」传了 `--count 12`，而降级判据是
       `len(bars) >= MIN_USABLE(=60)` ⇒ **即使腾讯完全成功**，
       12 根也会被判「根数不足」⇒ 全部降级到新浪（不复权），
       前复权口径被静默替换掉，**且日志里只写「降级到备源」不报错**。
       ⇒ 这里做下限保护并显式告警。

    对比附录 A.4：每批 200 只、批间 sleep 0.5s。实测腾讯 qps=10 且响应约 200ms，
    这里取 batch=40 更保守（约 8 QPS 有效吞吐），批间随机 sleep。
    """
    MIN_USABLE = 60
    if count < MIN_USABLE:
        logger.warning(
            f"★ --count={count} 低于降级判据 MIN_USABLE={MIN_USABLE} ⇒ "
            f"即使主源成功也会被判「根数不足」而**静默降级到不复权源**，"
            f"前复权口径丢失。已提升到 {MIN_USABLE}。")
        count = MIN_USABLE

    total = len(codes)
    done, failed, degraded = 0, [], []
    t0 = time.monotonic()

    for i in range(0, total, batch):
        chunk = codes[i : i + batch]
        for code in chunk:
            try:
                bars, src = fetch_daily_robust(code, count=count)
                if not bars:
                    raise ValueError("全部数据源均无该股历史")
                duck.upsert_kline_daily(bars)
                done += 1
                if src != "tencent":
                    degraded.append((code, src, len(bars)))
            except Exception as exc:            # 单只失败不中断整批
                failed.append((code, str(exc)[:80]))
        pct = min(100, int((i + len(chunk)) / total * 100))
        el = time.monotonic() - t0
        eta = el / max(1, done) * (total - done)
        logger.info(f"[{pct:>3}%] {done}/{total} 只成功 | 已耗时 {el:.0f}s | 预计剩余 {eta:.0f}s")
        # 每批结束推进水位：即使中断也能从这里续跑（附录 C.1 第 5 步）
        duck.set_watermark("kline_daily", f"{min(chunk)}..{max(chunk)}",
                           note=f"done={done}/{total} failed={len(failed)}")
        time.sleep(0.5 * (1 + (hash(str(i)) % 100) / 100 * 0.3))

    rows = duck.table_counts()["kline_daily"]
    logger.info(f"日线同步结束：成功 {done} 只，失败 {len(failed)} 只，库内 {rows:,} 行")
    if degraded:
        logger.info(f"降级到备源补齐 {len(degraded)} 只: "
                    + ", ".join(f"{c}->{s}({n}根)" for c, s, n in degraded[:5]))
    if failed:
        logger.warning("失败样例: " + "; ".join(f"{c}({e})" for c, e in failed[:5]))
    return {"done": done, "failed": failed, "degraded": degraded, "db_rows": rows}


# ================================================================== 分钟线
def sync_minutes(codes: list[str], period: str = "30m") -> dict:
    """跟踪池分钟线。

    注意：腾讯 mkline 端点单次返回约 480 根（超出请求 count），
    因此必须按 ts 裁剪窗口，否则会把早已过期的历史 bar重复写入。
    """
    bootstrap()
    tx = get_adapter("tencent")
    mp = period.replace("m", "") + "m" if not period.endswith("m") else period

    # ★★ 修复两个 bug（均导致本函数回归验证时静默无效）：
    #   b1 参数字符串错误。腾讯 mkline 端点要求 `m30` 这种带 m 前缀的周期，
    #      原来传的是 `mp.replace("m","")` 即 "30" —— 端点返回
    #      {"code":-1,"msg":"param error","data":[]}，adapter 里
    #      `node.get("30")` 恒为 None，于是每只都计入 failed 且**不报错**。
    #      scripts/sync_minutes.py 的同名函数不走这段代码，所以只有这里坏。
    #   b2 截留根数写反。`60 if mp.startswith("30") else 60*48` 让 m30 只留
    #      60 根（≈7.5 个交易日），而 m60 反而留 2880 根（≈360 天）。
    #      现按 sync_minutes.py 已验证的 COVERAGE_DAYS × 每日根数计算。
    bars_per_day = {"m1": 240, "m5": 48, "m15": 16, "m30": 8, "m60": 4}
    days_by_period = {"m1": 2, "m5": 11, "m15": 31, "m30": 61, "m60": 121}
    if mp not in bars_per_day:
        raise ValueError(f"不支持的分钟周期 {period!r}，"
                         f"可选：{sorted(bars_per_day)}")
    keep = days_by_period[mp] * bars_per_day[mp]

    done, failed = 0, []
    for code in codes:
        try:
            bars = tx.fetch_minute(code, mperiod=mp, count=320)
            if not bars:
                raise ValueError("返回空分钟线")
            bars = bars[-keep:]
            duck.upsert_kline_min(bars)
            done += 1
        except Exception as exc:
            failed.append((code, str(exc)[:80]))
    rows = duck.table_counts()["kline_min"]
    logger.info(f"分钟线({mp}) 同步：成功 {done} 只，失败 {len(failed)} 只，库内 {rows:,} 行")
    if failed:
        logger.warning("失败样例: " + "; ".join(f"{c}({e})" for c, e in failed[:5]))
    return {"done": done, "failed": failed, "db_rows": rows}


# ================================================================== 快照
def export_snapshot(date_str: str | None = None) -> Path:
    """导出 Parquet 快照。

    ★ 回测与复盘**一律读快照，不读实时表** —— 这是可复现性的前提（附录 B.1）。

    两个实测踩过的坑：
      1. 必须复用 duck.get_conn()，不能新开连接 —— 新连接不带已建表，
         会报 "Table with name quote_snapshot does not exist"
      2. 表不存在或无数据时应跳过并说明，不能让整个导出失败
    """
    d = date_str or duck.max_trade_date()
    if not d:
        logger.error("库内无日线数据，无法导出快照")
        raise SystemExit(1)
    out_dir = ROOT / "data" / "snapshots" / str(d)
    out_dir.mkdir(parents=True, exist_ok=True)

    con = duck.get_conn()          # 复用已有连接（含建好的表）
    exports = {
        "kline_daily.parquet": f"SELECT * FROM kline_daily WHERE trade_date <= DATE '{d}'",
        "board_strength.parquet": f"SELECT * FROM board_strength WHERE CAST(ts AS DATE) <= DATE '{d}'",
        "quote_snapshot.parquet": f"SELECT * FROM quote_snapshot WHERE CAST(ts AS DATE) <= DATE '{d}'",
        "kline_min.parquet": f"SELECT * FROM kline_min WHERE CAST(ts AS DATE) <= DATE '{d}'",
    }
    made, skipped = [], []
    for fname, sql in exports.items():
        path = out_dir / fname
        try:
            rows = con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]
            if not rows:
                skipped.append(f"{fname}(空表)")
                continue
            con.execute(f"COPY ({sql}) TO '{path.as_posix()}' (FORMAT PARQUET)")
            size_mb = path.stat().st_size / 1024 / 1024
            made.append(f"{fname}({size_mb:.1f}MB/{rows:,}行)")
            logger.info(f"  导出 {fname}  {size_mb:.2f}MB  {rows:,} 行")
        except Exception as exc:
            skipped.append(f"{fname}({type(exc).__name__})")
            logger.warning(f"  跳过 {fname}: {exc}")
    logger.info(f"快照目录: {out_dir}")
    logger.info(f"  已导出: {', '.join(made) if made else '无'}")
    if skipped:
        logger.info(f"  已跳过: {', '.join(skipped)}（对应模块尚未实现）")
    return out_dir


# ================================================================== 校验
def verify() -> dict:
    """M1 验收自测：数据完整性检查。"""
    logger.info("=" * 60)
    logger.info("数据完整性校验")
    logger.info("=" * 60)
    con = duck.get_conn()
    rep = {}

    n_stock = get_sqlite().execute("SELECT count(*) FROM stock").fetchone()[0]
    rep["全市场股票数"] = f"{n_stock:,}只"
    rep["覆盖市场"] = ", ".join(
        f"{m}:{c}" for m, c in get_sqlite().execute(
            "SELECT market, count(*) FROM stock GROUP BY market ORDER BY 2 DESC"
        ).fetchall()
    )
    rep["ST股数"] = f"{get_sqlite().execute('SELECT count(*) FROM stock WHERE is_st=1').fetchone()[0]}只"

    n_k = duck.table_counts()["kline_daily"]
    n_codes = con.execute("SELECT count(DISTINCT code) FROM kline_daily").fetchone()[0]
    rep["日线总行数"] = f"{n_k:,}行"
    rep["日线覆盖股票数"] = f"{n_codes:,}只"
    rep["日线日期区间"] = " ~ ".join(
        str(x) for x in con.execute(
            "SELECT min(trade_date), max(trade_date) FROM kline_daily").fetchone()
    ) if n_k else "无数据"

    # OHLC 自洽性 —— 字段错位会在此暴露
    bad = con.execute(
        "SELECT count(*) FROM kline_daily WHERE low > high OR high < open OR high < close"
    ).fetchone()[0]
    rep["OHLC不自洽行数"] = f"{bad}行{'（正常，应为0）' if bad == 0 else ' ★异常'}"

    rep["分钟线总行数"] = f"{duck.table_counts()['kline_min']:,}行"
    rep["快照数"] = f"{len(list((ROOT / 'data' / 'snapshots').glob('*')))}天"

    for k, v in rep.items():
        logger.info(f"  {k:18s}: {v}")
    return rep


# ================================================================== CLI
def main() -> int:
    p = argparse.ArgumentParser(description="Hawk 全量数据同步")
    p.add_argument("task",
                   choices=["stock_list", "daily", "minutes", "snapshot", "all", "verify"])
    p.add_argument("--codes", default="", help="代码文件，每行一个 6 位代码")
    p.add_argument("--count", type=int, default=250, help="日线条数")
    p.add_argument("--period", default="30m", help="分钟周期 5m/30m/60m")
    p.add_argument("--batch", type=int, default=40, help="日线每批只数")
    p.add_argument("--date", default="", help="快照日期 YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 只（试跑用）")
    a = p.parse_args()

    codes: list[str] = []
    if a.codes:
        cp = Path(a.codes)
        if not cp.is_absolute():
            cp = ROOT / a.codes
        for line in cp.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                codes.append(line)
        if a.limit:
            codes = codes[: a.limit]
        logger.info(f"待处理 {len(codes)} 只代码")

    if a.task in ("stock_list", "all"):
        sync_stock_list()
    if a.task in ("daily", "all"):
        if not codes:
            logger.error("daily 需要 --codes 指定代码文件")
            return 2
        sync_daily(codes, count=a.count, batch=a.batch)
    if a.task in ("minutes", "all"):
        if not codes:
            logger.error("minutes 需要 --codes 指定代码文件")
            return 2
        sync_minutes(codes, period=a.period)
    if a.task in ("snapshot", "all"):
        export_snapshot(a.date or None)
    if a.task == "verify":
        verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())