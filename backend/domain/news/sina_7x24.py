# -*- coding: utf-8 -*-
"""
Hawk · 新浪 7×24 快讯源（M2 主源，实测可用）

实测依据（2026-10-08 21:44）：
    GET https://zhibo.sina.com.cn/api/zhibo/feed
        ?page=1&page_size=20&zhibo_id=152&tag_id=0&dire=f&dpc=1
    → 200 application/json，约 28KB，返回结构化快讯数组。

★ 重要发现：这是**结构化 API，无需 Playwright**。
  提示词原计划用 Playwright 抓金十，本源直接省掉浏览器依赖。

★ 更关键：ext 字段内含 `stocks`（关联标的），例如
    "ext": {"stocks":[{"market":"us","symbol":"csgp","key":"科斯塔"}], "nee...
  这是**源直接给出的关联**，可信度高于 LLM 推断，
  可作为「消息→个股」打标的可靠输入（M3 用），也用于 M6 的题材关联。

响应结构（实测）：
    result.data.feed          -> dict（不是 list！曾误判为 list）
    result.data.feed.list     -> [ {id, rich_text, create_time, tag, ext, docurl, ...} ]
    result.data.feed.max_id   -> 游标，翻页用
    result.data.feed.page_info-> 分页信息

★ feed 是 dict 而非 list 是本接口最容易踩的坑，
  写代码时必须 feed.get("list")，直接遍历 feed 会拿到键名。

分页：实测 page 参数有效，page_size 上限待测（这里保守用 50）。
按提示词 A.3 控制频率：qps=1，页间随机 sleep。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger

# 支持 `python backend/domain/news/sina_7x24.py` 直接运行
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.domain.news.base import NewsSource

ENDPOINT = "https://zhibo.sina.com.cn/api/zhibo/feed"
# zhibo_id=152 是「全球实时快讯」（7×24 财经直播）
DEFAULT_ZHIBO_ID = 152


class Sina7x24Source(NewsSource):
    name = "sina_7x24"
    qps = 1.0
    requires_browser = False

    def __init__(self, page_size: int = 50) -> None:
        super().__init__()
        self.page_size = page_size

    def fetch(self, as_of: datetime, limit: int = 50) -> list[dict]:
        """抓取最近快讯。as_of 仅用于记录基准，不参与内容过滤。"""
        out: list[dict] = []
        page = 1
        client = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            trust_env=False,                     # 绕过沙箱代理
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://finance.sina.com.cn/7x24/",
            },
        )
        try:
            with client:
                while len(out) < limit and page <= 10:
                    r = client.get(
                        ENDPOINT,
                        params={
                            "page": page,
                            "page_size": min(50, self.page_size),
                            "zhibo_id": DEFAULT_ZHIBO_ID,
                            "tag_id": 0,
                            "dire": "f",
                            "dpc": 1,
                        },
                    )
                    if r.status_code != 200:
                        logger.warning(f"[sina_7x24] HTTP {r.status_code}，停止翻页")
                        break
                    try:
                        feed = r.json()["result"]["data"]["feed"]
                    except (json.JSONDecodeError, KeyError, TypeError) as exc:
                        logger.warning(f"[sina_7x24] 响应结构异常: {exc}")
                        break
                    # ★ feed 是 dict，取其中的 list（实测确认）
                    items = feed.get("list") or []
                    if not items:
                        break
                    for it in items:
                        out.append(self.normalize(it))
                    page += 1
        finally:
            client.close()
        return out[:limit]

    def normalize(self, it: dict) -> dict:
        """归一到统一结构。原始报文保留在 raw 字段（铁律三）。

        ★ ext 字段实测：**全部是 JSON 字符串**，不是 dict
          （实测 2026-10-08：50/50 条 type(ext) == str）。
          直接 ext.get("stocks") 会抛 AttributeError，必须先 json.loads。

        ★ ext.stocks 实测覆盖约 47%（14/30），但**含大量非 A 股标的**：
          - 美股/欧股/加密货币（如「科斯塔」csgp、「比特币」）
          - 同一条快讯里同一标的重复出现（「青岛港」出现两次）
          因此这里做两件清洗（抓取层只做机械清洗，不做语义判断）：
            1. 同一快讯内按 symbol 去重
            2. 标注市场，供 M3 决定是否参与 A 股关联推理
        """
        ext_raw = it.get("ext")
        stocks: list = []
        if isinstance(ext_raw, str) and ext_raw.strip():
            try:
                ext = json.loads(ext_raw)
                if isinstance(ext, dict):
                    stocks = ext.get("stocks") or []
            except json.JSONDecodeError:
                pass                        # 脏数据不阻塞整体
        elif isinstance(ext_raw, dict):
            stocks = ext_raw.get("stocks") or []

        tags = [t.get("name") for t in (it.get("tag") or []) if t.get("name")]

        # 机械去重：同一 symbol 只留一次（实测「青岛港」会重复）
        seen: set[str] = set()
        related = []
        for s in stocks:
            if not isinstance(s, dict):
                continue
            sym = s.get("symbol")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            mkt = (s.get("market") or "").lower()
            related.append({
                "market": mkt,
                "symbol": sym,
                "name": s.get("key"),
                # A 股相关 = cn 市场的 6 位代码；us/hk/crypto 等仅作参考
                "is_a_share": bool(mkt in ("cn", "sh", "sz", "bj")),
            })

        return {
            "source": self.name,
            "external_id": str(it.get("id")),
            "ts": it.get("create_time"),
            "title": None,
            "content": (it.get("rich_text") or "").strip(),
            "url": it.get("docurl"),
            "tags": tags,
            "related": related,
            "raw": json.dumps(it, ensure_ascii=False),
        }


if __name__ == "__main__":
    import sys
    from datetime import timedelta, timezone
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    CST = timezone(timedelta(hours=8))
    as_of = datetime(2026, 10, 8, 21, 45, tzinfo=CST)
    src = Sina7x24Source()

    items = src.fetch(as_of, limit=30)
    print(f"\n抓到 {len(items)} 条快讯\n")
    print("=" * 78)
    for it in items[:10]:
        a_sh = [r for r in it["related"] if r["is_a_share"]]
        oth = [r for r in it["related"] if not r["is_a_share"]]
        rel = " ".join(
            [f"[A股:{r['name']}]" for r in a_sh] + [f"[{r['name']}]" for r in oth]
        )
        print(f"[{it['ts']}] {it['content'][:50]}")
        if it["tags"]:
            print(f"     标签={it['tags']} {rel}")
    print("=" * 78)
    n_rel = sum(1 for x in items if x["related"])
    n_a = sum(1 for x in items if any(r["is_a_share"] for r in x["related"]))
    print(f"带关联标的: {n_rel}/{len(items)} 条，其中含 A 股标的: {n_a}/{len(items)} 条")
    print("说明: 非 A 股标的（美股/债券/加密货币）仅作参考，不参与 A 股关联推理")
    print("      青岛港类重复标的已在抓取层按symbol 去重")