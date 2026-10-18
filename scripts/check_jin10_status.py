"""Read-only live diagnosis: source implementation, database and public endpoints."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.domain.news.jin10 import Jin10Source

now = datetime.now(timezone(timedelta(hours=8)))
result = {"checked_at": now.isoformat(), "database": {}, "fetch": {}, "http": []}
con = sqlite3.connect(f"file:{(ROOT / 'data/hawk.sqlite').as_posix()}?mode=ro", uri=True)
for table, time_field in (("raw_news", "fetched_at"), ("news", "created_at")):
    result["database"][table] = [dict(zip(("source", "count", "latest_saved", "latest_news"), r))
        for r in con.execute(f"SELECT source,count(*),max({time_field}),max(ts) FROM {table} GROUP BY source")]
    result["database"][table + "_jin10_count"] = con.execute(
        f"SELECT count(*) FROM {table} WHERE lower(source) LIKE ? OR source LIKE ?", ("%jin10%", "%金十%")
    ).fetchone()[0]
con.close()
try:
    rows = Jin10Source().fetch(now, 3)
    result["fetch"] = {"ok": True, "count": len(rows)}
except Exception as exc:
    result["fetch"] = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
for url in ("https://www.jin10.com/", "https://flash-api.jin10.com/get_flash_list"):
    try:
        r = httpx.get(url, timeout=12, follow_redirects=True)
        result["http"].append({"url": url, "status": r.status_code,
                               "content_type": r.headers.get("content-type"), "bytes": len(r.content)})
    except Exception as exc:
        result["http"].append({"url": url, "error_type": type(exc).__name__, "message": str(exc)})
result["conclusion"] = "not_operational" if not result["fetch"]["ok"] else "inspect_data"
out = ROOT / "docs/jin10-check-20261011.json"
out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
