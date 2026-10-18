# -*- coding: utf-8 -*-
"""
Hawk · 行情/历史存储（DuckDB）

职责边界：
  - 只负责建表、写入、查询。不含任何业务判断。
  - 行情类数据放DuckDB（全市场日线 135 万行/年，需列式扫描性能）；
    业务类放 SQLite（见 sqlite.py）。

对应附录 B.1 DDL。此处做两处工程增强，原因写在注释里：
  1. kline_daily 增加 idx_market_date 索引 —— 板块强度全量重算要按日期扫全表
  2. 所有表加INSERT ... ON CONFLICT 保证增量同步幂等（断点续传的前提）
"""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = _ROOT / "data" / "hawk.duckdb"

_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None
_ro_conn: duckdb.DuckDBPyConnection | None = None
_ro_local = threading.local()
_ro_cursors: list[tuple[duckdb.DuckDBPyConnection, duckdb.DuckDBPyConnection]] = []


# ★ DuckDB 是单写者模型：同一时刻只能有一个进程持有写连接。
#  本项目 7×24 常驻且需要「同步进程写 + UI/API 进程读」并行，
#  因此提供两种连接：
#    get_conn()     —— 写连接，仅供同步/调度等写入方使用（需与写入方同进程）
#    get_ro_conn()  —— 只读连接，供 UI / API 进程并发查询
#  跨进程时读进程用 read_only=True 打开同一文件。
#  注意：read_only 连接要求文件已存在，否则报错。
#
# ★★ 实测纠正（2026-10-10，原docstring 的说法是错的）：
#   **同进程内 `read_only=True` 与读写连接不能共存**
#   （DuckDB 1.x 实测：`Can't open a connection to same database file
#     with a different configuration than existing connections`）。
#   ⇒任何「先 get_ro_conn() 查询、再 get_conn() 写入」的单进程脚本都会失败，
#     而且症状极具误导性：**抛错点在写入侧，但根因是早先那次读**。
#     （实测踩过：`scripts/fill_m30_gap.py` 996 只全部失败，
#       而同一只代码单独跑却成功 —— 差的就是前面那次读。）
#   ⇒ 修法二选一：① 先取好数据再 `release_ro_conn()` 然后写；
#     ② 或一开始就用 `get_conn()`（读写一体，单连接也够用）。
#     **推荐 ②**：写脚本本就要写，用读连接查只是顺手，不值得为它绕。
def get_ro_conn() -> duckdb.DuckDBPyConnection:
    """线程内复用只读游标。UI / FastAPI 的并发查询各自持有结果集。

    ⚠️ 同一进程内**不可**同时持有它与写连接（见上方实测纠正）。
       若本进程稍后需要写入，请改用 `get_conn()`，
       或在读完后调用 `release_ro_conn()`。
    """
    global _ro_conn
    with _lock:
        if _conn is not None:
            return _conn  # 同进程已有写连接，直接复用
        if _ro_conn is None:
            if not DB_PATH.exists():
                raise FileNotFoundError(
                    f"数据文件不存在: {DB_PATH}；请先运行 scripts/sync.py stock_list 初始化"
                )
            _ro_conn = duckdb.connect(str(DB_PATH), read_only=True)
            _ro_conn.execute("SET TimeZone='Asia/Shanghai'")
        # FastAPI executes sync endpoints on multiple worker threads.
        # Sharing execute()/fetchone() across them consumes another request's result.
        # cursor() is DuckDB's independent thread-local connection to the same DB.
        if getattr(_ro_local, "source", None) is not _ro_conn:
            _ro_local.source = _ro_conn
            _ro_local.cursor = _ro_conn.cursor()
            _ro_cursors.append((_ro_conn, _ro_local.cursor))
        return _ro_local.cursor


