# -*- coding: utf-8 -*-
"""
Hawk · 业务/信号/语料存储（SQLite）

对应附录 B.2 全量 DDL。两点工程说明：
  1. news_fts 用 FTS5 外部内容表模式（content='news'），
     写入 news 时必须同步写 news_fts，否则检索不到。
  2. signal 只追加不修改（防事后诸葛亮，附录7.2）——
     因此不提供 UPDATE 方法，eval_json 的填充由 15:05 判定任务
     以"仅在 eval_json IS NULL 时写入"的方式追加。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import sqlite3

_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = _ROOT / "data" / "hawk.sqlite"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stock (
  id TEXT PRIMARY KEY, name TEXT, market TEXT, industry TEXT,
  concept TEXT,               -- JSON 数组
  list_date TEXT, is_st INTEGER DEFAULT 0,
  total_mv REAL, float_mv REAL,
  is_suspend INTEGER DEFAULT 0,    -- 停牌标记：缠论引擎据此跳过该 bar（附录 C.3）
  status TEXT DEFAULT 'active'     -- active | suspended | delisted
);

CREATE TABLE IF NOT EXISTS watchlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL, group_name TEXT DEFAULT '核心池',
  add_price REAL, add_date TEXT, reason TEXT,
  tags TEXT,                  -- JSON: ["缠论买点","消息驱动"]
  priority INTEGER DEFAULT 0,
  last_mark TEXT, last_mark_at TEXT
);

CREATE TABLE IF NOT EXISTS position (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL, name TEXT,
  open_price REAL, open_date TEXT, shares INTEGER, cost REAL,
  stop_loss REAL, take_profit REAL,
  strategy_id TEXT DEFAULT 'manual',
  status TEXT DEFAULT 'holding',
  note TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS position_trade (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT, side TEXT, price REAL, shares INTEGER,
  trade_time TEXT, fee REAL, reason TEXT, signal_id TEXT
);

CREATE TABLE IF NOT EXISTS raw_news (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL, ts TEXT, payload TEXT, fetched_at TEXT,
  parsed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_raw_news_parsed ON raw_news(parsed, id);

CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, source TEXT, sources TEXT,
  title TEXT, content TEXT, url TEXT,
  simhash TEXT UNIQUE,
  category TEXT, related_codes TEXT,
  direction TEXT, strength INTEGER, horizon TEXT,
  llm_reason TEXT, confidence REAL, verified INTEGER DEFAULT 0,
  llm_provider TEXT, prompt_ver TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts DESC);
CREATE INDEX IF NOT EXISTS idx_news_codes ON news(related_codes);

-- 全文检索：按关键词搜历史消息
CREATE VIRTUAL TABLE IF NOT EXISTS news_fts USING fts5(
  title, content, content='news', content_rowid='id', tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS signal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
  kind TEXT, level TEXT, price REAL,
  strategy_id TEXT, confidence REAL,
  reason TEXT, chan_snapshot TEXT, market_state TEXT,
  status TEXT DEFAULT 'active',
  eval_json TEXT, human_label TEXT,
  rule_version TEXT, param_hash TEXT,
  backtest_winrate REAL, created_at TEXT
);
-- ★ 幂等约束：同一「代码+类型+日期+策略」只应有一条信号。
--   每日全量重扫是正常运维动作，没有唯一约束会不断累积重复行，
--   而重复信号会让推荐引擎把同一个买点重复计分。
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_uniq
  ON signal(code, kind, ts, strategy_id);
CREATE INDEX IF NOT EXISTS idx_signal_code_ts ON signal(code, ts DESC);

CREATE TABLE IF NOT EXISTS stock_signal_stats (
  code TEXT, strategy_id TEXT, window TEXT,
  total INTEGER, hit INTEGER, winrate REAL, avg_ret REAL, updated_at TEXT,
  PRIMARY KEY (code, strategy_id, window)
);

CREATE TABLE IF NOT EXISTS mail_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, kind TEXT, subject TEXT, body_hash TEXT,
  status TEXT, err TEXT, retry INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts);

CREATE TABLE IF NOT EXISTS corpus (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL, source TEXT, pub_time TEXT,
  content TEXT, transcript_ref TEXT, collected_at TEXT,
  processed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rule_library (
  rule_id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL, name TEXT,
  if_json TEXT, then_json TEXT, timeframe TEXT,
  evidence TEXT, confidence REAL, backtest TEXT,
  status TEXT DEFAULT 'pending',     -- active | pending | rejected
  version TEXT
);

-- 交易日历（影响所有定时任务，必须准确）
CREATE TABLE IF NOT EXISTS trade_calendar (
  trade_date TEXT PRIMARY KEY,
  is_open    INTEGER DEFAULT 1,
  prev_date  TEXT,
  note TEXT
);

-- LLM 调用审计（规格 2.4 硬性要求 4「每次调用记录 provider/model/prompt 版本/
-- 耗时/token/成本估算」+ M3「调用审计日志 + 成本统计」）。
--
-- ★★ 规格附录 B 的 DDL 里**没有这张表**（规格自身缺口），此处按正文要求自建。
--
-- ★★ 为什么 ts 与 as_of 是**两列**，不能只留一个
-- ------------------------------------------------------
-- 这两件事语义不同，混在一列里必有一处是错的：
--   ts     = **墙钟**：这次调用真实发生在什么时刻（运维/成本视角）
--   as_of  = **业务时点**：这次调用是为哪个历史时点的数据服务的（铁律二）
-- 若只留 as_of：回放历史数据时全部审计行塌缩到同一天
--                ⇒「今日调用量/成本」永远算不准。
-- 若只留 ts：审计表成为唯一读墙钟的表，容易被后续代码误当业务时间源。
-- ⇒ 两列都留，并在 llm.py 的写入处注释说明「这里为什么可以用墙钟」。
CREATE TABLE IF NOT EXISTS llm_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,              -- 墙钟：调用发生时刻（仅本表允许）
  as_of TEXT,                    -- 业务时点（铁律二注入，便于回放对账）
  task TEXT,                     -- news_classify / news_entity / ...（附录 E 路由名）
  provider TEXT,                 -- ollama / cloud
  model TEXT,
  prompt_ver TEXT,
  ok INTEGER DEFAULT 0,          -- 1 成功 / 0 失败或重试
  attempt INTEGER DEFAULT 1,     -- 第几次尝试（重试会多行，真实成本在这里）
  stage TEXT,                    -- chat / parse / schema / fallback
  elapsed_ms REAL,               -- 墙钟耗时（毫秒）
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost_cny REAL DEFAULT 0,       -- 估算成本，本地恒为 0
  err TEXT,
  extra TEXT                     -- JSON：降级原因、fallback 等
);
CREATE INDEX IF NOT EXISTS idx_llm_audit_ts ON llm_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_audit_task ON llm_audit(task, ts DESC);
"""


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(DDL)
            _conn.commit()
        return _conn


