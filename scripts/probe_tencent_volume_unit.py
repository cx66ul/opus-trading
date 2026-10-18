# -*- coding: utf-8 -*-
"""腾讯成交量单位**逐板块实测**（2026-10-10）

★ 起因：修688 单位错误时，我在测试里写了「689/787 未实测，按主板处理」——
  这本身是**把猜测固化成了断言**。本脚本把能实测的板块全部实测一遍，
  把「猜」变成「量」。

★ 判据（可自证，与量纲无关）：
  同一 code 同一天，腾讯 `it[5]` / 新浪 `volume` 求比值。
    比值 ≈ 1.00 ⇒ 腾讯已是「股」（×1）
    比值 ≈ 0.01 ⇒ 腾讯是「手」（×100）
  ★ 为什么用新浪做基准：新浪**全板块统一返回「股」**（实测 600519=3511051
    与腾讯 35111×100 吻合；920001=1463171 同理）⇒ 新浪是无歧义基准。
  ★ 为什么不用「成交额/(量×价)」：该判据在 part 1 已证明过——
    若volume 口径不一致，它会把两个错误混在一起（记忆里的JOIN/口径教训）。

★ 覆盖不到的（诚实标出，不外推）：
  腾讯日线对北交所旧码（43x/83x/87x）**返回空**⇒ 无法用腾讯侧判定。
  采集器的多源降级（`sync.fetch_daily_robust` 根数<60 自动切新浪）已覆盖该场景。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ★ 每块取 2 只，避免「单只异常」被当成板块规律
PROBES = [
    ("sh600519", "沪主板 600"), ("sh601398", "沪主板 601"),
    ("sh603288", "沪主板 603"), ("sh605588", "沪主板 605"),
    ("sz000001", "深主板 000"), ("sz001979", "深主板 001"),
    ("sz002594", "深主板 002"), ("sz003816", "深主板 003"),
    ("sz300750", "创业板 300"), ("sz301000", "创业板 301"),
    ("bj920001", "北证920"),
    ("sh688469", "科创板 688"), ("sh688008", "科创板 688"),
    ("sh688981", "科创板 688"), ("sh688256", "科创板 688"),
    ("sh689009", "★科创板CDR 689"), ("sh689008", "★科创板CDR 689"),
    ("bj430047", "北交所旧码 430"), ("bj830799", "北交所旧码 830"),
    ("bj871981", "北交所旧码 871"),
    ("sh787001", "★存托凭证 787"), ("sh787981", "★存托凭证 787"),
]


def probe(c, sym: str) -> dict:
    """返回 {ok, tencent, sina, ratio, verdict, note}"""
    out = {"sym": sym, "ok": False}
    # --- 腾讯日线 ---
    try:
        t = c.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                  params={"param": f"{sym},day,,,5,qfq"}).text
        node = (json.loads(t).get("data") or {}).get(sym) or {}
        arr = node.get("qfqday") or node.get("day") or []
        tx = {r[0]: float(r[5]) for r in arr if len(r) >= 6 and r[5]}
    except Exception as exc:
        out["note"] = f"腾讯失败 {str(exc)[:50]}"
        return out
    # --- 新浪日线（基准）---
    try:
        s = c.get("https://money.finance.sina.com.cn/quotes_service/api/"
                  "json_v2.php/CN_MarketData.getKLineData",
                  params={"symbol": sym, "scale": 240, "ma": "no",
                          "datalen": 5}).text
        sn = {r["day"][:10]: float(r["volume"]) for r in json.loads(s)}
    except Exception as exc:
        out["note"] = f"新浪失败 {str(exc)[:50]}"
        return out
    common = sorted(set(tx) & set(sn))
    if not common:
        out["note"] = (f"无共同日期（腾讯 {len(tx)} 根/新浪 {len(sn)} 根）"
                       f"{'—— 腾讯不覆盖该板块' if not tx else ''}")
        return out
    d = common[-1]
    ratio = tx[d] / sn[d] if sn[d] else 0.0
    out.update(ok=True, date=d, tencent=tx[d], sina=sn[d], ratio=round(ratio, 5))
    #★ 判据方向别搞反：腾讯/新浪。腾讯=股 ⇒ 1.00；腾讯=手 ⇒ 0.01
    if 0.5 < ratio < 2:
        out["verdict"] = "股(×1)"
    elif 0.005 < ratio < 0.02:
        out["verdict"] = "手(×100)"
    else:
        out["verdict"] = f"?未识别比值={ratio:.4f}"
    return out


def main() -> None:
    import httpx
    # ★ 沙箱代理会让localhost/内网探测出502、并把外网请求导向代理，
    #   导致"腾讯不覆盖"的假结论 ⇒ 必须 trust_env=False。
    c = httpx.Client(trust_env=False, timeout=20)
    print("=" * 78)
    print("腾讯成交量单位 · 逐板块实测（基准：新浪=股）")
    print("=" * 78)
    print(f"{'代码':<10}{'板块':<18}{'日期':<12}{'腾讯':>16}{'新浪':>16}"
          f"{'比值':>9}  判定")
    results = []
    for sym, board in PROBES:
        r = probe(c, sym)
        results.append({**r, "board": board, "code": sym[2:]})
        if r["ok"]:
            print(f"{sym[2:]:<10}{board:<18}{r['date']:<12}"
                  f"{r['tencent']:>16,.0f}{r['sina']:>16,.0f}"
                  f"{r['ratio']:>9.3f}  {r['verdict']}")
        else:
            print(f"{sym[2:]:<10}{board:<18}{'—':<12}{'—':>16}{'—':>16}"
                  f"{'—':>9}  ⚠ {r.get('note')}")

    # ---- 按板块汇总 ----
    print("\n" + "=" * 78)
    print("按板块汇总（同一板块内所有样本必须一致，不一致即报警）")
    print("=" * 78)
    by = {}
    for r in results:
        by.setdefault(r["board"], []).append(r)
    table = {}
    for board, rs in sorted(by.items()):
        ok = [r for r in rs if r["ok"]]
        bad = [r for r in rs if not r["ok"]]
        verdicts = sorted({r["verdict"] for r in ok})
        if len(verdicts) == 1:
            v = verdicts[0]
        elif not verdicts:
            v = "无法判定（腾讯无数据）"
        else:
            v = "⚠ 不一致:" + ",".join(verdicts)
        table[board] = {"verdict": v, "n_ok": len(ok), "n_fail": len(bad),
                        "codes": [r["code"] for r in rs],
                        "fail_reason": [r.get("note") for r in bad]}
        print(f"  {board:<18}{v:<24} 成功 {len(ok)}/{len(rs)}")

    p = ROOT / "data" / "tencent_volume_unit_probe.json"
    p.write_text(json.dumps({"probe": results, "by_board": table,
                             "baseline": "新浪 volume = 股（全板块统一）"},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()