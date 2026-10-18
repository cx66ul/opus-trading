#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 真实指数日线抓取（上证 / 深证 / 创业板 / 沪深300）

★★★ 为什么要专门做这件事
--------------------------------------------------
`backfill_regime.py` 原先用 `breadth.avg_chg`
（**全市场个股等权均值**）当 `index_pct` 的代理，
并在 `detail_json` 里诚实标注 `index_pct_source="equal_weight_avg"`。

标注做得对，但**代价值得实测**（2026-10-11 实测 30 个交易日）：

    日期          等权均值%    上证%     差(pp)
    2026-09-09     −0.469     +0.278    +0.747
    2026-09-10     −1.355     −0.433    +0.922
    2026-09-11     −1.999     −1.177    +0.823
    2026-09-16     +1.418     +0.707    −0.711
    2026-09-21     +1.919     +0.972    −0.946
    2026-09-28     −2.241     −1.665    +0.576
    2026-10-09     +0.608     +0.050    −0.558

⇒ **符号经常相反**，平均绝对差 0.5pp、最大 2.0pp。
根因是等权 vs 市值加权：小盘股主导时等权均值远大于指数。
⇒ 市场状态机的「大盘涨跌」若用等权代理，**regime 会判反**，
  而且**不报任何错**（数值都「正常」，只是口径不对）。

⇒ 本脚本按语料口径（2026-10-11 用户确认：**大盘 = 上证指数**）
  落真实指数行情到 `index_daily`。

⚠ 口径纪律
  · `amount` 源不给就留 NULL，**绝不用 volume×close 自算**
    （个股那边自算是因为库里 amount 全空；指数这边不适用，
     源口径不同会引入第二种误差）
  · 只落**交易日**（新浪返回的本身就是交易日序列）
  · 幂等：`INSERT OR REPLACE` 按主键去重，重跑安全

用法：
    python scripts/fetch_index_daily.py --probe
    python scripts/fetch_index_daily.py --all
    python scripts/fetch_index_daily.py --all --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data import duck                # noqa: E402

logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | {message}")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 语料口径（2026-10-11 用户确认）：**大盘 = 上证指数**
INDEXES = {
    "sh000001": "上证指数",       # ★ 语料里的「大盘」
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
}


def fetch(symbol: str, datalen: int = 4000,
          tries: int = 3) -> list[dict]:
    """从新浪取指数日线。`scale=240` 即日线（分钟线为 5/15/30/60）。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}"
           f"&scale=240&ma=no&datalen={datalen}")
    last = None
    for t in range(tries):
        try:
            with httpx.Client(trust_env=False, timeout=25,
                              headers={"User-Agent": UA}) as c:
                r = c.get(url)
            data = r.json()
            if not isinstance(data, list) or not data:
                raise ValueError(f"返回非列表或空：{str(data)[:120]}")
            return data
        except Exception as e:                     # noqa: BLE001
            last = e
            if t < tries - 1:
                time.sleep(1.0 * (t + 1))
    raise RuntimeError(f"{symbol} 取不到：{type(last).__name__}: {last}")


def to_rows(symbol: str, data: list[dict]) -> list[tuple]:
    out = []
    for d in data:
        try:
            out.append((symbol, d["day"], float(d["open"]), float(d["high"]),
                        float(d["low"]), float(d["close"]),
                        int(float(d.get("volume") or 0)),
                        #★ amount 源不给 ⇒ None。**不自算**。
                        float(d["amount"]) if d.get("amount") else None))
        except (KeyError, TypeError, ValueError):
            logger.warning(f"{symbol} 跳过畸形行：{d}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="指数日线抓取")
    p.add_argument("--index", action="append", choices=sorted(INDEXES))
    p.add_argument("--all", action="store_true")
    p.add_argument("--probe", action="store_true", help="只看可达性")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--datalen", type=int, default=4000)
    a = p.parse_args()

    keys = sorted(set(a.index or (list(INDEXES) if a.all else [])))
    if a.probe:
        keys = list(INDEXES)
    if not keys:
        p.error("给 --index / --all / --probe")

    if a.probe:
        rc = 0
        for k in keys:
            try:
                d = fetch(k, 5)
                logger.info(f"{k}({INDEXES[k]}) 可达，返回 {len(d)} 根 "
                            f"最新 {d[-1]['day']} close={d[-1]['close']}")
            except Exception as e:                  # noqa: BLE001
                logger.error(f"{k}({INDEXES[k]}) 不可达：{e}")
                rc = 1
        return rc

    con = duck.get_conn()
    #★ 幂等脚本必须先确保表存在：`DDL` 里的 `CREATE TABLE IF NOT EXISTS`
    #   只在 `init_db()` 里执行，直接跑本脚本会
    #   `Catalog Error: Table with name index_daily does not exist!`
    duck.init_db()
    con = duck.get_conn()
    summary = []
    for k in keys:
        try:
            rows = to_rows(k, fetch(k, a.datalen))
        except Exception as e:                      # noqa: BLE001
            #★ 分指数独立降级：一个失败不影响其它
            logger.error(f"{k}({INDEXES[k]}) 失败，跳过：{e}")
            summary.append((k, INDEXES[k], 0, "failed"))
            continue
        if not rows:
            logger.error(f"{k} 无数据，不写库")
            summary.append((k, INDEXES[k], 0, "empty"))
            continue
        days = [r[1] for r in rows]
        if a.dry_run:
            logger.info(f"[dry] {k}({INDEXES[k]}) {len(rows)} 根 "
                        f"{days[0]}~{days[-1]} 收{days[-1]}:{rows[-1][5]}")
            summary.append((k, INDEXES[k], len(rows), "dry"))
            continue
        # ★ 幂等：按主键替换，重跑不会翻倍
        con.executemany(
            "INSERT OR REPLACE INTO index_daily "
            "(index_code, trade_date, open, high, low, close, volume, amount)"
            " VALUES (?,?,?,?,?,?,?,?)", rows)
        logger.info(f"{k}({INDEXES[k]}) 写入 {len(rows)} 根 "
                    f"{days[0]}~{days[-1]}")
        summary.append((k, INDEXES[k], len(rows), "ok"))

    print("\n=== 指数日线汇总 ===")
    for k, name, n, st in summary:
        print(f"  {k:<10}{name:<10}{n:>6}  {st}")
    return 0 if all(s[3] in ("ok", "dry") for s in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
