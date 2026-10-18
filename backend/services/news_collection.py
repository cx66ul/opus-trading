"""新闻采集基础设施：独立 SQLite 连接、持久去重和桌面后台生命周期。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from backend.data.sqlite import DB_PATH
from backend.domain.news.base import CST, dedup_key

LOG = logging.getLogger(__name__)


def wallclock():
    return datetime.now(CST)


def _payload(item):
    return {key: item.get(key) for key in
            ("external_id", "ts", "title", "content", "url", "tags", "related", "raw")}


def _key(item):
    # 原始报文中的语音、推送等元数据变化不产生正文修订。
    semantic = {k: item.get(k) for k in ("ts", "title", "content", "url", "tags", "related")}
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True)
    return dedup_key(item), hashlib.sha256(encoded.encode()).hexdigest()


def _connect(path):
    con = sqlite3.connect(str(path), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS raw_news (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "source TEXT NOT NULL, ts TEXT, payload TEXT, fetched_at TEXT, parsed INTEGER DEFAULT 0)")
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE name='news_receipt'").fetchone()
        con.execute("CREATE TABLE IF NOT EXISTS news_receipt (item_key TEXT, revision TEXT, "
                    "PRIMARY KEY(item_key,revision))")
        con.execute("CREATE TABLE IF NOT EXISTS news_source_status (source TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        if not exists:
            # 一次性登记旧报文，不改动历史记录或 parsed 状态。
            for row in con.execute("SELECT source,payload FROM raw_news").fetchall():
                try:
                    item = json.loads(row["payload"])
                    item["source"] = row["source"]
                    con.execute("INSERT OR IGNORE INTO news_receipt VALUES (?,?)", _key(item))
                except (ValueError, TypeError):
                    continue
        con.commit()
        return con
    except Exception:
        con.close()
        raise


def save_items(items, as_of, db_path=DB_PATH):
    con = _connect(db_path)
    saved = duplicates = 0
    try:
        with con:
            for item in items:
                added = con.execute("INSERT OR IGNORE INTO news_receipt VALUES (?,?)", _key(item)).rowcount
                if not added:
                    duplicates += 1
                    continue
                con.execute("INSERT INTO raw_news(source,ts,payload,fetched_at) VALUES (?,?,?,?)",
                            (item["source"], item.get("ts"), json.dumps(_payload(item), ensure_ascii=False),
                             as_of.isoformat(timespec="seconds")))
                saved += 1
    finally:
        con.close()
    return dict(saved=saved, duplicates=duplicates, fetched_at=as_of.isoformat(timespec="seconds"))


def collect_source(source, as_of, limit=40, db_path=DB_PATH):
    result = dict(source=source.name, state="error", attempted_at=as_of.isoformat(timespec="seconds"))
    try:
        items = source.fetch(as_of, limit)
        result.update(save_items(items, as_of, db_path))
        stats = getattr(source, "last_stats", {})
        result.update(state="degraded" if stats.get("invalid") else "ok", fetched=len(items),
                      latest_item=max((i["ts"] for i in items), default=None),
                      skipped=stats, error=None)
    except NotImplementedError as exc:
        result.update(state="unsupported", error=str(exc).splitlines()[0])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
        result["skipped"] = getattr(source, "last_stats", {})
    con = _connect(db_path)
    try:
        with con:
            prev = con.execute("SELECT payload FROM news_source_status WHERE source=?", (source.name,)).fetchone()
            previous = json.loads(prev[0]) if prev else {}
            result["last_success"] = (as_of.isoformat(timespec="seconds") if result["state"] in ("ok", "degraded")
                                      else previous.get("last_success"))
            con.execute("INSERT OR REPLACE INTO news_source_status VALUES (?,?)",
                        (source.name, json.dumps(result, ensure_ascii=False)))
    finally:
        con.close()
    return result


class NewsCollector:
    """金十与新浪采集、重点股票解析；停止信号可立即中断轮询等待。"""
    interval_seconds = 60

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.stop_event = threading.Event()
        self.thread = None
        self.analysis_thread = None
        self.analysis_status = {"state": "starting"}

    def start(self):
        if os.environ.get("OPUS_NEWS_AUTOCOLLECT", "1") == "0":
            self.analysis_status = {"state": "not_running"}
            return
        self.thread = threading.Thread(target=self._run, name="jin10-collector", daemon=True)
        self.thread.start()
        self.analysis_thread = threading.Thread(target=self._analyze, name="tracked-news-analysis", daemon=True)
        self.analysis_thread.start()

    def _analyze(self):
        from backend.services.tracked_news import process_batch
        if self.stop_event.wait(5):
            return
        while not self.stop_event.is_set():
            try:
                self.analysis_status = process_batch(wallclock(), self.db_path, stop_event=self.stop_event)
            except Exception as exc:
                self.analysis_status = {"state": "error", "error": str(exc)[:300]}
                LOG.exception("重点股票新闻解析失败")
            if self.stop_event.wait(30):
                break

    def _run(self):
        from backend.domain.news.jin10 import Jin10Source
        from backend.domain.news.sina_7x24 import Sina7x24Source
        # 让桌面 ready/首屏先返回；HTTP 不在 API 主线程执行。
        if self.stop_event.wait(2):
            return
        sources = (Jin10Source(), Sina7x24Source())
        while not self.stop_event.is_set():
            for source in sources:
                if self.stop_event.is_set():
                    break
                try:
                    collect_source(source, wallclock(), limit=40, db_path=self.db_path)
                except Exception:
                    LOG.exception("新闻采集状态写入失败")
            if self.stop_event.wait(self.interval_seconds):
                break

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.analysis_thread:
            self.analysis_thread.join(timeout=1)

    def status(self):
        result = source_status(self.db_path, bool(self.thread and self.thread.is_alive()
                                               and not self.stop_event.is_set()), self.interval_seconds)
        result["analysis"] = self.analysis_status
        return result


def source_status(db_path=DB_PATH, running=False, interval=60):
    result = dict(running=running, interval_seconds=interval, sources=[], recent=[])
    if not Path(db_path).exists():
        return result
    # 查询端不建表，不获取采集写锁。
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='news_source_status'").fetchone():
            return result
        for name, payload in con.execute("SELECT source,payload FROM news_source_status ORDER BY source"):
            info = json.loads(payload)
            info["stale"] = (wallclock() - datetime.fromisoformat(info["attempted_at"])).total_seconds() > interval * 3
            info["raw_total"], info["pending"] = con.execute(
                "SELECT count(*),coalesce(sum(parsed=0),0) FROM raw_news WHERE source=?", (name,)).fetchone()
            result["sources"].append(info)
        for ts, payload in con.execute("SELECT ts,payload FROM raw_news WHERE source='jin10' ORDER BY ts DESC,id DESC LIMIT 5"):
            item = json.loads(payload)
            result["recent"].append(dict(ts=ts, content=item.get("content"), title=item.get("title")))
    finally:
        con.close()
    return result