# ------------------------------------------------------------------ stock
def upsert_stocks(rows: list[dict]) -> int:
    """全市场股票列表 upsert。

    list_date/industry 接口不返回时保持 NULL（不填默认值），
    下游过滤（如「上市 > 60 日」）必须显式处理 NULL，不得默认通过。
    is_suspend 来自行情快照，与 status 同步 —— 停牌信息不能丢，
    缠论引擎要靠它跳过停牌 bar（附录 C.3）。
    """
    if not rows:
        return 0
    c = get_conn()
    c.executemany(
        """
        INSERT INTO stock (id, name, market, industry, concept, list_date, is_st,
                           total_mv, float_mv, is_suspend, status)
        VALUES (:id, :name, :market, :industry, :concept, :list_date, :is_st,
                :total_mv, :float_mv, :is_suspend, :status)
        ON CONFLICT(id) DO UPDATE SET
          name=EXCLUDED.name, market=EXCLUDED.market, industry=EXCLUDED.industry,
          concept=EXCLUDED.concept, is_st=EXCLUDED.is_st,
          total_mv=EXCLUDED.total_mv, float_mv=EXCLUDED.float_mv,
          is_suspend=EXCLUDED.is_suspend, status=EXCLUDED.status
        """,
        [
            {
                **r,
                "industry": r.get("industry"),
                "concept": r.get("concept"),
                "list_date": r.get("list_date"),
                "is_st": r.get("is_st", 0),
                "is_suspend": r.get("is_suspend", 0),
                "status": r.get("status", "active"),
            }
            for r in rows
        ],
    )
    c.commit()
    return len(rows)


def valid_codes() -> set[str]:
    """本地股票代码全集 —— LLM 输出的代码必须在此集合内，否则拦截（附录 4.6 防幻觉）。"""
    return {r[0] for r in get_conn().execute("SELECT id FROM stock").fetchall()}


