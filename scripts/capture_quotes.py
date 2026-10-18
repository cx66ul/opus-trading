"""Persist one minute of the held/watchlist pool without sending notifications."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.tracking import CST, capture_quotes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codes", help="comma-separated six-digit codes; defaults to tracked pool"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    result = capture_quotes(datetime.now(CST), codes, dry_run=args.dry_run)
    result.pop("rows")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
