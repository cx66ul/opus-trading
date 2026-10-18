import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from backend.domain.news import jin10_http as J
from backend.services import news_collection as C

AS_OF = datetime.fromisoformat("2026-10-11T15:00:00+08:00")


def row(**changes):
    item = dict(id="20261011143300309800", time="2026-10-11 14:33:00",
                data={"title": "标题", "content": "<b>公开</b>&amp;正文<br>第二行<script>bad()</script>"},
                extras={"ad": False}, important=1, kinds=[{"id": 600000}])
    item.update(changes)
    return item


def mock_api(monkeypatch, body=None, status=200, error=None):
    client_class = httpx.Client
    def handler(request):
        assert str(request.url) == J.ENDPOINT
        assert request.headers["x-app-id"] == J.HEADERS["x-app-id"]
        assert "cookie" not in request.headers
        if error:
            raise error
        return httpx.Response(status, json=body)
    monkeypatch.setattr(J.httpx, "Client", lambda **kw: client_class(
        **kw, transport=httpx.MockTransport(handler)))


def test_public_text_and_skip_counts(monkeypatch):
    items = [row(), row(data={"lock": True, "content": "付费预览"}),
             row(extras={"ad": True}), row(data={"content": ""}), row(time="bad"),
             row(data={"exclusive_to": ["vip"], "content": "预览"})]
    mock_api(monkeypatch, {"status": 200, "data": items})
    source = J.Jin10Source()
    out = source.fetch(AS_OF)
    assert len(out) == 1
    assert out[0]["content"] == "公开&正文 第二行"
    assert out[0]["ts"] == "2026-10-11T14:33:00+08:00"
    assert out[0]["related"] == []
    assert out[0]["tags"] == ["重要"]
    assert json.loads(out[0]["raw"]) == items[0]
    assert source.last_stats == dict(received=6, locked=2, ads=1, empty=1, invalid=1)


@pytest.mark.parametrize("body", [{"status": 403, "data": []}, {"status": 200, "data": {}},
                                      {"status": 200, "data": [None]}, [],
                                      {"status": 200, "data": [{"data": {}}]}])
def test_schema_and_business_errors_are_explicit(monkeypatch, body):
    mock_api(monkeypatch, body)
    with pytest.raises(ValueError):
        J.Jin10Source().fetch(AS_OF)


@pytest.mark.parametrize("status", [429, 502])
def test_http_failure(monkeypatch, status):
    mock_api(monkeypatch, {}, status)
    with pytest.raises(httpx.HTTPStatusError):
        J.Jin10Source().fetch(AS_OF)


def test_timeout(monkeypatch):
    mock_api(monkeypatch, error=httpx.ReadTimeout("timed out"))
    with pytest.raises(httpx.ReadTimeout):
        J.Jin10Source().fetch(AS_OF)


def test_real_response_fixture(monkeypatch):
    fixture = Path(__file__).parents[1] / "samples/jin10-20261011/public-response.json"
    mock_api(monkeypatch, json.loads(fixture.read_text(encoding="utf-8")))
    source = J.Jin10Source()
    items = source.fetch(AS_OF)
    assert len(items) == 19
    assert source.last_stats["locked"] == 2
    assert source.last_stats["invalid"] == 0
    assert any("工业生产" in i["content"] for i in items)


def test_repeat_concurrent_revision_and_title(tmp_path):
    path = tmp_path / "test.sqlite"
    item = J.Jin10Source().normalize(row())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: C.save_items([item, item], AS_OF, path), range(4)))
    assert sum(r["saved"] for r in results) == 1
    assert sum(r["duplicates"] for r in results) == 7
    item["raw"] = "metadata only changed"
    assert C.save_items([item], AS_OF, path)["saved"] == 0
    item["content"] = "修正正文"
    assert C.save_items([item], AS_OF, path)["saved"] == 1
    with sqlite3.connect(path) as db:
        values = db.execute("SELECT payload FROM raw_news").fetchall()
    assert len(values) == 2
    assert json.loads(values[0][0])["title"] == "标题"


def test_existing_rows_seed_receipts(tmp_path):
    path = tmp_path / "test.sqlite"
    item = J.Jin10Source().normalize(row())
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE raw_news(id INTEGER PRIMARY KEY,source,ts,payload,fetched_at,parsed)")
        db.execute("INSERT INTO raw_news VALUES(1,?,?,?,?,1)",
                   (item["source"], item["ts"], json.dumps(C._payload(item)), AS_OF.isoformat()))
    assert C.save_items([item], AS_OF, path)["duplicates"] == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT parsed FROM raw_news").fetchone()[0] == 1


def test_error_status_keeps_last_success_and_history(monkeypatch, tmp_path):
    path = tmp_path / "test.sqlite"
    source = J.Jin10Source()
    monkeypatch.setattr(source, "fetch", lambda *a: [source.normalize(row())])
    assert C.collect_source(source, AS_OF, db_path=path)["state"] == "ok"
    def fail(*args):
        raise httpx.ReadTimeout("timeout")
    monkeypatch.setattr(source, "fetch", fail)
    result = C.collect_source(source, AS_OF, db_path=path)
    assert result["state"] == "error"
    assert result["last_success"] == AS_OF.isoformat()
    status = C.source_status(path)
    assert status["sources"][0]["raw_total"] == 1
    assert "timeout" in status["sources"][0]["error"]
    assert status["recent"][0]["content"]


def test_disable_and_stop_collector(monkeypatch, tmp_path):
    collector = C.NewsCollector(tmp_path / "test.sqlite")
    monkeypatch.setenv("OPUS_NEWS_AUTOCOLLECT", "0")
    collector.start()
    assert collector.thread is None
    monkeypatch.setenv("OPUS_NEWS_AUTOCOLLECT", "1")
    monkeypatch.setattr(C, "collect_source", lambda *a, **kw: pytest.fail("stop must cancel startup wait"))
    collector.start()
    collector.stop()
    assert not collector.thread.is_alive()
    assert not collector.status()["running"]


def test_api_lifecycle_does_not_wait_for_http(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from backend.api import webapi
    monkeypatch.setenv("OPUS_NEWS_AUTOCOLLECT", "0")
    monkeypatch.setattr(webapi, "SQLITE_PATH", tmp_path / "empty.sqlite")
    with TestClient(webapi.app) as client:
        result = client.get("/api/news/sources")
        assert result.status_code == 200
        assert result.json()["running"] is False
        assert result.json()["sources"] == []
