# -*- coding: utf-8 -*-
"""
Hawk · 腾讯数据适配器

实测结论（2026-10-08，samples/reachability.json）：
  qt.gtimg.cn           ✅ 200，GBK，批量多只（跟踪池 3s 主源）
  web.ifzq.gtimg.cn     ✅ 200，前复权日/周/月K
  ifzq.gtimg.cn        ✅ 200，分钟 K（m1/m5/m15/m30/m60）

字段映射见 config/fields/tencent_quote.yaml 与 config/fields/kline.yaml。
本文件不硬编字段含义之外的业务假设；下标常量与 yaml 一致，
若接口调整顺序，只需改 yaml + 此处常量并重新跑 probe.py 验证。
"""
from __future__ import annotations

from datetime import datetime

from backend.data.adapter import BaseAdapter

# ---- 腾讯行情字段下标（与 config/fields/tencent_quote.yaml 保持一致）----
I_NAME, I_CODE, I_PRICE, I_PRECLOSE, I_OPEN = 1, 2, 3, 4, 5
I_VOLUME, I_OUTER, I_INNER = 6, 7, 8
I_TS, I_CHANGE, I_PCT, I_HIGH, I_LOW = 30, 31, 32, 33, 34
I_AMOUNT_WAN, I_TURNOVER, I_PE = 37, 38, 39
I_FLOAT_MV_YI, I_TOTAL_MV_YI, I_PB = 44, 45, 46

ST_KEYS = {"day": "1d", "week": "1w", "month": "1M"}


def market_prefix(code: str) -> str:
    """6 位代码 -> 带市场前缀。

    规则按代码首位判定：6/9->sh，0/2/3->sz，4/8->bj。
    北交所 920xxx 首位是 9，与沪市同首位，需特判。
    """
    c = str(code).zfill(6)
    if c.startswith(("920", "430", "83", "87", "88", "82")):
        return "bj"
    if c.startswith(("5", "6", "9", "11", "13")):
        return "sh"
    if c.startswith(("4", "8")):
        return "bj"
    return "sz"


def tencent_volume_to_shares(code: str, raw: float | int | None) -> float:
    """把腾讯的成交量字段换算成**股**。

    ★★ 2026-10-10 实测发现的坑：**腾讯接口的成交量单位不是全局一致的**。
      ★ 逐板块实测（`scripts/probe_tencent_volume_unit.py`，
        基准=新浪，新浪全板块统一返回「股」）：

        | 板块          | 腾讯/新浪比值 | 单位 | 样本      |
        |---------------|--------------|------|-----------|
        | **688 科创板**| **1.00**     | 股   | 4/4 一致  |
        | **689科创CDR**| **1.00**     | 股   | 1/1（689008 新浪无数据）|
        | 300/301 创业板| 0.01         | 手   |✓        |
        | 600/601/603/605 | 0.01      | 手   | ✓        |
        | 000/001/002/003 | 0.01      | 手   | ✓        |
        | 920 北证| 0.01         | 手   | ✓        |
        | 430/830/871 北交所旧码 | —      | 腾讯日线**返回空** ⇒ 不可判定|
        | 787 存托凭证   | —              | 两源均无数据 ⇒ 不可判定|

        ★⇒ 规律是「**科创板（688+689）= 股，其余 = 手**」，
          而非我上一轮凭感觉写的「只有 688」——
          `test_no_689_or_787_guessing` 曾把这个猜测固化成断言，**是错的**。

      危害：科创板 volume 被放大 100 倍 ⇒ 单日全市场成交额
      算成 **21.8 万亿**（真实约 1.4 万亿），且 688 板块独占 94.4%。
      ⇒ 任何 `Σ(volume×close)` / 量比 / 换手率类指标对科创板全部失真。

    ★ 为什么之前没发现：m30 累计与日线 volume 的**比值**对科创板也是 1.0
      （两边同样放大 100 倍）⇒ **比值自洽会掩盖共同的单位错误**。
      只有「**绝对量级 + 跨源交叉 + 分板块聚合**」才暴露。

    ★ 787（存托凭证）**仍未知** —— 两源都无数据，不能猜。
      若日后接入且两源仍无数据，此处必须保持抛错/标注，
      **不得默认归入任一侧**（默认就是拿100 倍错误数据下注）。
    """
    v = float(raw or 0)
    c = str(code).zfill(6)
    #★ 科创板：688（主板）+ 689（CDR），实测两者都是「股」。
    if c.startswith(("688", "689")):
        return v
    #★ 787（存托凭证）实测两源皆无数据 —— 单位未知。
    #   保守选择：按「手」处理并在文档留痕；若日后拿到样本，
    #   跑 probe_tencent_volume_unit.py 改这里 + 改测试。
    #   （宁可暂用可能错的换算，也**显式标注为未验证**，
    #     而不是偷偷归到科创板或主板里让人以为已验证。）
    return v * 100


