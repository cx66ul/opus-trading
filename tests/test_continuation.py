"""Regression checks for partial factors, timestamps, and point-in-time assembly."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

from backend.data import duck
from backend.domain.strength import WEIGHTS, percentile, score_boards
from backend.services.market_context import board_context, market_context
from backend.services.tracking import capture_quotes, normalize_quote, tracked_codes
from scripts.daily_recommend import _gain_5d, _news_scores, calc_rps
from scripts.sync_board_strength import build_rows, upsert_board_strength

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 9, 10, 0, 30, tzinfo=CST)


@pytest.fixture
def db(monkeypatch):
    conn = duckdb.connect(":memory:")
    monkeypatch.setattr(duck, "get_conn", lambda: conn)
    monkeypatch.setattr(duck, "get_ro_conn", lambda: conn)
    duck.init_db()
    yield conn
    conn.close()


def test_percentile_ties_and_nonfinite():
    assert percentile(1, [1, 1, 1]) == 50
    assert percentile(1, [1, 1, 2]) == 25
    assert percentile(2, [1, 1, 2]) == 100
    assert percentile(float("nan"), [1, 2]) is None
    assert percentile(None, [1, 2]) is None


def test_eight_factors_and_separate_universes():
    assert sum(WEIGHTS.values()) == pytest.approx(1)
    rows = [
        {"board_code": "A", "type": "industry", **dict.fromkeys(WEIGHTS, 1)},
        {"board_code": "B", "type": "industry", **dict.fromkeys(WEIGHTS, 2)},
        {"board_code": "C", "type": "concept", **dict.fromkeys(WEIGHTS, 100)},
    ]
    result = {r["board_code"]: r for r in score_boards(rows, AS_OF)}
    assert result["A"]["strength"] == 0
    assert result["B"]["strength"] == pytest.approx(100)
    assert result["C"]["strength"] == pytest.approx(50)
    assert result["C"]["rank"] == 1
    assert all(r["score_method"] == "eight_factor_v1" for r in result.values())


def test_missing_factors_are_not_zero_or_raw_inflow():
    rows, _ = build_rows(
        [
            {
                "board_code": "A",
                "type": "industry",
                "pct_chg": 2,
                "net_inflow_yi": 500,
                "up_ratio": 0.8,
            }
        ],
        AS_OF,
    )
    row = rows[0]
    assert row["factor_coverage"] == pytest.approx(0.28)
    assert row["net_inflow"] == 500e8
    assert json.loads(row["factors_json"])["d5_inflow"]["raw"] is None
    assert row["rank_change"] is None
    assert row["score_method"] == "partial_factor_v1"
    assert row["ts"].hour == AS_OF.hour
    assert row["ts"].tzinfo is None


def test_volume_uses_previous_distinct_days_and_excludes_future():
    raw = [{"board_code": "A", "type": "industry", "pct_chg": 2, "amount_yi": 2}]
    history = {
        "A": [
            {"ts": AS_OF - timedelta(days=i), "amount": 1e8, "rank": 10 + i}
            for i in range(1, 6)
        ]
        + [
            {"ts": AS_OF, "amount": 999e8, "rank": 1},
            {"ts": AS_OF + timedelta(days=1), "amount": 999e8, "rank": 1},
        ]
    }
    rows, _ = build_rows(raw, AS_OF, history=history)
    assert rows[0]["vol_ratio"] == 2
    assert json.loads(rows[0]["factors_json"])["d8_rank_slope"]["raw"] == 1


def test_board_migration_and_upsert_are_idempotent(db):
    duck.init_db()
    rows, _ = build_rows(
        [{"board_code": "A", "name": "test", "type": "industry", "pct_chg": 2}], AS_OF
    )
    upsert_board_strength(rows)
    upsert_board_strength(rows)
    assert db.execute("SELECT count(*) FROM board_strength").fetchone()[0] == 1
    assert (
        db.execute("SELECT factor_coverage FROM board_strength").fetchone()[0] == 0.18
    )


def test_quote_age_and_minute_idempotence(db):
    raw = {"ts": "2026-10-09 10:00:29", "code": "600519", "price": 100}
    row = normalize_quote(raw, AS_OF, "tencent")
    assert row["ts"].second == 0
    duck.upsert_quote_snapshot([row])
    raw["price"] = 101
    row2 = normalize_quote(raw, AS_OF + timedelta(seconds=10), "tencent")
    duck.upsert_quote_snapshot([row2])
    assert db.execute("SELECT count(*), max(price) FROM quote_snapshot").fetchone() == (
        1,
        101,
    )
    assert db.execute("SELECT source FROM quote_snapshot").fetchone()[0] == "tencent"
    with pytest.raises(ValueError, match="timestamp"):
        normalize_quote({**raw, "ts": "2026-10-08 15:00:00"}, AS_OF, "sina")
    with pytest.raises(ValueError, match="timestamp"):
        normalize_quote({**raw, "ts": "2026-10-09 10:05:00"}, AS_OF, "sina")


def test_partial_quote_response_falls_back_per_symbol(monkeypatch):
    from backend.services import tracking

    monkeypatch.setattr(tracking, "bootstrap", lambda: None)
    calls = []

    class Adapter:
        def __init__(self, source):
            self.source = source

        def fetch_realtime(self, codes, as_of):
            calls.append((self.source, codes))
            code = "600519" if self.source == "tencent" else "000001"
            return [{"code": code, "ts": "2026-10-09 10:00:29", "price": 10}]

    monkeypatch.setattr(tracking, "get_adapter", Adapter)
    result = capture_quotes(AS_OF, ["600519", "000001"], dry_run=True)
    assert result["missing"] == []
    assert result["written"] == 0
    assert result["accepted"] == 2
    assert calls[1] == ("sina", ["000001"])


def test_track_pool_prioritizes_positions_and_deduplicates():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE position(code,status,shares); "
        "CREATE TABLE watchlist(id,code,priority,add_date);"
    )
    conn.executemany("INSERT INTO position VALUES (?,'holding',100)", [("600519",)])
    conn.executemany(
        "INSERT INTO watchlist VALUES (?,?,?,?)",
        [
            (1, "000001", 2, None),
            (2, "600519", 3, None),
            (3, "300750", 4, "2026-10-10"),
        ],
    )
    assert tracked_codes(conn, AS_OF) == ["600519", "000001"]
    conn.close()


def test_market_and_board_queries_do_not_read_future(db):
    before = AS_OF.replace(tzinfo=None) - timedelta(days=1)
    after = AS_OF.replace(tzinfo=None) + timedelta(days=1)
    db.executemany(
        "INSERT INTO market_regime(ts,state,score,position_cap) VALUES (?,?,?,?)",
        [(before, "ebb", 20, 0.2), (after, "start", 80, 0.7)],
    )
    assert market_context(db, AS_OF)["state"] == "ebb"
    assert market_context(db, before - timedelta(days=1))["state"] == "unknown"
    db.executemany(
        "INSERT INTO board_member_snapshot VALUES (?,?,?,NULL)",
        [(date(2026, 10, 8), "A", "600519"), (date(2026, 10, 10), "A", "000001")],
    )
    db.executemany(
        "INSERT INTO board_strength(ts,board_code,strength) VALUES (?,?,?)",
        [(before, "A", 10), (after, "A", 100)],
    )
    result = board_context(db, AS_OF)
    assert result["600519"]["strength"] == 10
    assert result["600519"]["degraded"]
    assert "000001" not in result


def test_rps_and_five_day_gain_stop_at_cutoff(db):
    rows = []
    for idx in range(21):
        for code, value in [("600519", 10 + idx), ("000001", 100 - idx)]:
            rows.append((code, date(2026, 9, 17) + timedelta(days=idx), value))
    rows.append(("000001", date(2026, 10, 20), 1000))
    db.executemany(
        "INSERT INTO kline_daily(code,trade_date,close) VALUES (?,?,?)", rows
    )
    rps = calc_rps(["600519", "000001"], date(2026, 10, 7))
    assert rps["600519"] > rps["000001"]
    assert calc_rps(["600519"], date(2026, 10, 7))["600519"] == rps["600519"]
    gains = _gain_5d(db, ["600519"], date(2026, 10, 7))
    assert gains["600519"] == pytest.approx((30 / 25 - 1) * 100, abs=0.01)


def test_news_uses_injected_time_and_excludes_future():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE news(ts,related_codes,direction,strength,content)")
    conn.executemany(
        "INSERT INTO news VALUES (?,?,?,?,?)",
        [
            ("2026-10-09T09:00:00+08:00", '["600519"]', "bull", 3, "before"),
            ("2026-10-09T11:00:00+08:00", '["600519"]', "bear", 5, "future"),
            ("2026-10-09 09:30:00", '["600519"]', "bull", 1, "naive CST"),
        ],
    )
    result = _news_scores(conn, ["600519"], as_of=AS_OF)
    assert result["600519"]["score"] == 70
    conn.close()


def test_incomplete_board_capture_is_persisted(db):
    rows, _ = build_rows([{"board_code": "A", "pct_chg": 2,
                           "collection_complete": False}], AS_OF)
    upsert_board_strength(rows)
    assert db.execute("SELECT source_complete FROM board_strength").fetchone() == (False,)


def test_stale_market_context_is_not_fabricated(db):
    db.execute("INSERT INTO market_regime(ts,state,score,position_cap) VALUES (?,?,?,?)",
               [AS_OF.replace(tzinfo=None) - timedelta(days=10), "start", 80, .7])
    context = market_context(db, AS_OF)
    assert context["stale"]
    assert context["state"] == "unknown"
    assert context["position_cap"] == 0


def test_morning_assembly_never_passes_current_day_close_to_chan(db, monkeypatch):
    from backend.data import sqlite
    from backend.domain import chan
    from scripts import daily_recommend

    business = sqlite3.connect(":memory:")
    business.executescript("CREATE TABLE stock(id,name,float_mv,is_suspend,status,is_st); "
                          "CREATE TABLE news(ts,related_codes,direction,strength,content);")
    business.execute("INSERT INTO stock VALUES ('600519','test',3000000000,0,'active',0)")
    bars = [("600519", date(2026, 7, 30) + timedelta(days=i), 10, 11, 9, 10, 100, 0)
            for i in range(75)]
    db.executemany("INSERT INTO kline_daily(code,trade_date,open,high,low,close,volume,pct_chg) "
                   "VALUES (?,?,?,?,?,?,?,?)", bars)
    monkeypatch.setattr(sqlite, "get_conn", lambda: business)
    seen = []
    def analyze(code, rows, as_of):
        seen.extend(r["trade_date"] for r in rows)
        return object()
    monkeypatch.setattr(chan, "analyze", analyze)
    monkeypatch.setattr(daily_recommend, "_chan_to_score", lambda result: (50, []))
    rows = daily_recommend.build_rows(AS_OF)
    assert len(rows) == 1
    assert seen and max(seen) == date(2026, 10, 8)
    assert rows[0].s_emotion == 0
    assert rows[0].board_degraded
    business.close()
