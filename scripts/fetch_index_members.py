#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 指数成分快照抓取（沪深300 / 上证50 / 创业板指 / 科创50）

★★★ 为什么要单独一个脚本，而不是塞进 `fetch_board_members.py`
------------------------------------------------------
板块成分来自同花顺（受 IP 频率阈值限制），指数成分有**独立且更稳的源**，
两者的失败模式、重试策略、合规频率都不一样 ⇒ 混在一起会互相拖累。

数据源：东方财富 `push2.eastmoney.com/api/qt/clist/get`
    `fs=b:BK0500`（沪深300）、`BK0611`（上证50）… 板块代码对应关系见 `INDEXES`

★★★ 实测坑（2026-10-10）
--------------------------------------------------
1. **分页上限 pz ≤ 50**：实测 `pz=300` 会让服务器
   `RemoteProtocolError: Server disconnected without sending a response`，
   而 `pz=50` 正常。⇒ 固定按 50 分页，不去「优化」成大分页。
2. ★★★ **源是间歇性可达的，绝不能当稳定依赖**（实测 7 次请求 0 次成功）
   ------------------------------------------------------------------
   今天 23:10 左右出现过一组「连续 5 次全部成功、取满 300 只」的结果，
   但随后 6 次探测 **0 次成功**，全为 `RemoteProtocolError`。
   而记忆里长期记着「push2.eastmoney.com 本机不可达」。

   ⇒ **正确结论是「不可靠且当前不可用」，不是「偶尔可用」**：
     一个 20 分钟窗口内 0/6 成功率的源不能进数据管道，
     否则调度器会在凌晨悄悄连抓 7 次全部失败，
     而 `index_member_snapshot` 会一直停在 0 行却没人知道。
   ⇒ 本脚本默认 `--probe` 而不是直接采集；
     **实测通过前不得接入调度**（`index_member_snapshot` 当前无消费方，
     保持 0 行是正确状态，不是待修的 bug）。
   ⇒ 若日后要用，必须先做「连续 N 次成功率」门槛，而不是「试一次能通」。
   ★★ 教训：「偶然成功一次」是最容易造成假依赖的证据形式 ——
      它让「重试一下就好了」和「这条路不通」两种解释都成立。

3. **必须核对 `total`**：`data.total` 是成分总数，
   实测取到 300、去重后仍 300 才算完整。
   若去重后少于 `total`（接口返回重复行），要**报错而不是默默入库**。

⚠️ 合规：单指数 ≤ 7 次请求（300 只 / 每页 50），页间 sleep 0.3s。

用法：
    python scripts/fetch_index_members.py --probe
    python scripts/fetch_index_members.py --index hs300
    python scripts/fetch_index_members.py --all
    python scripts/fetch_index_members.py --all --dry-run
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data import duck                                # noqa: E402
from backend.domain.trade_cal import is_trade_day, prev_trade_day  # noqa: E402

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | {message}")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

#★★★ pz 固定 50：实测 pz=300 会被服务器直接断连
PAGE_SIZE = 50
SLEEP = 0.3
RETRIES = 4

# `fs=b:<东财板块码>`。★ 这是东财的**板块**码，与指数代码 000300 不是一回事，
#   两者混用会取到完全不同的股票集合（实测 BK0500 才是 300 只）。
INDEXES = {
    "hs300": {"board": "BK0500", "code": "000300", "name": "沪深300", "n": 300},
    "sz50": {"board": "BK0611", "code": "000016", "name": "上证50", "n": 50},
    "cyb": {"board": "BK0800", "code": "399006", "name": "创业板指", "n": 100},
    "kc50": {"board": "BK1003", "code": "000688", "name": "科创50", "n": 50},
}


def _fetch_page(board: str, pn: int, tries: int = RETRIES) -> tuple[int, list[dict]]:
    """取一页成分。失败会重试；全部失败才抛错。

    ★★ 为什么不「失败就判定源不可用」：
      实测同一个 URL 第一次成功、隔几秒再试又断连
      ⇒ 单次失败只说明**这一次**没成，不代表源挂了。
      反过来也不能因为「重试成功」就当它稳定
      ⇒ 完整性由调用方核对 `total` 决定，不由重试次数决定。
    """
    url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn={pn}"
           f"&pz={PAGE_SIZE}&po=1&fid=f3&fs=b:{board}&fields=f12,f14")
    last = None
    for t in range(tries):
        try:
            with httpx.Client(trust_env=False, timeout=15,
                              headers={"User-Agent": UA}) as c:
                r = c.get(url)
            data = r.json().get("data") or {}
            diff = data.get("diff") or {}
            items = list(diff.values()) if isinstance(diff, dict) else list(diff)
            return int(data.get("total") or 0), items
        except Exception as e:                     # noqa: BLE001
            last = e
            if t < tries - 1:
                time.sleep(0.8 * (t + 1))            # 退避
    raise RuntimeError(f"{board} 第{pn} 页取不到：{type(last).__name__}: {last}")


