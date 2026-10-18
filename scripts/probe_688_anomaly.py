#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交叉验证 3 只比值异常 688（688837 / 688835 / 688806）的真实成交量单位。

★ 背景：验证「m30 Σvolume / 日线 volume ≈ 1.00」不变式时，发现这 3 只比值 = 0.0100
  （即 m30 是日线的 100 倍），且 bar 数都恰好 = 8。
  两种可能，必须用**跨源实测**区分，不能猜：
    ① 修复脚本漏修这 3 只（volume 被重复 ÷100 或从未换算）
    ② 腾讯对这 3 只返回「手」而非「股」（前缀特判失效）

判据：腾讯 fqkline it[5] ÷ 新浪日线 volume
    ≈1.00 ⇒ 腾讯是「股」  ≈0.01 ⇒ 腾讯是「手」
  ★ 基准必须选新浪（新浪全板块统一返回「股」，见 probe_tencent_volume_unit.py）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TARGETS = ["688837", "688835", "688806"]


def tencent_prefix(code: str) -> str:
    return "sh" if code[0] == "6" else ("bj" if code[0] in ("4", "8") else "sz")


def tencent_fqkline(code: str, count: int = 5) -> list[dict]:
    """前复权日线。数组顺序 [日期,开,收,高,低,成交量]。it[5] = 原始成交量（未换算）。"""
    pfx = tencent_prefix(code)
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={pfx}{code},day,,,{count},qfq")
    with httpx.Client(trust_env=False, timeout=15, headers={"User-Agent": UA}) as c:
        r = c.get(url)
    r.raise_for_status()
    node = (r.json().get("data") or {}).get(f"{pfx}{code}") or {}
    rows = node.get("qfqday") or node.get("day") or []
    return [{"date": it[0], "close": float(it[2]), "raw_vol": float(it[5])} for it in rows]


def sina_daily(code: str, datalen: int = 5) -> list[dict]:
    """新浪日线（不复权）。新浪全板块统一返回「股」。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={tencent_prefix(code)}{code}"
           f"&scale=240&ma=no&datalen={datalen}")
    with httpx.Client(trust_env=False, timeout=15, headers={"User-Agent": UA}) as c:
        r = c.get(url)
    r.raise_for_status()
    return [{"date": it["day"][:10], "volume": float(it["volume"])}
            for it in r.json() if it.get("volume")]


def main() -> int:
    report = []
    for code in TARGETS:
        row: dict = {"code": code}
        try:
            tx = tencent_fqkline(code)
            sn = sina_daily(code)
            row["tencent"] = tx[-3:]
            row["sina"] = sn[-3:]
            sn_map = {d["date"]: d["volume"] for d in sn}
            obs = []
            for d in tx:
                sv = sn_map.get(d["date"])
                if not sv:
                    continue
                ratio = d["raw_vol"] / sv
                verdict = ("股(×1)" if 0.5 < ratio < 2
                           else "手(×100)" if 0.005 < ratio < 0.02
                           else f"?未识别={ratio:.4f}")
                obs.append({"date": d["date"], "tencent": d["raw_vol"],
                            "sina": sv, "ratio": round(ratio, 5), "verdict": verdict})
            row["obs"] = obs
            verdicts = {o["verdict"] for o in obs}
            row["summary"] = ("一致:" + "/".join(sorted(verdicts)) if len(verdicts) == 1
                              else "不一致:" + str(sorted(verdicts)))
        except Exception as e:  # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"
        report.append(row)
        print(f"{code}  {row.get('summary') or row.get('error')}")
        for o in row.get("obs", []):
            print(f"    {o['date']}  腾讯={o['tencent']:>15,.0f}  "
                  f"新浪={o['sina']:>15,.0f}  比值={o['ratio']:.5f}  {o['verdict']}")

    out = Path(__file__).resolve().parents[1] / "data" / "probe_688_anomaly.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())