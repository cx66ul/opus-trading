from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math

import pytest

from backend.domain.image_strategy import evaluate, load_policy, RULES, cutoff_day
from backend.domain.recommend import StockRow, calc_score, recommend, score_weights


def histories():
    start = date(2026, 7, 1)
    bars = [{"trade_date": start + timedelta(days=i), "open": 10 + i * .025,
             "high": 10.1 + i * .025, "low": 9.9 + i * .025,
             "close": 10 + i * .025, "volume": 1000} for i in range(80)]
    idx = [{**r, "close": 3000.0} for r in bars]
    return bars, idx, bars[-1]["trade_date"]


def row(audit, code="600001", **kw):
    base = dict(code=code, name="测试", float_mv=5e9, list_date_lb="2000-01-01",
                s_board=70, s_chanlun=70, s_rps=70, s_news=70, s_emotion=70,
                image_strategy=audit)
    base.update(kw)
    return StockRow(**base)


def test_no_future_bars_and_repeatable():
    bars, idx, day = histories()
    p = load_policy()
    a = evaluate(bars, idx, day, p)
    future = {**bars[-1], "trade_date": day + timedelta(days=1), "close": float("nan")}
    assert evaluate(bars + [future], idx + [future], day, p) == a
    assert a["score"] > 50
    assert a["evaluation_mode"] == "current_policy_on_historical_prices"
    assert all(x["rule_id"] in RULES for x in a["matched"])


@pytest.mark.parametrize("field,value", [("close", float("nan")), ("volume", None), ("volume", 0), ("high", float("inf"))])
def test_bad_data_never_matches(field, value):
    bars, idx, day = histories()
    bars[-1][field] = value
    a = evaluate(bars, idx, day, load_policy())
    assert a["matched"] == [] and not a["eligible"] and a["coverage"] == 0
    assert math.isfinite(a["score"])
    assert recommend([row(a)], day)["recommendations"][0]["tier"] == "C"


def test_stale_or_short_or_duplicate_bars_fail_closed():
    bars, idx, day = histories()
    for data in (bars[:-1], bars[-20:], bars + [bars[-1]]):
        a = evaluate(data, idx, day, load_policy())
        assert not a["eligible"] and not a["matched"]


def test_missing_index_does_not_invent_relative_strength():
    bars, _, day = histories()
    a = evaluate(bars, [], day, load_policy())
    assert a["eligible"] and a["coverage"] == round(8 / 11, 3)
    assert not {"IMG05", "IMG10", "IMG11"}.intersection(x["rule_id"] for x in a["matched"])


def test_down_volume_and_weak_market_penalties_and_cap():
    bars, idx, day = histories()
    for i, r in enumerate(idx):
        r["close"] = 4000 - i * 5
    bars[-1].update(close=10, open=11.5, high=11.6, low=9.9, volume=2500)
    a = evaluate(bars, idx, day, load_policy())
    assert {"IMG08", "IMG09", "IMG10"}.issubset(x["rule_id"] for x in a["matched"])
    result = recommend([row(a)], day, position_cap=.8)
    assert result["position_cap"] == .3
    assert recommend([row(a)], day, position_cap=.1)["position_cap"] == .1


def test_breakout_excludes_today_from_prior_high():
    bars, idx, day = histories()
    bars[-1].update(close=12.5, open=12, high=12.6, low=11.9, volume=2000)
    a = evaluate(bars, idx, day, load_policy())
    assert "IMG03" in [x["rule_id"] for x in a["matched"]]


def test_max_weight_changes_rank_but_not_filters_or_disabled_baseline():
    bars, idx, day = histories()
    a = evaluate(bars, idx, day, load_policy())
    strong, weak = deepcopy(a), deepcopy(a)
    strong["score"], weak["score"] = 90, 20
    low_old = row(strong, "600001", s_board=60, s_chanlun=60, s_rps=60, s_news=60, s_emotion=60)
    high_old = row(weak, "600002", s_board=90, s_chanlun=90, s_rps=90, s_news=90, s_emotion=90)
    assert calc_score(low_old) == 78
    assert calc_score(high_old) == 48
    assert calc_score(low_old, {"image_strategy_enabled": False}) == 60
    weights = score_weights(low_old)
    assert weights["image_strategy"] == .6 > max(v for k, v in weights.items() if k != "image_strategy")
    assert sum(weights.values()) == pytest.approx(1)
    assert recommend([high_old, low_old], day)["recommendations"][0]["code"] == "600001"
    for kwargs in ({"is_st": True}, {"is_suspend": True}, {"is_one_word_limit": True}, {"gain_5d": 40}):
        assert not recommend([row(strong, **kwargs)], day)["recommendations"]
    assert calc_score(row(a, "600206")) == calc_score(row(a, "600999"))


def test_disabled_policy_and_before_close_cutoff():
    bars, idx, day = histories()
    p = load_policy()
    p["enabled"] = False
    a = evaluate(bars, idx, day, p)
    assert not a["enabled"] and calc_score(row(a)) == 70
    assert recommend([row(a)], day)["image_policy"] is None
    # UTC 06:30 = Shanghai 14:30, so same-day daily close must not be read.
    before = datetime(2026, 9, 18, 6, 30, tzinfo=timezone.utc)
    assert cutoff_day(before) == date(2026, 9, 17)


def test_catalog_and_source_provenance():
    from scripts.distill_trade_images import build_catalog, ROOT
    import hashlib
    catalog = build_catalog()
    assert {r["id"] for r in catalog["rules"]} == set(RULES)
    assert len({r["code"] for r in catalog["stock_forecasts"]}) == 29
    assert catalog["counterexamples"] and catalog["unresolved"]
    for rule in catalog["rules"]:
        assert rule["backtest"] is None
        for ev in rule["evidence"]:
            assert hashlib.sha256((ROOT / ev["source"]).read_bytes()).hexdigest() == ev["sha256"]
            assert len(ev["bbox"]) == 4


def test_api_cache_changes_when_policy_changes(monkeypatch):
    from backend.api.webapi import _cache_key
    import backend.domain.image_strategy as mod
    monkeypatch.setattr(mod, "load_policy", lambda: {"policy_hash": "a"})
    a = _cache_key("2026-10-09", 20)
    monkeypatch.setattr(mod, "load_policy", lambda: {"policy_hash": "b"})
    assert a != _cache_key("2026-10-09", 20)


def test_evidence_api_contains_actual_catalog():
    from backend.api.webapi import api_image_strategy
    result = api_image_strategy()
    assert result["policy"]["source_weight"] == .6
    assert len(result["catalog"]["stock_forecasts"]) == 29


def test_bulk_metadata_queries_preserve_subset_and_cutoff():
    import duckdb
    from scripts.daily_recommend import _gain_5d, _list_date_lower_bounds
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE kline_daily(code VARCHAR, trade_date DATE, close DOUBLE)")
    for code in ("600001", "600002"):
        for i in range(8):
            con.execute("INSERT INTO kline_daily VALUES (?,?,?)", [code, date(2026, 1, 1) + timedelta(days=i), 10 + i])
    assert _list_date_lower_bounds(con, ["600001"], date(2026, 1, 6)) == {"600001": "2026-01-01"}
    assert _gain_5d(con, ["600001"], date(2026, 1, 6)) == {"600001": 50.0}
    con.close()
