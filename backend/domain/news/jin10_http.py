"""金十公开快讯 HTTP 协议，2026-10-11 依据公开前端及响应实测。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from backend.domain.news.base import NewsSource, CST

ENDPOINT = "https://flash-api.jin10.com/get_flash_list"
# 来自公开前端的应用标识，不是用户密钥；不使用登录态或付费接口。
HEADERS = {"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0",
           "Referer": "https://www.jin10.com/"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    if not isinstance(value, str):
        raise ValueError("text field must be a string")
    parser = _Text()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


class Jin10Source(NewsSource):
    name = "jin10"
    qps = 0.5
    requires_browser = False

    def fetch(self, as_of: datetime, limit: int = 50) -> list[dict]:
        """只取公开最新一页；as_of 为采集基准，不伪装为历史查询。"""
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.last_stats = dict(received=0, locked=0, ads=0, empty=0, invalid=0)
        with httpx.Client(timeout=12.0, trust_env=False, headers=HEADERS) as client:
            response = client.get(ENDPOINT)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or body.get("status") != 200:
            raise ValueError("Jin10 business status is not 200")
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ValueError("Jin10 data must be an array")
        self.last_stats["received"] = len(rows)
        out = []
        for row in rows:
            try:
                if not isinstance(row, dict) or not isinstance(row.get("data"), dict):
                    raise ValueError("invalid item shape")
                data = row["data"]
                if data.get("lock") or data.get("exclusive_to"):
                    self.last_stats["locked"] += 1
                    continue
                if (row.get("extras") or {}).get("ad"):
                    self.last_stats["ads"] += 1
                    continue
                item = self.normalize(row)
                if not item["content"]:
                    self.last_stats["empty"] += 1
                    continue
                out.append(item)
            except (ValueError, TypeError, AttributeError, KeyError):
                self.last_stats["invalid"] += 1
        if self.last_stats["invalid"] and not out:
            raise ValueError("Jin10 response has no valid public items (schema drift)")
        return out[:limit]

    def normalize(self, row: dict) -> dict:
        data = row["data"]
        eid = str(row.get("id") or "")
        if not re.fullmatch(r"\d{10,30}", eid):
            raise ValueError("invalid Jin10 id")
        ts = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        title = plain(data.get("title") or "")
        content = plain(data.get("content") or "")
        link = data.get("source_link") or data.get("link")
        if not isinstance(link, str) or urlsplit(link).scheme not in ("http", "https"):
            link = None
        return dict(source=self.name, external_id=eid, ts=ts.isoformat(),
                    title=title or None, content=content or title, url=link,
                    tags=["重要"] if row.get("important") == 1 else [],
                    related=[], raw=json.dumps(row, ensure_ascii=False))