# ------------------------------------------------------------------ DDL
DDL = """
-- 全市场日线（前复权 + 原始价 + 复权因子）
CREATE TABLE IF NOT EXISTS kline_daily (
  code        VARCHAR NOT NULL,
  trade_date  DATE    NOT NULL,
  open        DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  pre_close   DOUBLE,
  volume      BIGINT,          -- 股
  amount      DOUBLE,          -- 元
  turnover    DOUBLE,          -- 换手率 %
  pct_chg     DOUBLE,          -- 涨跌幅 %
  adj_factor  DOUBLE,          -- 复权因子
  is_suspend  BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (code, trade_date)
);
-- 板块强度全量重算需按日期横切全市场，加此索引避免全表扫
CREATE INDEX IF NOT EXISTS idx_kline_date ON kline_daily(trade_date);

--★★ 指数日线（真实指数，不是等权均值代理）
--   背景：`backfill_regime.py` 原用 `breadth.avg_chg`（全市场**个股等权均值**）
--   当作 `index_pct` 代理，并标注 `index_pct_source="equal_weight_avg"`。
--   实测 2026-09 与 10月共 30 个交易日，两者**符号经常相反**
--   （09-09 等权 −0.47% vs 上证 +0.28%；09-16 等权 +1.42% vs 上证 +0.71%），
--   平均绝对差 0.5pp、最大 2.0pp。
--   根因：等权 vs 市值加权 —— 小盘股主导时等权均值远大于指数。
--   ⇒ 市场状态的「大盘涨跌」必须用真实指数，否则 regime 会判反。
--   ⚠ 只存指数本身，**不含成分**（`index_member_snapshot` 是另一回事）。
CREATE TABLE IF NOT EXISTS index_daily (
  index_code  VARCHAR NOT NULL,-- sh000001 / sz399001 ...
  trade_date  DATE    NOT NULL,
  open        DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  volume      BIGINT,-- 成交量（股/手，随源）
  amount      DOUBLE,          -- 成交额（元）；源不给时为 NULL，**不自算**
  PRIMARY KEY (index_code, trade_date)
);

-- 跟踪池分钟线（只保留最近 N 个交易日）
CREATE TABLE IF NOT EXISTS kline_min (
  code VARCHAR NOT NULL,
  ts   TIMESTAMP NOT NULL,
  period VARCHAR NOT NULL,      -- '1m' | '5m' | '30m'
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
  volume BIGINT, amount DOUBLE,
  PRIMARY KEY (code, period, ts)
);

-- 板块强度历史
CREATE TABLE IF NOT EXISTS board_strength (
  ts          TIMESTAMP NOT NULL,
  board_code  VARCHAR   NOT NULL,
  board_name  VARCHAR,
  type        VARCHAR,          -- 'industry' | 'concept' | 'custom'
  strength    DOUBLE,
  rank        INTEGER,
  rank_change INTEGER,
  limit_up_cnt   INTEGER,
  limit_down_cnt INTEGER,
  max_streak     INTEGER,
  net_inflow     DOUBLE,
  vol_ratio      DOUBLE,
  up_ratio       DOUBLE,
  leader_code    VARCHAR,
  PRIMARY KEY (ts, board_code)
);

-- 板块成分快照（★ 防前视偏差：每日快照，回测用当日快照）
CREATE TABLE IF NOT EXISTS board_member_snapshot (
  snap_date  DATE NOT NULL,
  board_code VARCHAR NOT NULL,
  code       VARCHAR NOT NULL,
  weight     DOUBLE,
  PRIMARY KEY (snap_date, board_code, code)
);

-- 大盘环境历史
CREATE TABLE IF NOT EXISTS market_regime (
  ts TIMESTAMP PRIMARY KEY,
  score DOUBLE, state VARCHAR, position_cap DOUBLE,
  limit_up_cnt INTEGER, limit_down_cnt INTEGER,
  broken_rate DOUBLE, max_streak INTEGER,
  total_amount DOUBLE, up_ratio DOUBLE,
  detail_json VARCHAR
);

-- 指数成分快照（同样防前视偏差）
CREATE TABLE IF NOT EXISTS index_member_snapshot (
  snap_date DATE NOT NULL, index_code VARCHAR NOT NULL, code VARCHAR NOT NULL,
  PRIMARY KEY (snap_date, index_code, code)
);

-- 同步水位（断点续传）
CREATE TABLE IF NOT EXISTS sync_watermark (
  task      VARCHAR PRIMARY KEY,   -- 'kline_daily' | 'kline_min' | 'stock_list' ...
  last_ok   VARCHAR,               -- 已完成到的日期/页码
  updated_at TIMESTAMP,
  note      VARCHAR
);

-- 原始行情快照（供复盘重放，1 分钟级）
CREATE TABLE IF NOT EXISTS quote_snapshot (
  ts TIMESTAMP NOT NULL,
  code VARCHAR NOT NULL,
  price DOUBLE, pre_close DOUBLE, open DOUBLE, high DOUBLE, low DOUBLE,
  volume BIGINT, amount DOUBLE, pct_chg DOUBLE,
  turnover DOUBLE, float_mv DOUBLE, total_mv DOUBLE,
  PRIMARY KEY (ts, code)
);
CREATE INDEX IF NOT EXISTS idx_quote_code_ts ON quote_snapshot(code, ts DESC);
"""


