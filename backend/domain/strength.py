"""Eight-factor cross-sectional strength with explicit partial-data evidence."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from datetime import datetime

WEIGHTS = {
    "d1_pct": 0.18,
    "d2_rs5": 0.14,
    "d3_limit_ratio": 0.15,
    "d4_max_streak": 0.10,
    "d5_inflow": 0.15,
    "d6_vol_ratio": 0.10,
    "d7_up_ratio": 0.10,
    "d8_rank_slope": 0.08,
}


def finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def percentile(value: object, population: list[float]) -> float | None:
    number = finite(value)
    values = sorted(x for item in population if (x := finite(item)) is not None)
    if number is None or not values:
        return None
    if len(values) == 1:
        return 50.0
    lo, hi = bisect_left(values, number), bisect_right(values, number)
    return 100 * (lo + hi - 1) / 2 / (len(values) - 1)


def score_boards(rows: list[dict], as_of: datetime) -> list[dict]:
    """Rank industry and concept universes separately; never invent missing factors."""
    output = []
    for kind in sorted({r.get("type", "industry") for r in rows}):
        group = [r for r in rows if r.get("type", "industry") == kind]
        populations = {
            key: [v for r in group if (v := finite(r.get(key))) is not None]
            for key in WEIGHTS
        }
        scored = []
        for row in group:
            factors = {}
            for key, weight in WEIGHTS.items():
                raw = finite(row.get(key))
                pct = percentile(raw, populations[key])
                factors[key] = {
                    "raw": raw,
                    "percentile": pct,
                    "weight": weight,
                    "contribution": weight * pct if pct is not None else None,
                }
            coverage = sum(
                f["weight"] for f in factors.values() if f["percentile"] is not None
            )
            total = sum(
                f["contribution"]
                for f in factors.values()
                if f["contribution"] is not None
            )
            scored.append(
                {
                    **row,
                    "ts": as_of,
                    "strength": total / coverage if coverage else None,
                    "factor_coverage": round(coverage, 8),
                    "score_method": "eight_factor_v1"
                    if coverage > 0.999999
                    else "partial_factor_v1",
                    "factors": factors,
                }
            )
        valid = sorted(
            (r for r in scored if r["strength"] is not None),
            key=lambda r: (-r["strength"], r["board_code"]),
        )
        rank_by_code = {}
        previous_score, previous_rank = None, 0
        for idx, row in enumerate(valid, 1):
            rank = previous_rank if row["strength"] == previous_score else idx
            rank_by_code[row["board_code"]] = rank
            previous_score, previous_rank = row["strength"], rank
        for row in scored:
            row["rank"] = rank_by_code.get(row["board_code"])
            output.append(row)
    return output
