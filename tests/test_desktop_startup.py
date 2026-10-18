"""Concurrent dashboard/recommendation queries must not consume each other's results."""
from concurrent.futures import ThreadPoolExecutor
import threading
import duckdb
from backend.data import duck


def test_readonly_query_results_are_isolated_by_thread(tmp_path, monkeypatch):
    db_path = tmp_path / "hawk.duckdb"
    duckdb.connect(str(db_path)).close()
    monkeypatch.setattr(duck, "DB_PATH", db_path)
    monkeypatch.setattr(duck, "_conn", None)
    monkeypatch.setattr(duck, "_ro_conn", None)
    if hasattr(duck, "_ro_cursors"):
        monkeypatch.setattr(duck, "_ro_cursors", [])
    first_executed = threading.Event()
    second_executed = threading.Event()

    def first():
        conn = duck.get_ro_conn()
        conn.execute("SELECT 101")
        first_executed.set()
        assert second_executed.wait(5)
        return conn.fetchone()

    def second():
        assert first_executed.wait(5)
        conn = duck.get_ro_conn()
        conn.execute("SELECT 202")
        result = conn.fetchone()
        second_executed.set()
        return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(first), pool.submit(second)
            assert one.result(timeout=10) == (101,)
            assert two.result(timeout=10) == (202,)
    finally:
        duck.release_ro_conn()


def test_ready_does_not_open_or_scan_database(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import webapi

    def forbidden():
        raise AssertionError("readiness must not access data")
    monkeypatch.setattr(duck, "get_ro_conn", forbidden)
    with TestClient(webapi.app) as client:
        response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["service"] == "hawk-webapi"
    assert response.json()["ready"] is True
