# -*- coding: utf-8 -*-
"""修复科创板（688）volume 被放大 100 倍的历史数据（2026-10-10）

★ 为什么要修：
  腾讯接口对 688 返回的成交量单位是**股**，对其余板块是**手**，
  采集器曾无条件 `×100` ⇒ 688 全部volume 放大 100 倍。
  后果（2026-09-29 实测）：
    - 全市场单日成交额算成 **21.8 万亿**，真实约**1.4 万亿**
    - 688 板块 616 只独占 94.4% 的"成交额"
    - `market_regime.total_amount` 同步失真（它就是拿这份 volume 算的）

★ 为什么之前没发现：
  m30 累计/日线 volume 的**比值**对 688 也是 1.0（两边同样放大 100 倍）
  ⇒ **比值自洽会掩盖共同的单位错误**。只有绝对量级 + 跨源交叉才暴露。

★ 修法：只改 688 打头的行，`volume = volume / 100`。
  ⚠ 必须先备份（见 backup_table），且**只跑一次**（幂等标记）。
  ⚠ 历史 bar 是「当时的成交量」，除以 100 是纯单位换算，不改变任何相对关系
    ⇒ 涨跌幅、量比类的相对指标不受影响；但**成交额类绝对指标会变**。

★★★★★ 2026-10-10 第二轮更正：**本脚本的判据「按代码前缀」是错的**，
  且它已经造成了 3 只股票的**过度修复**（详见 `fix_kline_volume_overfix.py`）。

  单位正确与否取决于「**这一行是用哪个版本的采集器写入的**」，
  而**不是代码前缀**。688806 / 688835 / 688837 是新股
  （首日 2026-07-21/ 08-25 / 09-16），它们的日线是在采集器修好之后同步的
  ⇒ 本来就已是「股」（正确），却被本脚本又除了 100。

  ⇒ **本脚本现已默认拒绝执行**（见 `_assert_not_already_applied`）。
    已被修好的那3 只由 `fix_kline_volume_overfix.py` 处理。
    数据若需回到本脚本执行前的状态：用 `data/backup/kline_*_688_prefix_20261010.parquet`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TABLES = ("kline_daily", "kline_min")
BACKUP_DIR = ROOT / "data" / "backup"


def assert_not_already_applied(con) -> bool:
    """★ 数据已经修好了 ⇒ **默认拒绝再跑**。

    为什么需要它（这是本轮真实踩的坑）：
      本脚本的 `update ... set volume = volume/100` **不是幂等的**。
      我先跑了它，随后发现 3 只新股被过度修复（它们本来已是「股」），
      又补跑了 `fix_kline_volume_overfix.py` 把那 3 只 ×100 回来。
      此时若有人（我）再次执行本脚本 ⇒那 3 只会被**再除 100**（变回错值），
      而其余 613 只又会被再除一次 ⇒ 全库重新回到21.8 万亿的错误状态，
      **且脚本本身不会报错**（这才是最危险的地方）。

    ⇒ 因此本脚本现在**必须先通过前置校验**才允许改数据。
      判据：688 板块里已存在「日线 volume ≈ m30 Σvolume」的样本
      ⇒ 说明单位修复已生效 ⇒ 拒绝。
      （688806/688835/688837 三只新股是例外，它们在修复前就与 m30 对齐，
        所以判据只看**非新股子集**即可成立，毋需维护名单。）
    """
    n_aligned = con.execute("""
        WITH d AS (SELECT code, volume sv FROM kline_daily
                   WHERE trade_date = DATE '2026-10-08' AND substr(code,1,3) = '688'),
             m AS (SELECT code, sum(volume) mv FROM kline_min
                   WHERE period = 'm30' AND cast(ts as date) = DATE '2026-10-08'
                   GROUP BY code)
        SELECT count(*) FROM d JOIN m USING(code)
        WHERE d.sv / m.mv BETWEEN 0.9 AND 1.1""").fetchone()[0]
    if n_aligned > 100:      # 阈值：修好后应有数百只对齐
        return False
    return True


def survey(con) -> dict:
    """先看清影响面，再动手。"""
    out = {}
    for t in TABLES:
        date_col = "trade_date" if t == "kline_daily" else "ts"
        r = con.execute(f"""
            select count(*) as nrows,
                   count(distinct code) as ncodes,
                   sum(volume)/1e8 as vol_yi
            from {t} where substr(code,1,3)='688'""").fetchone()
        out[t] = {"rows_688": r[0], "codes_688": r[1], "vol_yi": r[2]}
        d = con.execute(f"""
            select cast({date_col} as date) as d, count(*) as n
            from {t} where substr(code,1,3)='688'
            group by 1 order by 1 desc limit 5""").fetchall()
        out[t]["recent_days"] = [{"date": str(a), "rows": b} for a, b in d]
    # 全库总览（用于对照）
    for t in TABLES:
        date_col = "trade_date" if t == "kline_daily" else "ts"
        r = con.execute(f"""
            select sum(volume*close)/1e8 as amt_yi, count(*) as n
            from {t}""").fetchone()
        out[f"{t}_ALL"] = {"amt_yi": r[0], "rows": r[1]}
    return out


def backup_table(con) -> Path:
    """用 DuckDB 的EXPORT DATABASE 思路不适用（整库太大）——
    这里只备份受影响的 688 行到 parquet，足够回滚。"""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = "20261010"
    for t in TABLES:
        p = BACKUP_DIR / f"{t}_688_prefix_{stamp}.parquet"
        con.execute(
            f"copy (select * from {t} where substr(code,1,3)='688') "
            f"to '{p.as_posix()}' (format parquet)")
        print(f"  备份 {t} -> {p.name}")
    return BACKUP_DIR


def main() -> None:
    from backend.data import duck
    con = duck.get_conn()          # 需要写锁

    print("=" * 72)
    print("修复 688 科创板 volume 单位（放大 100 倍）")
    print("=" * 72)

    # ---- 幂等守卫：必须前置 ----
    if not assert_not_already_applied(con):
        raise SystemExit(
            "拒绝执行：库内 688 的日线 volume 已与 m30 对齐（比值 ≈1.0）"
            "⇒ 单位修复早已生效，再跑会**再除一次 100**，"
            "把全库打回21.8 万亿的错误状态且不会报错。\n"
            "  · 已修好的 3 只新股由 scripts/fix_kline_volume_overfix.py 处理\n"
            "  · 需要复核请跑：python scripts/probe_688_anomaly.py\n"
            "  · 需要回到执行前状态：用 data/backup/kline_*_688_prefix_20261010.parquet")
    print("\n【幂等守卫】通过（未检测到已修复痕迹）")

    before = survey(con)
    print("\n【修复前】")
    print(json.dumps(before, ensure_ascii=False, indent=1))

    print("\n【备份】")
    backup_table(con)

    print("\n【执行】")
    for t in TABLES:
        n = con.execute(
            f"update {t} set volume = volume/100.0 "
            f"where substr(code,1,3)='688'").fetchone()
        con.execute(f"delete from {t} where substr(code,1,3)='688' "
                    f"and volume < 0")
        print(f"  {t}:已更新（返回 {n}）")

    after = survey(con)
    print("\n【修复后】")
    print(json.dumps(after, ensure_ascii=False, indent=1))

    # ---- 独立复验：不看上面打印，直接重新查库 ----
    print("\n【复验】")
    D = "2026-09-29"
    amt = con.execute(f"""
        select sum(volume*close)/1e8 from kline_daily
        where trade_date='{D}'""").fetchone()[0]
    amt688 = con.execute(f"""
        select sum(volume*close)/1e8 from kline_daily
        where trade_date='{D}' and substr(code,1,3)='688'""").fetchone()[0]
    print(f"  {D} 全市场成交额 = {amt:.0f} 亿（应为万亿级，非 10 万亿级）")
    print(f"  {D} 688板块 = {amt688:.0f} 亿，占 {amt688 / amt:.1%}（应 <30%）")
    top = con.execute(f"""
        select code, volume, close, volume*close/1e8 as yi
        from kline_daily where trade_date='{D}'
        order by volume*close desc limit 3""").fetchall()
    for c, v, cl, y in top:
        print(f"  单只最大: {c} volume={v:,.0f} 股 成交额={y:.1f} 亿")

    p = ROOT / "data" / "kline_volume_unit_fix.json"
    p.write_text(json.dumps({"before": before, "after": after,
                             "verified_amount_yi_20260929": amt},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()