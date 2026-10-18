"""User-priority screenshot strategy. Pure price-pattern evaluation, not a return forecast.

Evidence catalog: data/image_distillation/20261011/distilled.json.
All numeric tolerances are disclosed engineering interpretations. No ticker whitelist.
Missing evidence never matches; no claim that volume identifies actual institutional flows.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

POLICY_PATH = Path(__file__).resolve().parents[2] / "config/image_strategy.json"
CST = timezone(timedelta(hours=8))
BASE_WEIGHTS = {"board": .25, "chanlun": .25, "rps": .20, "news": .15, "emotion": .15}

# Rule identifiers resolve to visually checked quotes and tile bounding boxes in the catalog.
RULES = {
    "IMG01": "上升趋势沿5日线回踩",
    "IMG02": "缩量企稳且未破前低",
    "IMG03": "横盘整理后放量突破",
    "IMG04": "均线收敛、波动收缩",
    "IMG05": "阶段表现强于上证指数",
    "IMG06": "远离5日线，追高风险",
    "IMG07": "高位连续上涨后放量",
    "IMG08": "放量大跌，回避信号",
    "IMG09": "5日线低于10日线且收盘低于5日线",
    "IMG10": "上证空头排列，反弹不等于反转",
    "IMG11": "上证缩量，轮动持续性不足",
}


def load_policy(path=POLICY_PATH):
    raw = Path(path).read_bytes()
    p = json.loads(raw)
    w = p["source_weight"]
    if not isinstance(w, (float, int)) or not math.isfinite(w) or not .5 < w < 1:
        raise ValueError("图片策略权重必须大于50%且小于100%")
    if not isinstance(p.get("enabled"), bool):
        raise ValueError("enabled必须为布尔值")
    for key in ("ma5_distance", "shrink_ratio", "breakout_volume_ratio", "base_range",
                "ma_spread", "chase_distance", "high_gain20", "weak_market_cap"):
        if not isinstance(p[key], (float, int)) or not math.isfinite(p[key]) or p[key] <= 0:
            raise ValueError(f"非法策略参数: {key}")
    if not math.isfinite(p["heavy_down_return"]) or p["heavy_down_return"] >= 0:
        raise ValueError("heavy_down_return必须小于0")
    if p["weak_market_cap"] > 1:
        raise ValueError("仓位上限不能超过100%")
    p["policy_hash"] = hashlib.sha256(raw).hexdigest()
    return p


def cutoff_day(as_of):
    if isinstance(as_of, datetime):
        local = as_of.astimezone(CST) if as_of.tzinfo else as_of
        return local.date() if local.hour >= 15 else local.date() - timedelta(days=1)
    return date.fromisoformat(str(as_of)[:10])


def _history(rows, cutoff):
    # Filter future bars BEFORE inspecting their data. Duplicate dates fail closed.
    out, seen = [], set()
    for r in rows:
        try:
            d = date.fromisoformat(str(r["trade_date"])[:10])
        except (KeyError, ValueError, TypeError):
            return [], "交易日期无效"
        if d > cutoff:
            continue
        if d in seen:
            return [], "重复交易日期"
        seen.add(d)
        out.append({**r, "trade_date": d})
    return sorted(out, key=lambda r: r["trade_date"]), None


def _valid(rows, fields):
    try:
        return all(all(math.isfinite(float(r[k])) and float(r[k]) > 0 for k in fields)
                   for r in rows)
    except (KeyError, TypeError, ValueError):
        return False


def evaluate(bars, index_bars, as_of, policy, *, expected_day=None):
    """Use completed bars <= as_of; expected_day is latest market session at that cutoff."""
    cutoff = cutoff_day(as_of)
    result = {
        "enabled": policy["enabled"], "revision": policy["revision"],
        "policy_hash": policy["policy_hash"], "weight": policy["source_weight"],
        "available_from": policy["available_from"], "validation_status": policy["validation_status"],
        "evaluation_mode": ("current_policy_on_historical_prices" if str(cutoff) < policy["available_from"]
                            else "current_policy"),
        "score": 50.0, "coverage": 0.0, "matched": [], "checks": [], "metrics": {},
        "warnings": [], "eligible": False, "market_position_cap": None,
        "unautomated": ["题材与财报增长需时点财报核验", "圆弧底/首板示意图未作精确模式识别",
                        "无法仅凭量价证明主力资金行为"],
    }
    if not policy["enabled"]:
        return result
    rows, error = _history(bars, cutoff)
    idx, idx_error = _history(index_bars, cutoff)
    if error or len(rows) < 61 or not _valid(rows[-61:], ("open", "high", "low", "close", "volume")):
        result["warnings"].append(error or "至少需要61根有效日线，OHLC与成交量须为正数")
        return result
    last = rows[-1]["trade_date"]
    expected = date.fromisoformat(str(expected_day)[:10]) if expected_day else cutoff
    if last != expected:
        result["warnings"].append(f"行情过期：个股{last}，应为{expected}")
        return result
    if any(float(r["low"]) > min(float(r["open"]), float(r["close"])) or
           float(r["high"]) < max(float(r["open"]), float(r["close"])) for r in rows[-61:]):
        result["warnings"].append("OHLC价格范围无效")
        return result
    result["eligible"] = True
    c = [float(r["close"]) for r in rows]
    v = [float(r["volume"]) for r in rows]
    ma5, ma10, ma20, ma60 = [mean(c[-n:]) for n in (5, 10, 20, 60)]
    prev_ma5 = mean(c[-6:-1])
    dist = c[-1] / ma5 - 1
    ret = c[-1] / c[-2] - 1
    gain20 = c[-1] / c[-21] - 1
    vr = v[-1] / mean(v[-6:-1])
    shrink = mean(v[-3:]) / mean(v[-13:-3])
    prior = rows[-21:-1]
    top = max(float(r["high"]) for r in prior)
    bottom = min(float(r["low"]) for r in prior)
    stable = c[-1] >= min(float(r["low"]) for r in rows[-11:-1]) and c[-1] >= c[-2] * .98
    uptrend = ma5 >= ma10 >= ma20 and ma5 > prev_ma5
    spread = max(ma5, ma10, ma20) / min(ma5, ma10, ma20) - 1
    range5 = max(c[-5:]) / min(c[-5:]) - 1
    prior_range = max(c[-15:-5]) / min(c[-15:-5]) - 1
    result["metrics"] = {"last_bar": str(last), "ma5": ma5, "ma10": ma10, "ma20": ma20,
                         "ma60": ma60, "ma5_distance": dist, "volume_ratio": vr,
                         "shrink_ratio": shrink, "gain20": gain20,
                         "prior20_high": top, "prior20_low": bottom}
    score = 50.0

    def check(rule_id, matched, points, available=True):
        nonlocal score
        item = {"rule_id": rule_id, "name": RULES[rule_id], "available": available,
                "matched": bool(matched) if available else False,
                "points": points if available and matched else 0}
        result["checks"].append(item)
        if available and matched:
            result["matched"].append(item)
            score += points

    check("IMG01", uptrend and abs(dist) <= policy["ma5_distance"] and ret <= .03, 20)
    check("IMG02", shrink <= policy["shrink_ratio"] and stable and c[-1] >= ma20, 12)
    check("IMG03", top / bottom - 1 <= policy["base_range"] and c[-1] > top
          and vr >= policy["breakout_volume_ratio"], 18)
    check("IMG04", spread <= policy["ma_spread"] and range5 < prior_range
          and stable and c[-1] >= ma20, 8)
    index_ok = not idx_error and len(idx) >= 61 and idx[-1]["trade_date"] == last and _valid(idx[-61:], ("close", "volume"))
    relative_ok = index_ok and rows[-21]["trade_date"] == idx[-21]["trade_date"]
    check("IMG05", relative_ok and gain20 > float(idx[-1]["close"]) / float(idx[-21]["close"]) - 1, 10, relative_ok)
    check("IMG06", dist > policy["chase_distance"], -25)
    check("IMG07", gain20 > policy["high_gain20"] and c[-1] >= max(c[-21:]) * .95
          and vr >= policy["breakout_volume_ratio"], -20)
    check("IMG08", ret <= policy["heavy_down_return"] and vr >= policy["breakout_volume_ratio"], -35)
    check("IMG09", ma5 < ma10 and c[-1] < ma5, -25)
    bearish = thin = False
    if index_ok:
        ic = [float(r["close"]) for r in idx]
        bearish = ic[-1] < mean(ic[-5:]) < mean(ic[-10:]) < mean(ic[-20:])
        thin = mean([float(r["volume"]) for r in idx[-3:]]) < mean([float(r["volume"]) for r in idx[-23:-3]]) * policy["shrink_ratio"]
        if bearish:
            result["market_position_cap"] = policy["weak_market_cap"]
    else:
        result["warnings"].append("同期上证指数数据不足，市场与相对强弱规则未触发")
    if index_ok and not relative_ok:
        result["warnings"].append("个股与指数20日窗口起点不一致，相对强弱未触发")
    check("IMG10", bearish, -15, index_ok)
    check("IMG11", thin, -8, index_ok)
    result["coverage"] = round(sum(x["available"] for x in result["checks"]) / len(RULES), 3)
    result["score"] = round(max(0, min(100, score)), 2)
    return result