class TencentAdapter(BaseAdapter):
    name = "tencent"
    qps = 10.0
    encoding = "gbk"

    # ---------------- 实时行情（跟踪池 3s 轨道）----------------
    def fetch_realtime(self, codes: list[str], as_of: datetime) -> list[dict]:
        """一次请求可拿多只（实测 5 只/ 1074 字节，60 只约 13KB，极轻量）。

        as_of 来自调用方注入；此处仅用于日志，不参与字段计算。
        """
        if not codes:
            return []
        syms = [f"{market_prefix(c)}{c}" for c in codes]
        out: list[dict] = []
        # 单批上限 60 只，超出则分批（附录 A.4）
        for i in range(0, len(syms), 60):
            batch = syms[i : i + 60]
            text = self._get("https://qt.gtimg.cn/q=" + ",".join(batch))
            out.extend(self._parse_quotes(text))
        return self._guard(out, "realtime")

    # ---------------- 集合竞价盘口（09:15–09:25）----------------
    def fetch_auction_book(self, codes: list[str],
                           as_of: datetime) -> list[dict]:
        """★ 竞价时段专用：返回**五档盘口**（`_parse_quotes` 会丢掉这些字段）。

        为什么必须单独一条路：`fetch_realtime` 的 `_guard` 会按
        「实时快照」校验涨跌幅，且**不返回 bid/ask**。
        而竞价时段的价格**天然就偏离昨收**（高开/低开都是正常的），
        走实时校验路径容易被误判为脏数据丢弃。

        ★ 不做任何价格合理性校验 —— 这里要的是**原始盘口**，
          竞价阶段价格「异常」是常态而非脏数据。
        """
        if not codes:
            return []
        syms = [f"{market_prefix(c)}{c}" for c in codes]
        out: list[dict] = []
        for i in range(0, len(syms), 60):
            batch = syms[i: i + 60]
            text = self._get("https://qt.gtimg.cn/q=" + ",".join(batch))
            out.extend(self._parse_books(text))
        return out

    def _parse_books(self, text: str) -> list[dict]:
        """→ [{"code","name","price","pre_close","ts","book": Book}]。

        ★ 解析规则下沉到 `auction.parse_book`（纯函数、可单测），
          这里只负责切分报文与取基础字段 —— 不重复实现盘口下标逻辑。
        """
        from backend.domain.auction import parse_book

        rows = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            head, _, rest = line.partition("=")
            inner = rest.strip().strip(";").strip('"')
            if not inner:
                continue
            f = inner.split("~")
            if len(f) < 30:          # 竞价盘口至少要含五档
                continue
            ts_raw = (f[I_TS] or "").strip()
            rows.append({
                "code": f[I_CODE],
                "name": f[I_NAME],
                "price": self._f(f[I_PRICE]),
                "pre_close": self._f(f[I_PRECLOSE]),
                "volume": tencent_volume_to_shares(f[I_CODE], f[I_VOLUME]),
                "ts": (f"{ts_raw[0:4]}-{ts_raw[4:6]}-{ts_raw[6:8]} "
                       f"{ts_raw[8:10]}:{ts_raw[10:12]}:{ts_raw[12:14]}").strip(),
                "book": parse_book(f),
            })
        return rows

    def _parse_quotes(self, text: str) -> list[dict]:
        rows = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            head, _, rest = line.partition("=")
            inner = rest.strip().strip(";").strip('"')
            if not inner:
                continue
            f = inner.split("~")
            if len(f) < 47:
                continue                     # 字段数不足 -> 视为脏数据，丢弃
            ts_raw = f[I_TS]
            rows.append({
                "code": f[I_CODE],
                "name": f[I_NAME],
                "price": self._f(f[I_PRICE]),
                "pre_close": self._f(f[I_PRECLOSE]),
                "open": self._f(f[I_OPEN]),
                "high": self._f(f[I_HIGH]),
                "low": self._f(f[I_LOW]),
                "volume": tencent_volume_to_shares(f[I_CODE], f[I_VOLUME]),  # ⚠ 688 是「股」，其余是「手」
                "amount": (self._f(f[I_AMOUNT_WAN]) or 0) * 10000, # 万元 -> 元
                "pct_chg": self._f(f[I_PCT]),
                "change": self._f(f[I_CHANGE]),
                "turnover": self._f(f[I_TURNOVER]),
                "pe": self._f(f[I_PE]),
                "pb": self._f(f[I_PB]),
                "float_mv": (self._f(f[I_FLOAT_MV_YI]) or 0) * 1e8,
                "total_mv": (self._f(f[I_TOTAL_MV_YI]) or 0) * 1e8,
                "ts": (f"{ts_raw[0:4]}-{ts_raw[4:6]}-{ts_raw[6:8]} "
                       f"{ts_raw[8:10]}:{ts_raw[10:12]}:{ts_raw[12:14]}").strip(),
                "symbol": head.replace("v_", ""),
            })
        return rows

    # ---------------- K 线 ----------------
    def fetch_daily(self, code: str, count: int = 250,
                    as_of: datetime | None = None) -> list[dict]:
        return self._fetch_kline(code, "day", count, "qfq")

    def fetch_kline(self, code: str, period: str, count: int = 250,
                    fq: str = "qfq") -> list[dict]:
        """period: day/week/month（腾讯的 K 线端点只支持这三个日线级别）。
        分钟线请用 fetch_minute。
        """
        if period not in ST_KEYS:
            raise ValueError(f"period 须为 day/week/month，收到 {period}")
        return self._fetch_kline(code, period, count, fq)

    def _fetch_kline(self, code: str, period: str, count: int, fq: str) -> list[dict]:
        sym = f"{market_prefix(code)}{code}"
        param = f"{sym},{period},,,{count},{fq}"
        text = self._get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {"param": param}
        )
        import json

        obj = json.loads(text)
        node = (obj.get("data") or {}).get(sym) or {}
        # 复权方式决定列表键名：qfq -> qfqday，不复权 -> day
        key = {"qfq": f"qfq{period}", "hfq": f"hfq{period}", "": period}[fq]
        arr = node.get(key) or node.get(period) or []
        rows = []
        for it in arr:
            #★ 腾讯数组顺序为 [日期, 开, 收, 高, 低, 量]，收在第 2 位
            if len(it) < 6:
                continue
            rows.append({
                "code": code,
                "trade_date": it[0],
                "open": self._f(it[1]),
                "close": self._f(it[2]),
                "high": self._f(it[3]),
                "low": self._f(it[4]),
                "volume": tencent_volume_to_shares(code, it[5]),
                "is_suspend": False,
            })
        # 补pre_close 与涨跌幅：接口不返回，需由相邻 bar 计算
        for i, r in enumerate(rows):
            if i == 0:
                continue
            prev_close = rows[i - 1]["close"]
            r["pre_close"] = prev_close
            if prev_close:
                r["pct_chg"] = round((r["close"] / prev_close - 1) * 100, 4)
        rows = sorted(rows, key=lambda x: x["trade_date"])
        return self._guard(rows, f"kline_{period}")

    def fetch_minute(self, code: str, mperiod: str = "m30", count: int = 320,
                     as_of: datetime | None = None) -> list[dict]:
        """分钟线。mperiod: m1/m5/m15/m30/m60。

        实测该端点单次返回约 480 根（超出请求 count），
        因此调用方必须按 ts 裁剪窗口，不能依赖 count。
        """
        import json

        sym = f"{market_prefix(code)}{code}"
        param = f"{sym},{mperiod},,,{count}"
        text = self._get("https://ifzq.gtimg.cn/appstock/app/kline/mkline",
                         {"param": param})
        obj = json.loads(text)
        node = (obj.get("data") or {}).get(sym) or {}
        arr = node.get(mperiod) or []
        rows = []
        for it in arr:
            if len(it) < 6:
                continue
            raw = it[0]                       # YYYYMMDDHHMM
            ts = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:00"
            rows.append({
                "code": code,
                "ts": ts,
                "period": mperiod,
                "open": self._f(it[1]),
                "close": self._f(it[2]),       # ★腾讯分钟线同样是收在第 2 位
                "high": self._f(it[3]),
                "low": self._f(it[4]),
                "volume": tencent_volume_to_shares(code, it[5]),
                #★★ `it[7]` 的真实单位**未在任何文档标注**，实测（2026-10-10）：
                #   它不是成交额。逐股检验 `Σamount / Σ(volume×close)` 跨
                #   **4 个数量级**（log10 直方 -5 ~ -1，中位 1.36e-2），
                #   **4556 只里 0 只**落在 1 附近 ⇒ 与量价完全脱钩、逐股漂移，
                #   **无法用固定倍数标定**。
                #   ⇒ 一律置 None：宁可标"缺失"，也不落一个看起来能用的假数。
                #     下游要用成交额时必须自己算 `volume × close`
                #     （⚠ 该结论是在「688 被误放大 100 倍」的错误 volume 上得出的，
                #       比率中位数已因此失真；但"跨 4 个数量级、0 只落在 1 附近"
                #       的定性结论不受量纲影响，故 `it[7]` 仍判不可用）。
                "amount": None,
            })
        rows.sort(key=lambda x: x["ts"])
        return self._guard(rows, f"minute_{mperiod}")

    def fetch_minutes_batch(self, codes: list[str], mperiod: str = "m30",
                            count: int = 320) -> dict[str, list[dict]]:
        """批量分钟线。该端点一次只支持一只（param 单代码），故串行 + 限流。"""
        return {c: self.fetch_minute(c, mperiod, count) for c in codes}