# -*- coding: utf-8 -*-
"""
Hawk · 新浪数据适配器

实测结论（2026-10-08，samples/reachability.json）：
  hq.sinajs.cn                ✅ 200，GBK，批量多只（必须带 Referer）
  vip.stock.finance.sina.com.cn ✅ 200，全市场列表（node=hs_a 单节点覆盖沪深京）
  money.finance.sina.com.cn     ✅ 200，日线（不复权，datalen 生效）

定位：
  - 全市场行情扫描的**主源**（东财 clist 在本机不可达，见 config/sources.yaml）
  - 实时行情的**备源**（腾讯故障时自动切这里）
  - 日线仅用于当日校验与交叉比对，不入回测库（不复权）

字段映射见 config/fields/sina_list.yaml。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from loguru import logger

from backend.data.adapter import BaseAdapter
from backend.data.base import jittered_sleep

ST_KEYWORDS = ("ST", "*ST", "退", "PT")


def is_st_name(name: str) -> bool:
    """风险警示股判定。名称含 ST/*ST/退/PT 即置 1。

    注意 '退' 也会命中正常名称里含'退'字的极少情况，
    但 A 股命名规范下「退」几乎只出现在退市整理期，风险可接受。
    """
    n = (name or "").upper().replace(" ", "")
    return any(k in n for k in ST_KEYWORDS)


class SinaAdapter(BaseAdapter):
    name = "sina"
    qps = 5.0
    referer = "https://finance.sina.com.cn"
    encoding = "gbk"

    # ---------------- 实时行情（备源）----------------
    def fetch_realtime(self, codes: list[str], as_of: datetime) -> list[dict]:
        if not codes:
            return []
        from backend.data.adapters.tencent import market_prefix

        syms = [f"{market_prefix(c)}{c}" for c in codes]
        out: list[dict] = []
        for i in range(0, len(syms), 60):
            batch = syms[i : i + 60]
            text = self._get("https://hq.sinajs.cn/list=" + ",".join(batch))
            out.extend(self._parse_quotes(text))
        return self._guard(out, "realtime")

    def _parse_quotes(self, text: str) -> list[dict]:
        """新浪格式：var hq_str_sh600519="名称,今开,昨收,现价,最高,最低,买一,卖一,成交量,成交额,...";
        共 33 个字段，时间与日期在倒数第 3、第 2 位。
        """
        rows = []
        for line in text.strip().splitlines():
            m = re.match(r'var hq_str_([a-z]{2}\d{6})="(.*)";', line.strip())
            if not m:
                continue
            symbol, body = m.group(1), m.group(2)
            f = body.split(",")
            if len(f) < 32:
                continue
            name, opn, pre, price, high, low = f[0], f[1], f[2], f[3], f[4], f[5]
            volume = self._i(f[8])          # 新浪成交量单位为股（实测茅台 2516757）
            amount = self._f(f[9])
            date, tm = f[30], f[31]
            # ★★ 盘前兜底：新浪在**开盘前**会把现价/最高/最低都填 0
            #   （实测 2026-10-09 09:11 的原始响应：
            #     名称,0.000,1255.790,0.000,0.000,0.000,...，字段2 是昨收 1255.79）
            #   若原样返回，`_guard` 会因 price=0 把**全部**记录丢掉
            #   ⇒ 降级链在盘前时段完全失效（实测 fetch_realtime 返回 0 条）。
            #   盘前没有「现价」这回事，用昨收作为参考价是唯一合理的选择。
            px = self._f(price)
            hi, lo, op = self._f(high), self._f(low), self._f(opn)
            if px <= 0 and self._f(pre) > 0:
                px = self._f(pre)
                hi = hi if hi > 0 else px
                lo = lo if lo > 0 else px
                op = op if op > 0 else px
            rows.append({
                "code": symbol[2:],
                "symbol": symbol,
                "name": name,
                "price": px,
                "pre_close": self._f(pre),
                "open": op,
                "high": hi,
                "low": lo,
                "volume": volume,
                "amount": amount,
                "pct_chg": round((px / self._f(pre) - 1) * 100, 4)
                          if px and self._f(pre) else None,
                "turnover": None,           # 该接口无换手率
                "float_mv": None,
                "total_mv": None,
                "ts": f"{date} {tm}",
            })
        return rows

    # ---------------- 全市场列表（准实时轨道主源）----------------
    def fetch_stock_list(self, as_of: datetime, max_pages: int = 70) -> list[dict]:
        """分页拉全市场。实测 node=hs_a 单节点覆盖沪深京全部 A 股，num=100 可用。

        5400 只 / 100 = 54 页，按 5 QPS + 批间随机 sleep 约需 30~50 秒，
        满足 30~60s 全市场扫描周期。
        """
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            text = self._get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                "json_v2.php/Market_Center.getHQNodeData",
                {"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a"},
            )
            try:
                rows = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.warning(f"[sina] 第 {page} 页 JSON 解析失败: {exc}")
                break
            if not rows:
                break                         # 最后一页，正常终止
            for r in rows:
                out.append(self._map_list_row(r))
            logger.info(f"[sina] 股票列表 {page} 页，累计 {len(out)} 只")
            page += 1
            jittered_sleep(0.35, jitter=0.30)
        return self._guard(out, "stock_list")

    def _map_list_row(self, r: dict) -> dict:
        """字段映射见 config/fields/sina_list.yaml。

        注意市值单位：接口 mktcap/nmc 返回**万元**，需×10000 换算为元。
        """
        symbol = r.get("symbol", "")
        market = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(symbol[:2].lower(), "")
        name = r.get("name", "")
        price = self._f(r.get("trade"))
        high = self._f(r.get("high"))
        low = self._f(r.get("low"))
        opn = self._f(r.get("open"))
        pre = self._f(r.get("settlement"))
        # 停牌判定（附录 C.3）：今日无成交（现价 0）或开高低全为 0。
        # 停牌股必须被显式标记 —— 缠论引擎遇到停牌要跳过该 K 线，
        # 否则包含处理会因连续同价bar而出错。
        is_suspend = (price is None or price == 0) or (
            high is not None and low is not None and high == 0 and low == 0
        )
        return {
            "id": r.get("code"),                       # 主键用6 位代码
            "symbol": symbol,
            "code": r.get("code"),
            "name": name,
            "market": market,
            "price": price,
            "pre_close": pre,
            "open": opn,
            "high": high,
            "low": low,
            "pct_chg": self._f(r.get("changepercent")),
            "change": self._f(r.get("pricechange")),
            "volume": self._i(r.get("volume")),
            "amount": self._f(r.get("amount")),
            "turnover": self._f(r.get("turnoverratio")),
            "total_mv": (self._f(r.get("mktcap")) or 0) * 10000,   # 万元 -> 元
            "float_mv": (self._f(r.get("nmc")) or 0) * 10000,      # 万元 -> 元
            "pe": self._f(r.get("per")),
            "pb": self._f(r.get("pb")),
            "is_st": 1 if is_st_name(name) else 0,
            "is_suspend": 1 if is_suspend else 0,
            # 接口不返回这两项 —— 保持 None，下游过滤时必须显式处理，不得默认通过
            "industry": None,
            "list_date": None,
            "concept": None,
            "status": "suspended" if is_suspend else "active",
        }

    # ---------------- 日线（不复权，仅校验用）----------------
    def fetch_daily(self, code: str, count: int = 250,
                    as_of: datetime | None = None) -> list[dict]:
        """scale=240 即日线。该接口无复权参数，返回**不复权**价。

        回测统一用腾讯 qfq，此方法用于交叉校验与当日增量兜底。
        """
        from backend.data.adapters.tencent import market_prefix

        sym = f"{market_prefix(code)}{code}"
        text = self._get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData",
            {"symbol": sym, "scale": 240, "ma": "no", "datalen": count},
        )
        rows = json.loads(text)
        out = []
        for r in rows:
            out.append({
                "code": code,
                "trade_date": r["day"],
                "open": self._f(r.get("open")),
                "high": self._f(r.get("high")),
                "low": self._f(r.get("low")),
                "close": self._f(r.get("close")),
                "volume": self._i(r.get("volume")),   # 实测单位：股
                "is_suspend": False,
                "_fq": "none",
            })
        out.sort(key=lambda x: x["trade_date"])
        return self._guard(out, "kline_daily")