def get_conn() -> duckdb.DuckDBPyConnection:
    """全局单连接。DuckDB 不支持多并发写，用锁串行化。

    ★ **写脚本请直接用这个**，不要先 `get_ro_conn()` 再切到这里
      （同进程内两者不能共存，见文件头实测纠正）。
      DuckDB 单连接本身支持读写，用它查即可，无需另开读连接。
    """
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = duckdb.connect(str(DB_PATH))
            _conn.execute("SET TimeZone='Asia/Shanghai'")
        return _conn


def release_ro_conn() -> None:
    """显式关闭只读连接，让本进程之后能开写连接。

    ★ 为什么需要它：`get_ro_conn()` 返回的是**模块级全局单连接**
      （`_ro_conn`），`del con` 只删本地名字、模块仍持有 ⇒
      写连接永远开不出来。必须显式关。

    ⚠️ 铁律：**只读连接是共享单连接，绝不可在普通代码里close()**
       （会打断其他持有者）。本函数只供「读完就要写」的单进程脚本使用，
       且调用方须确保没有别的线程正在用这张连接。
    """
    global _ro_conn
    with _lock:
        if _ro_conn is not None:
            for source, cursor in _ro_cursors[:]:
                if source is _ro_conn:
                    cursor.close()
                    _ro_cursors.remove((source, cursor))
            _ro_conn.close()
            _ro_conn = None


def init_db() -> None:
    c = get_conn()
    c.execute(DDL)
    # Preserve existing snapshots; legacy rows remain distinguishable from new scores.
    for name, kind in {
        "pct_chg": "DOUBLE",
        "amount": "DOUBLE",
        "factor_coverage": "DOUBLE",
        "score_method": "VARCHAR",
        "factors_json": "VARCHAR",
        "raw_json": "VARCHAR",
        "source": "VARCHAR",
        "source_complete": "BOOLEAN",
    }.items():
        c.execute(f"ALTER TABLE board_strength ADD COLUMN IF NOT EXISTS {name} {kind}")
    for name, kind in {
        "source": "VARCHAR",
        "source_ts": "TIMESTAMP",
        "captured_at": "TIMESTAMP",
    }.items():
        c.execute(f"ALTER TABLE quote_snapshot ADD COLUMN IF NOT EXISTS {name} {kind}")


def upsert_kline_daily(rows: list[dict]) -> int:
    """日线增量 upsert。rows 需含 code/trade_date。

    断点续传依赖此幂等性：重跑同一批数据不会产生重复行。
    """
    if not rows:
        return 0
    c = get_conn()
    c.executemany(
        """
        INSERT INTO kline_daily
          (code, trade_date, open, high, low, close, pre_close,
           volume, amount, turnover, pct_chg, adj_factor, is_suspend)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (code, trade_date) DO UPDATE SET
          open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
          close=EXCLUDED.close, pre_close=EXCLUDED.pre_close,
          volume=EXCLUDED.volume, amount=EXCLUDED.amount,
          turnover=EXCLUDED.turnover, pct_chg=EXCLUDED.pct_chg,
          adj_factor=EXCLUDED.adj_factor, is_suspend=EXCLUDED.is_suspend
        """,
        [
            (
                r["code"],
                r["trade_date"],
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                r.get("pre_close"),
                r.get("volume"),
                r.get("amount"),
                r.get("turnover"),
                r.get("pct_chg"),
                r.get("adj_factor"),
                bool(r.get("is_suspend", False)),
            )
            for r in rows
        ],
    )
    return len(rows)