# ------------------------------------------------------------------ news
def insert_raw_news(source: str, ts: str, payload: str, fetched_at: str) -> int:
    """抓取层只落原始报文，不做任何解析（铁律三）。"""
    cur = get_conn().execute(
        "INSERT INTO raw_news (source, ts, payload, fetched_at, parsed) VALUES (?,?,?,?,0)",
        [source, ts, payload, fetched_at],
    )
    get_conn().commit()
    return int(cur.lastrowid or 0)


def fetch_unparsed_raw_news(limit: int = 200) -> list[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM raw_news WHERE parsed = 0 ORDER BY id LIMIT ?", [limit]
    ).fetchall()


def mark_raw_parsed(ids: list[int]) -> None:
    if not ids:
        return
    q = f"UPDATE raw_news SET parsed = 1 WHERE id IN ({','.join('?' * len(ids))})"
    get_conn().execute(q, ids)
    get_conn().commit()


def insert_news(row: dict) -> int | None:
    """写入结构化消息。simhash 唯一约束用于去重，冲突返回 None（不覆盖最早一条）。"""
    c = get_conn()
    try:
        cur = c.execute(
            """
            INSERT INTO news (ts, source, sources, title, content, url, simhash,
                              category, related_codes, direction, strength, horizon,
                              llm_reason, confidence, verified, llm_provider,
                              prompt_ver, created_at)
            VALUES (:ts,:source,:sources,:title,:content,:url,:simhash,:category,
                    :related_codes,:direction,:strength,:horizon,:llm_reason,
                    :confidence,:verified,:llm_provider,:prompt_ver,:created_at)
            """,
            row,
        )
        rid = int(cur.lastrowid or 0)
        # 同步写入 FTS 索引（外部内容表模式必须显式写）
        c.execute(
            "INSERT INTO news_fts (rowid, title, content) VALUES (?,?,?)",
            [rid, row.get("title", ""), row.get("content", "")],
        )
        c.commit()
        return rid
    except sqlite3.IntegrityError:
        return None      # simhash 重复 -> 视为同一事件，保留最早一条


