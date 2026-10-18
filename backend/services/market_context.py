"""Point-in-time queries shared by recommendation and strategy assembly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def market_context(conn, as_of: datetime) -> dict:
    cutoff = (
        as_of.replace(tzinfo=CST) if as_of.tzinfo is None else as_of.astimezone(CST)
    )
    row = conn.execute(
        "SELECT ts, state, score, position_cap FROM market_regime WHERE ts<=? "
        "ORDER BY ts DESC LIMIT 1",
        [cutoff.replace(tzinfo=None)],
    ).fetchone()
    if row is None:
        return {
            "state": "unknown",
            "score": None,
            "position_cap": 0.0,
            "ts": None,
            "missing": True,
            "stale": False,
        }
    stale = (cutoff.replace(tzinfo=None) - row[0]).days > 7
    return {
        "ts": row[0].isoformat(),
        "state": "unknown" if stale else row[1],
        "score": row[2],
        "position_cap": 0.0 if stale else row[3],
        "missing": False,
        "stale": stale,
    }


def board_context(conn, as_of: datetime) -> dict[str, dict]:
    cutoff = (
        as_of.replace(tzinfo=CST) if as_of.tzinfo is None else as_of.astimezone(CST)
    )
    rows = conn.execute(
        """SELECT m.code, b.board_code, b.board_name, b.strength, b.rank,
                  b.factor_coverage, b.score_method, b.ts, b.source_complete
           FROM (
             SELECT * FROM board_member_snapshot WHERE snap_date<=?
             QUALIFY snap_date=max(snap_date) OVER (PARTITION BY board_code)
           ) m JOIN (
             SELECT * FROM board_strength WHERE ts<=?
             QUALIFY ts=max(ts) OVER (PARTITION BY board_code)
           ) b ON m.board_code=b.board_code
           ORDER BY m.code, b.strength DESC NULLS LAST, b.board_code""",
        [cutoff.date(), cutoff.replace(tzinfo=None)],
    ).fetchall()
    result = {}
    for code, board, name, strength, rank, coverage, method, ts, complete in rows:
        if code in result or strength is None:
            continue
        result[code] = {
            "board_code": board,
            "name": name,
            "strength": strength,
            "rank": rank,
            "coverage": coverage,
            "method": method,
            "ts": ts.isoformat(),
            "degraded": coverage is None or coverage < 0.999999 or complete is not True,
            "source_complete": complete,
            "stale": (cutoff.replace(tzinfo=None) - ts).days > 7,
        }
    return result
