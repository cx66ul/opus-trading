# -*- coding: utf-8 -*-
"""
Hawk · 上市日期推导（决策 D11）

背景：
    提示词要求推荐模块做「上市 > 60 日」硬性过滤，
    但新浪/腾讯的行情接口都不返回 list_date。

方案：长历史首根 K 线日期反推。
    实测（2026-10-08）：
      ✓ 新浪 datalen=6000 可回溯到 2001 年
      ✗ 腾讯 fqkline 的 count 参数**上限约 250**，传 8000 直接返回 0 根
        （实测踩坑：腾讯不是「给多少给多少」，超出范围会返回空）

    可靠性验证（对照公开上市日）：
      宁德时代 2018-06-11 → 精确命中
      中国平安 2007-03-01 → 精确命中
      贵州茅台 2001-08-27 → 首根 2001-09-18，差 22 天
      五粮液   1998-04-27 → 首根 2001-06-27，差 3.2 年
      平安银行 1991-04-03 → 首根 2001-05-21，差 10.1 年

    ★ 结论：首根日期是「上市日**下界**」—— 只会偏晚，不会偏早。
      因为老股的历史数据可能不完整，接口从某个时点才开始提供。
    这个性质恰好适合「上市 > 60 日」判断：
      若首根日期 + 60 天 <= 今天 → 该股必然已上市超过 60 天 → **判定可靠**
      若首根日期 + 60 天 >  今天 → 实际可能已上市，需保守处理

    因此本模块返回 `list_date_lower_bound`（下界）而非假装精确的 list_date，
    下游必须按「下界」语义使用，不可当作精确上市日展示给用户。
"""
from __future__ import annotations

from datetime import date, timedelta

from loguru import logger

# 新浪 datalen 上限探测结果
SINA_MAX_DATALEN = 6000
TENCENT_MAX_COUNT = 250        # ★ 实测：腾讯超过此值直接返回空，不是截断


def list_date_lower_bound(first_bar_date: str | None) -> str | None:
    """首根 K 线日期 -> 上市日期下界。

    若首根日期距今已超过 60 个自然日，则该股上市时间可确定为超过 60 天。
    """
    if not first_bar_date:
        return None
    try:
        return str(date.fromisoformat(first_bar_date[:10]))
    except ValueError:
        return None


def listed_days_ok(list_date_lb: str | None, as_of: date,
                   min_days: int = 60) -> tuple[bool, str]:
    """判断是否满足「上市 > min_days 日」。

    返回 (是否通过, 说明)。**保守策略**：无法判定时一律不通过，
    宁可漏掉次新股的推荐机会，也不能把次新股当成熟标的推出去
    —— 后者会直接导致追高风险。

    语义：list_date_lb 是下界，所以
        as_of - list_date_lb > min_days  =>  必然满足（真）
        否则不确定 => 判否
    """
    if not list_date_lb:
        return False, "无上市日期数据，按未满足处理"
    try:
        d = date.fromisoformat(list_date_lb[:10])
    except ValueError:
        return False, f"上市日期格式非法: {list_date_lb}"
    gap = (as_of - d).days
    if gap > min_days:
        return True, f"已上市 {gap} 天（下界推算）"
    return False, f"上市日期下界距今仅 {gap} 天，≤ {min_days} 天，按次新股处理"


def probe_sina_history(code: str, client, prefix: str | None = None) -> dict:
    """用新浪长历史反推上市日期下界。code 为 6 位。"""
    from backend.data.adapters.tencent import market_prefix

    sym = f"{prefix or market_prefix(code)}{code}"
    r = client.get(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        params={"symbol": sym, "scale": 240, "ma": "no", "datalen": SINA_MAX_DATALEN},
        headers={"Referer": "https://finance.sina.com.cn"},
    )
    rows = r.json()
    if not rows:
        return {"code": code, "list_date_lb": None, "bars": 0,
                "note": "新浪无历史数据（新股或该源未覆盖）"}
    first = rows[0]["day"]
    return {
        "code": code,
        "list_date_lb": list_date_lower_bound(first),
        "bars": len(rows),
        "first_bar": first,
        "last_bar": rows[-1]["day"],
        "note": "首根日期为上市日下界，老股可能偏晚",
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    import httpx

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    client = httpx.Client(timeout=30, follow_redirects=True, trust_env=False,
                          headers={"User-Agent": UA})

    as_of = date(2026, 10, 8)
    samples = ["600519", "300750", "000001", "601318", "000858", "688981", "002058"]
    print(f"{'代码':8s}{'首根日期':>12s}{'根数':>7s}{'>60日?':>9s} 说明")
    for code in samples:
        info = probe_sina_history(code, client)
        lb = info["list_date_lb"]
        ok, why = listed_days_ok(lb, as_of, 60)
        print(f"{code:8s}{(lb or '—'):>12s}{info['bars']:>7d}{'通过' if ok else '不通过':>9s} {why}")

    print()
    print("=== 边界：刚上市的新股 ===")
    for d in [date(2026, 9, 25), date(2026, 8, 1)]:
        lb = str(d)
        ok, why = listed_days_ok(lb, as_of, 60)
        print(f"  上市日下界 {lb} -> {'通过' if ok else '不通过'}（{why}）")

    print()
    print("=== 数据缺失时必须保守 ===")
    for lb in [None, "非法日期"]:
        ok, why = listed_days_ok(lb, as_of, 60)
        print(f"  {str(lb):10s} -> {'通过' if ok else '不通过'}（{why}）")