def search_news_fts(keyword: str, limit: int = 50) -> list[dict]:
    """FTS5 全文检索。keyword 需自行处理引号转义。"""
    safe = keyword.replace('"', '""')
    rows = get_conn().execute(
        """
        SELECT n.id, n.ts, n.title, n.direction, n.strength, n.related_codes
        FROM news_fts f JOIN news n ON n.id = f.rowid
        WHERE news_fts MATCH ?
        ORDER BY n.ts DESC LIMIT ?
        """,
        [safe, limit],
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ signal
def insert_signal(row: dict) -> int:
    """信号生成即冻结，禁止事后修改（附录 4.10）。因此只有 INSERT。"""
    cur = get_conn().execute(
        """
        INSERT INTO signal (ts, code, name, kind, level, price, strategy_id,
                            confidence, reason, chan_snapshot, market_state,
                            rule_version, param_hash, created_at)
        VALUES (:ts,:code,:name,:kind,:level,:price,:strategy_id,:confidence,
                :reason,:chan_snapshot,:market_state,:rule_version,:param_hash,:created_at)
        """,
        row,
    )
    get_conn().commit()
    return int(cur.lastrowid or 0)


def fill_signal_eval(signal_id: int, eval_json: dict) -> None:
    """15:05 判定任务填充。仅在 eval_json 为 NULL 时写入，避免覆盖已判定结果。"""
    get_conn().execute(
        "UPDATE signal SET eval_json = ? WHERE id = ? AND eval_json IS NULL",
        [json.dumps(eval_json, ensure_ascii=False), signal_id],
    )
    get_conn().commit()


def label_signal(signal_id: int, human_label: str) -> None:
    """人工打标：correct | wrong | unclear（复盘模式错误归因面板）"""
    assert human_label in ("correct", "wrong", "unclear")
    get_conn().execute(
        "UPDATE signal SET human_label = ? WHERE id = ?", [human_label, signal_id]
    )
    get_conn().commit()


# ------------------------------------------------------------------ event_log
def log_event(kind: str, payload: dict[str, Any], ts: str) -> None:
    """事件溯源（附录 7.3）。ts 由调用方注入，此处不取系统时间。"""
    get_conn().execute(
        "INSERT INTO event_log (ts, kind, payload) VALUES (?,?,?)",
        [ts, kind, json.dumps(payload, ensure_ascii=False, default=str)],
    )
    get_conn().commit()


# ------------------------------------------------------------------ llm_audit
def log_llm_audit(row: dict) -> int:
    """写一条 LLM 调用审计。

    ★ 与 `log_event` 同样的铁律二纪律：ts 由调用方注入，此处**不取系统时间**。
      唯一区别是本表的 ts 是**墙钟**——因为「这次调用花了多少钱/多少秒」是
      运维事实，无法用业务时点代替（详见 DDL 注释里ts/as_of 两列的理由）。
    """
    cols = ("ts", "as_of", "task", "provider", "model", "prompt_ver",
            "ok", "attempt", "stage", "elapsed_ms",
            "prompt_tokens", "completion_tokens", "cost_cny", "err", "extra")
    get_conn().execute(
        f"INSERT INTO llm_audit ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [row.get(c) for c in cols],
    )
    get_conn().commit()
    return int(get_conn().execute("SELECT last_insert_rowid()").fetchone()[0])


def llm_cost_summary(as_of: str, days: int = 30) -> dict:
    """LLM 成本与用量汇总（规格 2.4 要求 4/ M3「成本统计」）。

    ★ 统计窗口以 **as_of** 为锚倒推 `days` 天，而不是 `date('now')`：
      回放历史时也能得到「当时那段时间的成本」，可复现。
    ★ 缺失值一律返回 0 行而非 None 混淆：调用数为 0 是合法状态。
    """
    con = get_conn()
    #★ 天数必须**内插**而不是绑定参数：SQLite 的 date() 第二个参数是
    #   modifier（形如 '-30 days'），不是普通值⇒ 写成 date(?, ?)
    #   会报 "Incorrect number of bindings"（实测踩过）。
    #   int() 转换是必须的：直接拼 f"{days} days" 在 days 为 str 时会注入 SQL。
    n = max(1, int(days))
    since = con.execute(
        f"SELECT date(?, '-{n} days')", [as_of]).fetchone()[0]
    rows = con.execute(
        """
        SELECT provider, model, task,
               count(*)                                   AS n_calls,
               sum(ok)                                    AS n_ok,
               sum(CASE WHEN stage = 'fallback' THEN 1 ELSE 0 END) AS n_fallback,
               sum(CASE WHEN ok = 0 THEN 1 ELSE 0 END)     AS n_fail,
               sum(coalesce(prompt_tokens, 0))            AS n_prompt,
               sum(coalesce(completion_tokens, 0))        AS n_completion,
               sum(coalesce(elapsed_ms, 0))               AS ms_total,
               sum(coalesce(cost_cny, 0))                 AS cost
        FROM llm_audit
        WHERE substr(coalesce(as_of, ts), 1, 10) >= ?
        GROUP BY provider, model, task
        ORDER BY n_calls DESC
        """, [since]).fetchall()
    return {
        "since": since,
        "as_of": as_of,
        "days": days,
        "rows": [
            {"provider": r[0], "model": r[1], "task": r[2],
             "n_calls": r[3], "n_ok": r[4] or 0,
             "n_fallback": r[5] or 0, "n_fail": r[6] or 0,
             "prompt_tokens": r[7] or 0, "completion_tokens": r[8] or 0,
             "elapsed_ms": round(r[9] or 0, 1),
             "cost_cny": round(r[10] or 0, 6)}
            for r in rows
        ],
    }


# ------------------------------------------------------------------ 交易日历
def set_trade_days(dates: list[str], is_open: dict[str, int] | None = None) -> int:
    c = get_conn()
    c.executemany(
        """
        INSERT INTO trade_calendar (trade_date, is_open) VALUES (?, ?)
        ON CONFLICT(trade_date) DO UPDATE SET is_open=EXCLUDED.is_open
        """,
        [(d, (is_open or {}).get(d, 1)) for d in dates],
    )
    c.commit()
    return len(dates)


def is_trade_day(date_str: str) -> bool:
    """日历缺失时保守返回 False —— 宁可漏跑任务，不可误判交易日发错邮件。"""
    r = get_conn().execute(
        "SELECT is_open FROM trade_calendar WHERE trade_date = ?", [date_str]
    ).fetchone()
    if r is None:
        return False
    return bool(r[0])


if __name__ == "__main__":
    get_conn()
    print("SQLite 初始化完成:", DB_PATH)
    tables = get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    print(f"共 {len(tables)} 张表/视图:")
    for t in tables:
        n = get_conn().execute(f"SELECT count(*) FROM {t[0]}").fetchone()[0]
        print(f"  {t[0]:<24} {n:>8,} 行")