def upsert_kline_min(rows: list[dict]) -> int:
    if not rows:
        return 0
    c = get_conn()
    c.executemany(
        """
        INSERT INTO kline_min (code, ts, period, open, high, low, close, volume, amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (code, period, ts) DO UPDATE SET
          open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
          close=EXCLUDED.close, volume=EXCLUDED.volume, amount=EXCLUDED.amount
        """,
        [
            (
                r["code"],
                r["ts"],
                r.get("period", "1m"),
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                r.get("volume"),
                r.get("amount"),
            )
            for r in rows
        ],
    )
    return len(rows)


def upsert_quote_snapshot(rows: list[dict]) -> int:
    if not rows:
        return 0
    c = get_conn()
    c.executemany(
        """
        INSERT INTO quote_snapshot
          (ts, code, price, pre_close, open, high, low, volume, amount, pct_chg,
           turnover, float_mv, total_mv, source, source_ts, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ts, code) DO UPDATE SET
          price=EXCLUDED.price, pre_close=EXCLUDED.pre_close, open=EXCLUDED.open,
          high=EXCLUDED.high, low=EXCLUDED.low, volume=EXCLUDED.volume,
          amount=EXCLUDED.amount, pct_chg=EXCLUDED.pct_chg,
          turnover=EXCLUDED.turnover, float_mv=EXCLUDED.float_mv,
          total_mv=EXCLUDED.total_mv, source=EXCLUDED.source,
          source_ts=EXCLUDED.source_ts, captured_at=EXCLUDED.captured_at
        """,
        [
            (
                r["ts"],
                r["code"],
                r.get("price"),
                r.get("pre_close"),
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("volume"),
                r.get("amount"),
                r.get("pct_chg"),
                r.get("turnover"),
                r.get("float_mv"),
                r.get("total_mv"),
                r.get("source"),
                r.get("source_ts"),
                r.get("captured_at"),
            )
            for r in rows
        ],
    )
    return len(rows)


# ------------------------------------------------------------------ 水位
def get_watermark(task: str) -> str | None:
    r = (
        get_conn()
        .execute("SELECT last_ok FROM sync_watermark WHERE task = ?", [task])
        .fetchone()
    )
    return r[0] if r else None


def set_watermark(task: str, last_ok: str, note: str = "") -> None:
    """只在整批成功后才更新水位（附录 C.1 第4 步）。"""
    get_conn().execute(
        """
        INSERT INTO sync_watermark (task, last_ok, updated_at, note)
        VALUES (?, ?, now(), ?)
        ON CONFLICT (task) DO UPDATE SET
          last_ok=EXCLUDED.last_ok, updated_at=EXCLUDED.updated_at, note=EXCLUDED.note
        """,
        [task, last_ok, note],
    )


def max_trade_date() -> str | None:
    r = get_conn().execute("SELECT max(trade_date) FROM kline_daily").fetchone()
    return str(r[0]) if r and r[0] else None


# ------------------------------------------------------------------ 查询
# 查询统一走只读连接：写入方（同步进程）与查询方（UI/API）可并行，
# 互不争锁。这是 7×24 常驻架构的必要条件。
def get_daily(code: str, limit: int = 250) -> list[dict]:
    """按时间升序返回日线。缠论引擎依赖升序输入。"""
    rows = (
        get_ro_conn()
        .execute(
            """
        SELECT trade_date, open, high, low, close, volume, amount, pct_chg, is_suspend
        FROM kline_daily WHERE code = ?
        ORDER BY trade_date DESC LIMIT ?
        """,
            [code, limit],
        )
        .fetchall()
    )
    cols = [
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pct_chg",
        "is_suspend",
    ]
    return [dict(zip(cols, r)) for r in reversed(rows)]


def table_counts() -> dict[str, int]:
    c = get_ro_conn()
    out = {}
    for t in (
        "kline_daily",
        "kline_min",
        "board_strength",
        "quote_snapshot",
        "board_member_snapshot",
        "market_regime",
        "sync_watermark",
    ):
        try:
            out[t] = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except duckdb.Error:
            out[t] = -1
    return out


if __name__ == "__main__":
    init_db()
    print("DuckDB 初始化完成:", DB_PATH)
    for k, v in table_counts().items():
        print(f"  {k:26s} {v:>10,} 行")
