"""Read-only status report for the current continuation milestone."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.data import duck  # noqa: E402
from backend.services.market_context import CST, board_context, market_context  # noqa: E402


def main() -> int:
    conn = duck.get_ro_conn()
    now = datetime.now(CST)
    data = {
        "as_of": now.isoformat(),
        "tables": duck.table_counts(),
        "market_context": market_context(conn, now),
        "latest_boards": conn.execute(
            """SELECT type, count(*), min(factor_coverage), max(factor_coverage),
                      min(ts), max(ts), count(*) FILTER (WHERE NOT source_complete)
               FROM board_strength WHERE ts=(SELECT max(ts) FROM board_strength)
               GROUP BY type ORDER BY type"""
        ).fetchall(),
        "board_stock_mappings": len(board_context(conn, now)),
        "quotes": conn.execute(
            "SELECT count(*), count(DISTINCT code), max(ts), max(source_ts) FROM quote_snapshot"
        ).fetchone(),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
