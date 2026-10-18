# -*- coding: utf-8 -*-
"""
Hawk · 消息源抽象（铁律三：抓取与解析分离）

为什么这样设计：
    抓取（fetch）与解析（parse）必须分离，否则解析逻辑一改就要重抓，
    且无法离线重放历史报文。分离后：
      · 抓取层只负责「拿到原始报文，原样落raw_news」，不做任何字段理解
      · 解析层异步消费 raw_news，可反复重跑（改prompt / 换模型都不必重抓）
    这是 M3 消息打标与 M8 复盘重放的基础。

统一结构（所有源归一到这里）：
    {
      "source":     源标识，如 "sina_7x24"
      "external_id": 源内唯一 id（去重用）
      "ts":         源给出的时间（北京时间字符串）
      "title":      标题（可空）
      "content":    正文/快讯文本
      "url":        原文链接
      "tags":       分类标签列表
      "related":    源直接给出的关联标的（如新浪 ext.stocks）
      "raw":        原始报文（JSON 串或 HTML 片段）
    }

★ related 是「源直接给出的关联」，与 LLM 抽取的间接关联是两回事：
    前者可信度高（源的结构化字段），后者是推断（可能幻觉）。
    落库时分别存，UI 上要能区分展示。
"""
from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST = timezone(timedelta(hours=8))

# 铁律三：抓取层只落原始报文，**不做任何解析判断**
# 因此这里不 import sqlite / 不写库，只负责产出统一结构。
# 落库由 scripts/fetch_news.py 调用 backend.data.sqlite.insert_raw_news。


class NewsSource(ABC):
    """消息源基类。"""

    name: str = "base"
    qps: float = 1.0
    requires_browser: bool = False    # True 表示需 Playwright（M2 后期实现）

    def __init__(self, qps: float | None = None) -> None:
        self.qps = qps or self.qps

    @abstractmethod
    def fetch(self, as_of: datetime, limit: int = 50) -> list[dict]:
        """抓取最近若干条，返回统一结构列表。

        实现要求：
          · 只做「拿到什么就返回什么」，不丢弃、不改写、不判断利好利空
          · 时间为源提供的时间，不取系统时间（铁律二）
          · 单条失败不影响整体，尽量返回能拿到的部分
        """

    def normalize(self, raw_item: dict) -> dict:
        """把源结构归一。子类覆盖。"""
        return raw_item

    def __repr__(self) -> str:
        mode = "browser" if self.requires_browser else "http"
        return f"<{self.name} qps={self.qps} mode={mode}>"


# ------------------------------------------------------------------ 注册表
_SOURCES: dict[str, NewsSource] = {}


def register_source(name: str, factory: Callable[[], NewsSource]) -> None:
    _SOURCES[name] = factory()


def get_source(name: str) -> NewsSource:
    if name not in _SOURCES:
        raise KeyError(f"消息源 {name} 未注册。已注册: {sorted(_SOURCES)}")
    return _SOURCES[name]


def registered_sources() -> list[str]:
    return sorted(_SOURCES)


def bootstrap_sources() -> None:
    """注册全部消息源。"""
    try:
        from backend.domain.news.sina_7x24 import Sina7x24Source
        register_source("sina_7x24", Sina7x24Source)
    except ImportError as exc:
        logger.warning(f"新浪7x24 源未加载: {exc}")
    try:
        from backend.domain.news.jin10 import Jin10Source
        register_source("jin10", Jin10Source)
    except ImportError as exc:
        logger.warning(f"金十源未加载: {exc}")
    try:
        from backend.domain.news.cls_telegraph import ClsTelegraphSource
        register_source("cls", ClsTelegraphSource)
    except ImportError as exc:
        logger.warning(f"财联社源未加载: {exc}")


# ------------------------------------------------------------------ 去重
def dedup_key(item: dict) -> str:
    """去重键：优先用源内id，缺失时退回 (源 + 时间 + 内容前80 字) 哈希。

    不依赖 LLM：去重必须在原始层做，且要稳定可复现。
    """
    eid = item.get("external_id")
    if eid:
        return f"{item.get('source')}#{eid}"
    import hashlib

    text = (item.get("content") or item.get("title") or "")[:80]
    ts = (item.get("ts") or "")[:16]
    h = hashlib.sha1(f"{ts}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"{item.get('source')}#{h}"


def merge_sources(*batches: list[dict]) -> list[dict]:
    """多源合并去重。同一事件被多源报道时保留全部（sources 字段记录）。

    ★ 注意：不按内容相似度合并 —— 那属于 M3 的聚类去重，
      原始层只做「完全相同」的精确去重，避免误合并不同事件。
    """
    seen: dict[str, dict] = {}
    for batch in batches:
        for it in batch:
            k = dedup_key(it)
            if k in seen:
                srcs = seen[k].setdefault("also_seen_in", [])
                s = it.get("source")
                if s and s not in srcs:
                    srcs.append(s)
            else:
                seen[k] = it
    return list(seen.values())


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    bootstrap_sources()
    print("已注册消息源:", registered_sources())