def fetch_index_members(board: str, expect: int | None = None
                        ) -> tuple[list[str], int, str]:
    """返回 `(成分代码, 期望数, 完整性说明)`。

    ★★ 完整性必须**核对**而不是假设：
      接口分页边界不稳定（实测第 7 页会抛错而不是返回空），
      若「拿到不足 50 就当取完」，缺页会被当成正常结束。
      ⇒ 循环条件是「累计 < total」，不是「本页为空」。
    """
    total, first = _fetch_page(board, 1)
    if expect and total != expect:
        logger.warning(f"{board}: total={total} 与预期 {expect} 不符，"
                       f"按实际 total 采集")
    names = {str(i["f12"]): str(i.get("f14") or "") for i in first}
    pn = 2
    while len(names) < total:
        try:
            _, items = _fetch_page(board, pn)
        except RuntimeError as e:
            #★ 最后一页取不到时，如实记录「不完整」而**不是静默收尾**
            logger.warning(f"{board}: 第{pn} 页失败（{e}），"
                           f"已收 {len(names)}/{total}")
            return sorted(names), total, f"incomplete:page{pn}_failed"
        if not items:
            return sorted(names), total, f"incomplete:short_by_{total - len(names)}"
        for i in items:
            names.setdefault(str(i["f12"]), str(i.get("f14") or ""))
        pn += 1
        time.sleep(SLEEP)
    return sorted(names), total, "complete"


def main() -> int:
    p = argparse.ArgumentParser(description="指数成分快照抓取")
    p.add_argument("--index", action="append",
                   choices=sorted(INDEXES), help="可重复；缺省=全部")
    p.add_argument("--all", action="store_true")
    p.add_argument("--probe", action="store_true", help="只探测可达性")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--as-of", help="交易日 YYYY-MM-DD（默认归一到最近交易日）")
    a = p.parse_args()

    keys = sorted(set(a.index or (list(INDEXES) if a.all else [])))
    if a.probe:
        keys = list(INDEXES)
    if not keys:
        p.error("给 --index 或 --all（或 --probe）")

    if a.probe:
        for k in keys:
            meta = INDEXES[k]
            try:
                total, items = _fetch_page(meta["board"], 1)
                logger.info(f"{k}({meta['name']}) 可达 total={total} "
                            f"本页 {len(items)} 只 例：{items[:2]}")
            except Exception as e:                  # noqa: BLE001
                logger.error(f"{k}({meta['name']}) 不可达：{e}")
        return 0

    # ---- 采集日必须归一到**交易日**
    #★ 与板块/日线同一铁律：非交易日实时接口返回的是**上一交易日收盘**的静态数据，
    #  直接归一会凭空造出库里不存在的交易日。
    day = a.as_of or datetime.now(CST).strftime("%Y-%m-%d")
    if not is_trade_day(day):
        prev = prev_trade_day(day)
        logger.info(f"{day} 非交易日 → 归一到{prev}")
        if prev is None:
            logger.error("找不到上一个交易日，拒绝写入（宁可无数据）")
            return 1
        day = prev

    con = duck.get_conn()
    summary = []
    for k in keys:
        meta = INDEXES[k]
        try:
            codes, total, state = fetch_index_members(meta["board"], meta["n"])
        except Exception as e:                      # noqa: BLE001
            #★ 分指数独立降级：一个指数失败不影响其它指数落库
            logger.error(f"{k}({meta['name']}) 失败，跳过：{e}")
            summary.append((k, 0, 0, "failed"))
            continue

        # ★★ 去重后少于 total ⇒ 接口给了重复行，**必须报错而不是默默入库**
        if len(codes) != total:
            logger.error(f"{k}({meta['name']}) 去重后 {len(codes)} != total {total}"
                         f"，判定为抓取不完整，不写库")
            summary.append((k, len(codes), total, "mismatch"))
            continue

        if a.dry_run:
            logger.info(f"[dry] {k}({meta['name']}) {len(codes)}/{total} "
                        f"例：{codes[:5]}")
            summary.append((k, len(codes), total, state))
            continue

        # ★★ 幂等增量：先 DELETE 同 (snap_date, index_code) 再插，
        #   但**只在本次抓取完整时**才做 —— 半截数据会把上次的完整快照抹掉。
        #   （这正是同花顺板块脚本踩过的坑：限流中断 + 先删 ⇒ 快照越来越空。）
        con.execute("DELETE FROM index_member_snapshot "
                    "WHERE snap_date = ? AND index_code = ?",
                    [day, meta["code"]])
        con.executemany(
            "INSERT INTO index_member_snapshot (snap_date, index_code, code) "
            "VALUES (?,?,?)",
            [(day, meta["code"], c) for c in codes])
        logger.info(f"{k}({meta['name']}) {day}: {len(codes)}/{total} 已写入")
        summary.append((k, len(codes), total, state))

    print("\n=== 指数成分快照汇总 ===")
    for k, got, tot, state in summary:
        print(f"  {k:<8} {got:>4}/{tot:<4} {state}")
    if a.dry_run:
        return 0
    return 0 if all(s[3] == "complete" for s in summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
