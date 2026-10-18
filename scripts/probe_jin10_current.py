"""Capture public homepage assets and rendered flash data, without login or private data."""
import json
import re
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples/jin10-20261011"
OUT.mkdir(parents=True, exist_ok=True)
with httpx.Client(timeout=20, follow_redirects=True) as client:
    r = client.get("https://www.jin10.com/")
    (OUT / "homepage.html").write_text(r.text, encoding="utf-8")
    print(r.status_code, r.text[:100])
    scripts = re.findall(r'<script[^>]+src=[\"\x27]([^\"\x27]+)', r.text)
    print(json.dumps(scripts))
    from urllib.parse import urljoin
    for i, url in enumerate(scripts):
        url = urljoin(str(r.url), url)
        try:
            s = client.get(url)
            if s.status_code == 200:
                (OUT / f"asset-{i}.js").write_text(s.text, encoding="utf-8")
                print(i, url, len(s.content))
        except Exception as exc:
            print(type(exc).__name__)
