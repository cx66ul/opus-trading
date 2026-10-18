"""Read-only real-data comparison; not a historical performance backtest."""
import json
import sys
from collections import Counter
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.api.webapi import _as_of_or_latest, _dt
from backend.data import duck
from backend.domain.recommend import recommend
from backend.services.market_context import market_context
from scripts.daily_recommend import build_rows


def main():
    started = perf_counter()
    day = _as_of_or_latest(None)
    as_of = _dt(day)
    rows = build_rows(as_of)
    market = market_context(duck.get_ro_conn(), as_of)
    kwargs = dict(market_state=market["state"], position_cap=market["position_cap"])
    result = recommend(rows, as_of, **kwargs)
    baseline = recommend(rows, as_of, params={"image_strategy_enabled": False}, **kwargs)
    details = {
        "trade_date": day, "elapsed_seconds": round(perf_counter() - started, 3),
        "input_rows": len(rows), "eligible_rows": sum(r.image_strategy["eligible"] for r in rows),
        "match_counts": dict(Counter(x["rule_id"] for r in rows for x in r.image_strategy["matched"])),
        "warning_counts": dict(Counter(x for r in rows for x in r.image_strategy["warnings"])),
        "baseline_codes": [r["code"] for r in baseline["recommendations"]],
        "priority_codes": [r["code"] for r in result["recommendations"]],
        "baseline": baseline, "priority": result,
        "performance_claim": "none; only deterministic ranking comparison on current local database",
    }
    assert result["image_policy"]["weight"] == .6
    assert all("image_strategy" in r["factors"] for r in result["recommendations"])
    assert result["after_filter"] == baseline["after_filter"]
    out = ROOT / "docs/image-strategy-20261011"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ranking-comparison.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in details.items() if k not in ("baseline", "priority", "warning_counts")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
