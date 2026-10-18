# -*- coding: utf-8 -*-
"""pytest 全局配置。

★★ 为什么需要 `duckdb_locked` 标记（实测踩过）：

  DuckDB 是**单写者**模型（文件级独占锁）。只要另一个进程
  （典型：全市场分钟线同步、行情同步）在写 `data/hawk.duckdb`，
  本进程 `duckdb.connect(..., read_only=True)` 就会抛
  `IOException: 另一个程序正在使用此文件`。

  现象：同步任务一跑，全量测试里依赖真实行情的用例集体变红，
  但那些用例本身完全正常。这会让人误以为是代码改坏了 ——
  实测就曾因此怀疑刚修的三买逻辑。

  处理方式是**显式 skip 并给出可读原因**，而不是：
    · 静默通过（会让这6 条永久失去防护，等于没测）
    · 直接失败（噪声淹没真实问题）

  `skip` 在报告里可见，信号是「被跳过」而非「已验证」，
  不会造成虚假的安全感。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def forbid_real_smtp(monkeypatch):
    """回归测试不得连接用户真实 SMTP；发送器用例须显式提供模拟连接。"""
    import smtplib
    monkeypatch.setenv("OPUS_MAIL_DISABLE", "1")
    monkeypatch.setenv("OPUS_INTRADAY_DISABLE", "1")

    def blocked(*args, **kwargs):
        pytest.fail("测试试图连接真实 SMTP；请用模拟发送器隔离外部副作用", pytrace=False)

    monkeypatch.setattr(smtplib.SMTP, "connect", blocked)


def _network_available() -> bool:
    """探测网络是否可用（数据层测试需要真实接口）。"""
    try:
        import httpx
        r = httpx.Client(trust_env=False, timeout=3.0)
        r.get("https://qt.gtimg.cn/q=sh000001")
        return True
    except Exception:
        return False


def _duckdb_is_locked() -> tuple[bool, str]:
    """探测行情库是否被其他进程占用。返回 (locked, 原因)。"""
    try:
        import duckdb
        from backend.data.duck import DB_PATH
    except Exception as exc:                      # pragma: no cover
        return False, f"duckdb 不可用: {exc}"

    if not DB_PATH.exists():
        return False, "行情库不存在"
    try:
        con = duckdb.connect(str(DB_PATH), read_only=True)
        con.close()
        return False, ""
    except Exception as exc:
        return True, f"行情库被其他进程占用（通常是同步任务在跑）：{exc}"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "duckdb_locked: 需要独占读取行情库；被同步进程占用时自动跳过",
    )
    config.addinivalue_line(
        "markers",
        "needs_network: 需要访问真实数据源；网络不可用时自动跳过",
    )


def pytest_collection_modifyitems(config, items):
    """按环境可用性降级为 skip（原因显式可见，不伪装成通过）。"""
    locked, reason = _duckdb_is_locked()
    if locked:
        skip = pytest.mark.skip(reason=f"[行情库被占用] {reason}")
        for item in items:
            if "duckdb_locked" in item.keywords:
                item.add_marker(skip)
    # ★ 网络探测只做一次（否则每个用例都等超时，跑到后面极慢）
    need_net = [it for it in items if "needs_network" in it.keywords]
    if need_net and not _network_available():
        skip = pytest.mark.skip(reason="[网络不可用] 无法访问真实数据源")
        for item in need_net:
            item.add_marker(skip)

@pytest.fixture(scope="module")
def client():
    """FastAPI 测试客户端（共享给所有需要打API 的测试文件）。

    ★ 放conftest 而不是各文件各自定义 ——
      `test_webapi.py` 里原本有个局部 fixture，`test_llm_audit.py` 再定义一份
      就成了两份口径（改路由前缀时只改一处，另一处静默失效）。
    """
    from fastapi.testclient import TestClient

    from backend.api import webapi

    return TestClient(webapi.app)
