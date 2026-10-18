"""Capture the tracked pool using per-symbol failover and source timestamps."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from backend.data import duck
from backend.data.adapter import bootstrap, get_adapter
from backend.data.base import SourceError
from backend.data.sqlite import get_conn, log_event

CST = timezone(timedelta(hours=8))


def tracked_codes(conn, as_of: datetime, limit: int = 60) -> list[str]:
    if not 1 <= limit <= 60:
        raise ValueError("tracking limit must be between 1 and 60")
    held = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT code FROM position WHERE status='holding' AND shares>0 ORDER BY code"
        )
    ]
    watched = [
        r[0]
        for r in conn.execute(
            "SELECT code FROM watchlist WHERE add_date IS NULL OR substr(add_date,1,10)<=? "
            "ORDER BY priority DESC, id",
            (as_of.date().isoformat(),),
        )
    ]
    codes = list(dict.fromkeys(held + watched))
    if len(held) > limit:
        raise ValueError("held positions exceed tracking budget")
    return codes[:limit]


def normalize_quote(
    row: dict, as_of: datetime, source: str, max_age_s: int = 180
) -> dict:
    captured = (
        as_of.replace(tzinfo=CST) if as_of.tzinfo is None else as_of.astimezone(CST)
    )
    source_time = datetime.fromisoformat(str(row["ts"]))
    source_time = (
        source_time.replace(tzinfo=CST)
        if source_time.tzinfo is None
        else source_time.astimezone(CST)
    )
    age = (captured - source_time).total_seconds()
    if age < 0 or age > max_age_s:
        raise ValueError(f"stale/future source timestamp: age={age:.0f}s")
    price = row.get("price")
    if (
        isinstance(price, bool)
        or price is None
        or not math.isfinite(float(price))
        or price <= 0
    ):
        raise ValueError("invalid price")
    return {
        **row,
        "ts": captured.replace(second=0, microsecond=0, tzinfo=None),
        "source_ts": source_time.replace(tzinfo=None),
        "captured_at": captured.replace(tzinfo=None),
        "source": source,
    }


def capture_quotes(
    as_of: datetime,
    codes: list[str] | None = None,
    dry_run: bool = False,
    max_age_s: int = 180,
) -> dict:
    codes = list(
        dict.fromkeys(codes if codes is not None else tracked_codes(get_conn(), as_of))
    )
    if len(codes) > 60 or any(len(c) != 6 or not c.isdigit() for c in codes):
        raise ValueError("tracked pool requires at most 60 six-digit codes")
    if max_age_s < 0:
        raise ValueError("max_age_s must be non-negative")
    if not codes:
        return {"requested": 0, "written": 0, "missing": [], "errors": [], "rows": []}
    bootstrap()
    pending = set(codes)
    accepted = {}
    errors = []
    for source in ("tencent", "sina"):
        if not pending:
            break
        try:
            rows = get_adapter(source).fetch_realtime(sorted(pending), as_of)
        except (SourceError, ValueError, TypeError) as exc:
            errors.append({"source": source, "error": str(exc)[:200]})
            continue
        for row in rows:
            code = row.get("code")
            if code not in pending:
                continue
            try:
                accepted[code] = normalize_quote(row, as_of, source, max_age_s)
                pending.remove(code)
            except (ValueError, TypeError, KeyError) as exc:
                errors.append({"source": source, "code": code, "error": str(exc)})
    normalized = list(accepted.values())
    written = 0
    if not dry_run and normalized:
        duck.init_db()
        written = duck.upsert_quote_snapshot(normalized)
    detail = {
        "requested": len(codes),
        "accepted": len(normalized),
        "written": written,
        "missing": sorted(pending),
        "errors": errors,
        "sources": sorted({r["source"] for r in normalized}),
    }
    if not dry_run:
        log_event("quote_capture", detail, as_of.isoformat(timespec="seconds"))
    return {**detail, "rows": normalized}
