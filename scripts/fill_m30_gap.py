# -*- coding: utf-8 -*-
"""补齐 m30覆盖缺口（2026-10-10）

★ 背景：`R_PREOPEN_VOL_1T`（11 点前全市场成交额）需要全市场 m30，
  但 `sync_minutes --all` 的选股口径是
  `float_mv BETWEEN 2e9 AND 2e11`（流通市值 20亿~2000亿）
  ⇒ **大盘股（>2000亿）与北交所 920 全被排除**，
  缺口稳定在 **1001 只 / 14% 成交额**（2026-09-29 实测）。
  ⚠ 缺口里成交最大的偏偏是 300308 中际旭创(130亿)、688825 长鑫(83亿)
    —— 因为它们流通市值超2000 亿 ⇒ **缺口不是随机噪声，是系统性偏向大票**。

★ 腾讯实测（2026-10-10）：
  - 601288/601398 等大盘股：count=720 → 482 根/ 61 天（2026-07-09~10-09）✅
  - **920001（北交所）：count=320/720 均返回 0 根** ⇒ 腾讯不覆盖北交所
    ⇒ 该部分**如实留缺口，不外推、不用别源冒充腾讯口径**。

★ 本脚本只做一件事：把「日线有、m30 无」的代码补进来，
  并**如实记录哪些补到了、哪些源本身没有**。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PERIOD = "m30"
COUNT = 720              # ★ 实测 320/720 都返回 482 根，720 是上限
PAUSE = 0.35             # 与 sync_minutes 一致，别把qps 打爆


def find_gap(con, ref_date: str) -> list[str]:
    """日线有、m30 无的代码。取**最新完整交易日**做参照。"""
    rows = con.execute(f"""
        select d.code
        from kline_daily d
        where d.trade_date = ?
          and d.code not in (select distinct code from kline_min
                             where period = '{PERIOD}')
        order by d.code""", [ref_date]).fetchall()
    return [r[0] for r in rows]


def main() -> None:
    import httpx
    from backend.data import duck
    from backend.data.adapter import bootstrap, get_adapter

    #★★ DuckDB **单写者**：同进程内 `read_only=True` 与读写连接**不能共存**
    #   （实测：`Can't open a connection to same database file with a
    #     different configuration`）。
    #   本脚本第一版先 get_ro_conn() 再 upsert ⇒ 996 只全部失败，
    #   而同一只代码**单独跑却成功** —— 差的就是前面那次读。
    #   ⇒ 直接用写连接（单连接本身支持读写，无需另开读连接）。
    con = duck.get_conn()
    ref = con.execute("""
        select max(cast(trade_date as date)) from kline_daily
        where cast(trade_date as date) < (select max(cast(trade_date as date))
                                         from kline_daily)""").fetchone()[0]
    print("=" * 72)
    print("补齐 m30 覆盖缺口")
    print("=" * 72)
    print(f"参照日: {ref}")

    # ★★ 补齐只用写连接；`del con` 这类解引用**没用**——
    #   get_ro_conn() 返回的是模块级全局 `_ro_conn`，模块仍持有 ⇒
    #   写连接永远开不出来（这正是第一版失败的原因，教训记在 duck.py）。
    gap = find_gap(con, str(ref))
    cov_before = con.execute(f"""
        select count(distinct code) from kline_min
        where period='{PERIOD}' and cast(ts as date) = ?""",
        [str(ref)]).fetchone()[0]
    print(f"待补: {len(gap)} 只｜当前 m30 覆盖 {cov_before} 只")

    # ★ 必须清掉沙箱代理：它会把外网请求导向代理，
    #   得到「腾讯没数据」的假结论 ⇒ 那是网络问题，不是源的能力问题。
    #   （记忆铁律：判断「源没数据」必须 `trust_env=False` / unset 代理）
    import os
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)

    bootstrap()
    tx = get_adapter("tencent")
    t0 = time.monotonic()
    ok, empty, failed = [], [], []
    for i, code in enumerate(gap, 1):
        try:
            bars = tx.fetch_minute(code, PERIOD, COUNT)
            if not bars:
                empty.append(code)          # ★ 源本身没有，如北交所 920
            else:
                duck.upsert_kline_min(bars)
                ok.append({"code": code, "bars": len(bars)})
        except Exception as exc:
            failed.append({"code": code, "err": f"{type(exc).__name__} {str(exc)[:60]}"})
        if i % 100 == 0:
            el = time.monotonic() - t0
            print(f"  [{i}/{len(gap)}] 成功 {len(ok)} 空 {len(empty)} "
                  f"失败 {len(failed)} | {i / el:.2f} 只/s")
            time.sleep(PAUSE)
        elif i % 20 == 0:
            time.sleep(PAUSE * 0.4)

    el = time.monotonic() - t0
    print(f"\n完成：成功 {len(ok)} / 源无数据 {len(empty)} / 失败 {len(failed)}"
          f"，耗时 {el / 60:.1f} 分钟")

    # ---- 复验：重新查库，不看上面打印 ----
    print("\n【复验】")
    # ★ 继续复用写连接做复验（同进程不能再开只读连接）
    still = set(find_gap(con, str(ref)))
    still_after = sorted(still)
    cov = con.execute(f"""
        select count(distinct code) from kline_min where period='{PERIOD}'
          and cast(ts as date) = '{ref}'""").fetchone()[0]
    tot = con.execute(f"select count(distinct code) from kline_daily "
                      f"where trade_date = '{ref}'").fetchone()[0]
    print(f"  {ref} 日线 {tot} 只 / m30 {cov} 只 ⇒ 覆盖率 {cov / tot:.2%}"
          f"（缺口 {len(still_after)} 只）")
    if still_after:
        by = {}
        for c in still_after:
            by.setdefault(c[:3], []).append(c)
        print("  仍缺按前缀: " + ", ".join(
            f"{k}={len(v)}" for k, v in sorted(by.items(), key=lambda x: -len(x[1]))))
    amt = con.execute(f"""
        select sum(d.volume*d.close)/1e8 from kline_daily d
        where trade_date='{ref}' and d.code not in
              (select distinct code from kline_min where period='{PERIOD}')
    """).fetchone()[0]
    full = con.execute(f"select sum(volume*close)/1e8 from kline_daily "
                       f"where trade_date='{ref}'").fetchone()[0]
    print(f"  ★ 剩余缺口占全市场成交额 {amt / full:.2%}（补齐前 14.00%）")

    p = ROOT / "data" / "m30_gap_fill.json"
    p.write_text(json.dumps({
        "ref_date": str(ref), "period": PERIOD, "count": COUNT,
        "n_gap_before": len(gap),
        "filled": ok, "no_data_in_source": empty, "failed": failed,
        "coverage_after": cov / tot, "gap_after": still_after,
        "gap_share_of_amount_after": amt / full,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()