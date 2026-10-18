# -*- coding: utf-8 -*-
"""修复 `fix_kline_volume_unit.py` 的**过度修复**（2026-10-10· 第二轮）

★★ 背景：这是同一个错误的**第二段**，方向与第一段相反。

  第一轮（`fix_kline_volume_unit.py`）发现 688 科创板 volume 被放大 100 倍，
  按 `substr(code,1,3)='688'` 无条件 `÷100` —— **那个判据本身是错的**。

  为什么错：**单位正确与否取决于「这行是什么时刻、用哪个版本的采集器写入的」，
  而不是代码前缀。**
  这 3 只（688806 / 688835 / 688837）是**新股**：
    688837 首日 2026-09-16、688835 首日 2026-08-25、688806 首日 2026-07-21
  它们的日线是在采集器修好（`tencent_volume_to_shares()` 上线）之后同步的
  ⇒ **本来就已经是「股」，是正确的**，被第一轮脚本又除了 100。

★ 证据链（全部跨源实测，不靠推测）：
  ①腾讯/新浪实测：688837 在 2026-10-08 的 volume = 14,353,149（比值 1.00000，股）
  ② 第一轮**修复前的备份 parquet** 里，该值 = 14,353,149 ⇒ 修复前就是对的
  ③ 现库日线 = 143,531 = 14,353,149 ÷ 100 ⇒ 被多除了
  ④ 抽样 12 只其他 688：备份日线 ÷ 腾讯实测 = **100.000（12/12）**
     ⇒ 其余 688 确实需要 ÷100，第一轮的判据对它们是对的
  ⑤ 全库 5207 只逐股 `日线volume / m30Σvolume` 比值，偏离 0.5~2 的**只有这 3 只**
  ⑥ 抽样 10 只 688 的现库 m30 vs 腾讯实测：10/10 = 1.000 ⇒ **m30 侧全对，不要动**

⇒ **修法**：只把 `kline_daily` 里这 3 只的 volume `×100` 回来。
  `kline_min` **不动**（m30 侧本来就是对的）。

★ 教训（写进记忆，务必记住）：
  **批量单位换算不能只按代码前缀 —— 必须按「写入时点/来源版本」判定。**
  前缀只能说明「这个板块的源单位是手」，不能说明「这一行是否已经被换算过」。
  正确判据是**跨源对照**（腾讯 vs 新浪），且要覆盖「曾被修复过的行」这类子集。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#★ 被过度修复的代码清单。**实测得来，不是推断**：
#   修复前备份 = 腾讯实测值（正确） → 现库 = 备份 ÷ 100（被多除了）。
# 若日后发现新的同类个股，必须重跑 scripts/probe_688_anomaly.py 走一遍
#   「腾讯实测 → 备份值 → 现库值」三段对照再加入，**不要凭前缀补**。
OVER_FIXED = ("688806", "688835", "688837")

BACKUP_DIR = ROOT / "data" / "backup"
TABLES = ("kline_daily",)          #★ kline_min 不在其中（m30 侧本来就正确）
PROBE_DATE = "2026-10-08"


def code_clause(codes: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{c}'" for c in codes) + ")"


def verify_only(con) -> dict:
    """只做复验，不改数据。

    UPDATE 是一次性的（×100 不可逆），所以复验必须能独立运行：
    重跑整脚本会被幂等守卫拦下，但**报告仍需要能重新生成**。
    """
    print("\n【复验】逐股 日线volume / m30Σvolume（2026-10-08，应为 1.0000）")
    for c in OVER_FIXED:
        dv = con.execute(
            f"select volume, close from kline_daily where code = '{c}' "
            f"and trade_date = DATE '{PROBE_DATE}'").fetchone()
        mv = con.execute(
            f"select sum(volume) from kline_min where code = '{c}' and period = 'm30' "
            f"and cast(ts as date) = DATE '{PROBE_DATE}'").fetchone()[0]
        ratio = dv[0] / mv if mv else float("nan")
        print(f"  {c}  日线={dv[0]:>14,.0f}  m30={mv:>14,.0f}  "
              f"比值={ratio:.4f}  成交额={dv[0] * dv[1] / 1e8:.2f} 亿")

    print("\n【复验】全库逐股不变式偏离只数（应为 0）")
    n_dev = con.execute("""
        WITH d AS (SELECT code, sum(volume) sv FROM kline_daily
                   WHERE trade_date = DATE '2026-10-08' GROUP BY code),
             m AS (SELECT code, sum(volume) mv FROM kline_min
                   WHERE period = 'm30' AND cast(ts as date) = DATE '2026-10-08'
                   GROUP BY code)
        SELECT count(*) FROM d JOIN m USING(code)
        WHERE d.sv / m.mv < 0.5 OR d.sv / m.mv > 2""").fetchone()[0]
    print(f"  偏离只数 = {n_dev}")

    print("\n【复验】全市场成交额量级（2026-10-08，应为万亿级）")
    amt = con.execute("select sum(volume * close) / 1e8 from kline_daily "
                      f"where trade_date = DATE '{PROBE_DATE}'").fetchone()[0]
    amt688 = con.execute("select sum(volume * close) / 1e8 from kline_daily "
                         f"where trade_date = DATE '{PROBE_DATE}' "
                         "and substr(code,1,3) = '688'").fetchone()[0]
    print(f"  全市场 = {amt:,.0f} 亿   688板块 = {amt688:,.0f} 亿"
          f"（占 {amt688 / amt:.1%}）")
    return {"over_fixed_codes": list(OVER_FIXED), "tables": list(TABLES),
            "probe_date": PROBE_DATE,
            "verified_deviating_codes_after_fix": n_dev,
            "verified_total_amount_yi": amt,
            "verified_688_amount_yi": amt688}


def main() -> None:
    from backend.data import duck
    con = duck.get_conn()

    print("=" * 74)
    print("修复第一轮脚本的过度修复（688 新股日线被多除 100）")
    print("=" * 74)

    if "--verify-only" in sys.argv:
        out = verify_only(con)
        p = ROOT / "data" / "kline_volume_overfix.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n→ {p}")
        return

    # ---- 修前：独立复验用的现状快照 ----
    print("\n【修前】")
    for t in TABLES:
        rows = con.execute(
            f"select code, volume from {t} where code in {code_clause(OVER_FIXED)} "
            f"and trade_date = DATE '{PROBE_DATE}' order by code").fetchall()
        for c, v in rows:
            print(f"  {t} {c} {PROBE_DATE} volume = {v:,.0f}")

    # ---- 幂等守卫：UPDATE 是 ×100，重复执行会把数据再放大 100 倍（不可逆）----
    # 判据：这 3 只的日线 volume 应已等于 m30 Σvolume（比值 ≈1.0000）。
    # 若已经相等 ⇒ 说明本脚本已执行过，**必须拒绝**而不是再乘一次。
    # ★ 这条守卫本身也说明了另一个教训：**非幂等的批量换算必须自带前置校验**，
    #   否则「多跑一次」就等于造一个新错误（第一轮脚本就栽在这里）。
    print("\n【幂等守卫】")
    already = True
    for c in OVER_FIXED:
        dv = con.execute(
            f"select volume from kline_daily where code = '{c}' "
            f"and trade_date = DATE '{PROBE_DATE}'").fetchone()
        mv = con.execute(
            f"select sum(volume) from kline_min where code = '{c}' and period = 'm30' "
            f"and cast(ts as date) = DATE '{PROBE_DATE}'").fetchone()[0]
        if not (dv and mv and 0.9 < dv[0] / mv < 1.1):
            already = False
    if already:
        raise SystemExit(
            f"拒绝执行：这 {len(OVER_FIXED)} 只的日线 volume 已与 m30 对齐"
            "（比值 ≈1.0）⇒ 本脚本已跑过，再跑会把数据再 ×100。"
            " 如需重来，请先用 data/backup/kline_daily_overfix_*.parquet 回滚。")
    print("  通过（当前日线/ m30 比值均 ≈0.01，说明确实需要 ×100）")

    # ---- 备份（可回滚）----
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = "20261010_overfix"
    for t in TABLES:
        p = BACKUP_DIR / f"{t}_overfix_{stamp}.parquet"
        con.execute(
            f"copy (select * from {t} where code in {code_clause(OVER_FIXED)}) "
            f"to '{p.as_posix()}' (format parquet)")
        print(f"\n【备份】{t} -> {p.name}")

    # ---- 执行 ----
    print("\n【执行】")
    for t in TABLES:
        n = con.execute(
            f"update {t} set volume = volume * 100.0 "
            f"where code in {code_clause(OVER_FIXED)}").fetchone()
        print(f"  {t}: 已 ×100（返回 {n}）")

    # ---- 独立复验：不看上面的打印，重新查库 ----
    out = verify_only(con)
    p = ROOT / "data" / "kline_volume_overfix.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()