#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 接口字段探测脚本（附录 A.5）

设计意图：免费接口的字段编码会无声变动，**严禁凭记忆硬编字段含义**。
本脚本的职责是把"真实响应"变成"可版本管理的样本文件"，
字段含义的确认由人工完成，确认结果落config/fields/*.yaml，代码只读配置。

用法：
    python scripts/probe.py --source sina_list  --out samples/sina_list.json
    python scripts/probe.py --source tencent_quote --codes 600519,000001 --out samples/tencent_quote.txt
    python scripts/probe.py --source sina_kline --code 600519 --scale 240 --datalen 5 --out ...
    python scripts/probe.py --source tencent_kline --code 600519 --period day --out ...
    python scripts/probe.py --source reachability --out samples/reachability.json
    python scripts/probe.py --source all --out-dir samples

硬性要求（对应附录 A.5）：
  ① 原始响应原样落盘（不做任何字段改写）
  ② 附带请求入参
  ③ 附带抓取时间
  ④ 支持 --repeat N 观察稳定性
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
CST = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 端点定义
# 说明：端点与参数来自附录 A.1，但**可达性必须实测**（见 samples/reachability.json）。
# push2.eastmoney.com 在部分网络环境不可达，因此东财相关端点标为可选。
ENDPOINTS: dict[str, dict] = {
    # ---- 新浪：全市场列表（本机实测可用，单节点 hs_a 覆盖沪深京）----
    "sina_list": {
        "url": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData",
        "params": {"page": 1, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a"},
        "headers": {"Referer": "https://finance.sina.com.cn/"},
        "encoding": "utf-8",
        "enabled": True,
    },
    # ---- 新浪：分时列表（跟踪池分钟线的候选源）----
    "sina_minute": {
        "url": "https://vip.stock.finance.sina.com.cn/quotes_service/view/vMS_tradedetail.php",
        "params": {"page": 1, "num": 5, "symbol": "sh600519"},
        "headers": {"Referer": "https://finance.sina.com.cn/"},
        "encoding": "utf-8",
        "enabled": True,
    },
    # ---- 新浪：日线K（不复权）----
    "sina_kline": {
        "url": "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "CN_MarketData.getKLineData",
        "params": {"symbol": "sh600519", "scale": 240, "ma": "no", "datalen": 5},
        "headers": {"Referer": "https://finance.sina.com.cn/"},
        "encoding": "utf-8",
        "enabled": True,
    },
    # ---- 新浪：实时行情（跟踪池 3s 轨道备源，必须带 Referer）----
    "sina_quote": {
        "url": "https://hq.sinajs.cn/list=sh600519,sz000001",
        "headers": {"Referer": "https://finance.sina.com.cn"},
        "encoding": "gbk",
        "enabled": True,
    },
    # ---- 腾讯：实时行情（跟踪池 3s 主源，一次请求可拿多只）----
    "tencent_quote": {
        "url": "https://qt.gtimg.cn/q=sh600519,sz000001",
        "headers": {},
        "encoding": "gbk",
        "enabled": True,
    },
    # ---- 腾讯：前复权K线 ----
    "tencent_kline": {
        "url": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "params": {"code": "600519", "prefix": "sh", "period": "day", "count": 5, "fq": "qfq"},
        "headers": {},
        "encoding": "utf-8",
        "enabled": True,
    },
    # ---- 腾讯：分钟K ----
    "tencent_minute": {
        "url": "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
        "params": {"code": "600519", "prefix": "sh", "period": "m30", "count": 10},
        "headers": {},
        "encoding": "utf-8",
        "enabled": True,
    },
    # ---- 东财：全市场行情（本机实测不可达，保留配置待换网络环境验证）----
    "eastmoney_clist": {
        "url": "https://push2.eastmoney.com/api/qt/clist/get",
        "params": {
            "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23",
        },
        "headers": {"Referer": "https://quote.eastmoney.com/"},
        "encoding": "utf-8",
        "enabled": False,
    },
    "eastmoney_kline": {
        "url": "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "params": {
            "secid": "1.600519", "klt": 101, "fqt": 1, "lmt": 5, "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        "headers": {"Referer": "https://quote.eastmoney.com/"},
        "encoding": "utf-8",
        "enabled": False,
    },
    "eastmoney_ztpool": {
        "url": "https://push2ex.eastmoney.com/getTopicZTPool",
        "params": {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": 0, "pagesize": 10, "sort": "fbt:asc", "date": "20261008",
        },
        "headers": {"Referer": "https://quote.eastmoney.com/"},
        "encoding": "utf-8",
        "enabled": True,
    },
    # ---- 网易（实测不可达，留档）----
    "netease_quote": {
        "url": "https://api.money.126.net/data/feed/0600519",
        "headers": {},
        "encoding": "utf-8",
        "enabled": False,
    },
}


def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def fetch(name: str, override_params: dict | None = None, timeout: float = 20.0) -> dict:
    """抓一个端点，返回 {meta, meta.status, raw} —— raw 保持原样，不做任何加工"""
    spec = ENDPOINTS[name]
    params = dict(spec.get("params") or {})
    if override_params:
        params.update(override_params)
    # 实时行情类端点：代码列表内嵌在 URL 路径里，没有额外 query 参数
    embedded = params.pop("codes", None)
    url = spec["url"]
    if name == "tencent_quote":
        url = f"https://qt.gtimg.cn/q={embedded or 'sh600519,sz000001'}"
        params = {}
    elif name == "sina_quote":
        url = f"https://hq.sinajs.cn/list={embedded or 'sh600519,sz000001'}"
        params = {}
    elif name == "tencent_kline":
        code = params.get("code", "600519")
        pre = params.get("prefix", "sh")
        params = {"param": f"{pre}{code},{params.get('period', 'day')},,,"
                         f"{params.get('count', 5)},{params.get('fq', 'qfq')}"}
    elif name == "tencent_minute":
        code = params.get("code", "600519")
        pre = params.get("prefix", "sh")
        params = {"param": f"{pre}{code},{params.get('period', 'm30')},,,"
                         f"{params.get('count', 10)}"}

    headers = {"User-Agent": UA, **spec.get("headers", {})}
    meta = {
        "source": name,
        "url": url,
        "params": params,
        "headers": {k: v for k, v in headers.items() if k != "User-Agent"},
        "encoding": spec["encoding"],
        "fetched_at": _now_iso(),
    }

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as c:
            r = c.get(url, params=params, headers=headers)
        meta["status"] = r.status_code
        meta["elapsed_ms"] = int(r.elapsed.total_seconds() * 1000)
        meta["content_type"] = r.headers.get("content-type", "")
        body = r.content.decode(spec["encoding"], errors="replace")
        meta["bytes"] = len(r.content)
        return {"meta": meta, "raw": body}
    except Exception as exc:  # 网络异常也要落样本，否则下次又要重新排查
        meta["status"] = 0
        meta["error"] = f"{type(exc).__name__}: {exc}"
        return {"meta": meta, "raw": ""}


def dump(res: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    m, raw = res["meta"], res["raw"]
    if out.suffix == ".txt":
        out.write_text(
            f"# fetched_at: {m['fetched_at']}\n# url: {m['url']}\n"
            f"# params: {json.dumps(m['params'], ensure_ascii=False)}\n"
            f"# status: {m['status']}  bytes: {m.get('bytes')}  encoding: {m['encoding']}\n"
            f"# ---- 以下为原始响应，未做任何改写 ----\n{raw}",
            encoding="utf-8",
        )
    else:
        out.write_text(
            json.dumps({"meta": m, "raw": raw}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def probe_reachability() -> dict:
    """逐个端点探活，结果决定 config/config.yaml 的降级顺序"""
    rows = []
    for name, spec in ENDPOINTS.items():
        r = fetch(name)
        ok = r["meta"]["status"] == 200 and len(r["raw"]) > 0
        rows.append({
            "source": name,
            "url": spec["url"],
            "status": r["meta"]["status"],
            "bytes": r["meta"].get("bytes", 0),
            "elapsed_ms": r["meta"].get("elapsed_ms"),
            "error": r["meta"].get("error", ""),
            "reachable": ok,
        })
        print(f"  {'OK  ' if ok else 'FAIL'} {name:22s} status={rows[-1]['status']:<4}"
              f" bytes={rows[-1]['bytes']:<7} {rows[-1]['elapsed_ms']}ms "
              f"{rows[-1]['error'][:60]}")
    out = {"fetched_at": _now_iso(), "results": rows}
    (SAMPLES / "reachability.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


PROBES = {
    "sina_list": lambda a: (None, {"page": a.page, "num": a.num}),
    "sina_kline": lambda a: (None, {"symbol": f"{a.prefix}{a.code}", "scale": a.scale,
                                    "ma": "no", "datalen": a.datalen}),
    "sina_minute": lambda a: (None, {"symbol": f"{a.prefix}{a.code}", "page": 1, "num": a.num}),
    "tencent_quote": lambda a: (None, {"codes": "sh600519,sz000001,sz300750,sh688981,bj920000"}),
    "sina_quote": lambda a: (None, {"codes": "sh600519,sz000001,sz300750,sh688981,bj920000"}),
    "tencent_kline": lambda a: (None, {"code": a.code, "period": a.period, "count": a.num,
                                       "fq": a.fq}),
    "tencent_minute": lambda a: (None, {"code": a.code, "period": a.mperiod,
                                        "count": a.num}),
    "eastmoney_clist": lambda a: (None, {"page": a.page, "num": a.num}),
    "eastmoney_kline": lambda a: (None, None),
    "eastmoney_ztpool": lambda a: (None, {"date": a.date}),
    "netease_quote": lambda a: (None, None),
}

SUFFIX = {"sina_quote": ".txt", "tencent_quote": ".txt"}


def main() -> int:
    p = argparse.ArgumentParser(description="Hawk 接口字段探测")
    p.add_argument("--source", required=True,
                   help="端点名 / all / reachability，详见脚本内 ENDPOINTS")
    p.add_argument("--out", default="")
    p.add_argument("--out-dir", default="samples")
    p.add_argument("--codes", default="sh600519,sz000001")
    p.add_argument("--code", default="600519")
    p.add_argument("--prefix", default="sh", choices=["sh", "sz", "bj"])
    p.add_argument("--period", default="day", help="日K周期：day/week/month")
    p.add_argument("--mperiod", default="m30",
                   help="分钟K周期：m1/m5/m15/m30/m60（与 --period 分开，避免互相覆盖）")
    p.add_argument("--fq", default="qfq", choices=["qfq", "hfq", ""])
    p.add_argument("--scale", type=int, default=240)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--num", type=int, default=5)
    p.add_argument("--datalen", type=int, default=5)
    p.add_argument("--date", default=datetime.now(CST).strftime("%Y%m%d"))
    p.add_argument("--repeat", type=int, default=1, help="重复次数，观察稳定性")
    a = p.parse_args()

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if a.source in ("all", "reachability"):
        print("[probe] 端点可达性探测")
        probe_reachability()
        if a.source == "reachability":
            return 0

    targets = list(PROBES) if a.source == "all" else [a.source]
    for name in targets:
        if name not in PROBES:
            print(f"未知端点: {name}", file=sys.stderr)
            return 2
        _, params = PROBES[name](a)
        print(f"[probe] {name}")
        for i in range(a.repeat):
            res = fetch(name, params)
            suffix = SUFFIX.get(name, ".json")
            out = Path(a.out) if a.out else out_dir / f"{name}{suffix}"
            dump(res, out)
            m = res["meta"]
            print(f"  #{i+1} status={m['status']} bytes={m.get('bytes', 0)} "
                  f"{m.get('elapsed_ms')}ms -> {out.name}")
            if i < a.repeat - 1:
                time.sleep(0.7)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())