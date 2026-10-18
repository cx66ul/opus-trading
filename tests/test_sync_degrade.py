# -*- coding: utf-8 -*-
"""★ 日线多源降级 的守护测试

★★★ 这个模块防的是「主源整体挂掉，而降级代码一行都没执行」
----------------------------------------------------------------
实测 2026-10-10：`web.ifzq.gtimg.cn`（腾讯前复权日线）**整源返回 HTTP 501**。
原`fetch_daily_robust()` 只处理「返回根数不足」：

    bars = get_adapter("tencent").fetch_daily(...)   # ← 抛 SourceError
    if len(bars) >= MIN_USABLE:                     # ← 永远到不了
        return bars, "tencent"
    bars2 = get_adapter("sina").fetch_daily(...)     # ← 降级代码没机会跑

⇒ 全量同步 5571 只里 **5051 只失败**，而新浪**明明能取到数据**
（我手工验证：001369 新浪12 根，最后一根 2026-10-09）。

为什么这类 bug 特别贵：
  · 错误信息是 `tencent HTTP 501`，看着像「这只票没数据」⇒ 归因错方向
  · 主源健康检查会显示「腾讯正常」（`/api/tags` 之类与日线端点无关）
  · **降级逻辑看起来是写好的**，只是从未被执行过

另一个同源缺陷：`MIN_USABLE=60` 而CLI 允许 `--count 12`
⇒ 即使腾讯完全成功，12 根 < 60 也被判「根数不足」
⇒ 静默降级到**不复权**新浪，前复权口径丢失且**日志只写「降级到备源」不报错**。

运行： pytest tests/test_sync_degrade.py -v
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SYNC_PY = ROOT / "scripts" / "sync.py"


def _load_sync_module():
    """以模块方式加载 scripts/sync.py（它不是包内模块）。"""
    import importlib.util

    sys.argv = ["sync"]                      # 避免 argparse 读 pytest 的 argv
    spec = importlib.util.spec_from_file_location("hawk_sync_script", SYNC_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bar(day: str, close: float = 10.0) -> dict:
    return {"trade_date": day, "open": close, "close": close,
            "high": close, "low": close, "volume": 1000.0}


class _FakeAdapter:
    """按源名返回预置结果或抛异常。"""

    def __init__(self, table: dict):
        self.table = table
        self.calls: list[str] = []

    def fetch_daily(self, code: str, count: int = 250) -> list[dict]:
        self.calls.append(code)
        item = self.table[code]
        if isinstance(item, Exception):
            raise item
        return list(item)


@pytest.fixture
def fake_sources(monkeypatch):
    """接管 bootstrap / get_adapter，使测试不发真实网络请求。"""
    table: dict = {}

    def _install():
        fake = _FakeAdapter(table)

        def _get_adapter(name):
            return _FakeAdapter(table[name] if isinstance(table[name], dict)
                                else {})
        return fake, _get_adapter

    return table, _install


# ═══════════════════════════════════════════════ 核心行为
class TestDegradeOnException:
    """★ 主源抛异常时也必须降级。"""

    def test_tencent_raises_falls_back_to_sina(self, monkeypatch):
        mod = _load_sync_module()
        bars = [_bar("2026-09-30"), _bar("2026-10-08"), _bar("2026-10-09")]

        calls: list[str] = []

        class A:
            def __init__(self, name, result):
                self.name, self.result = name, result

            def fetch_daily(self, code, count=250):
                calls.append(self.name)
                if isinstance(self.result, Exception):
                    raise self.result
                return list(self.result)

        adapters = {
            "tencent": A("tencent", RuntimeError("tencent HTTP 501")),
            "sina": A("sina", bars),
        }
        monkeypatch.setattr(mod, "bootstrap", lambda: None)
        monkeypatch.setattr(mod, "get_adapter", lambda n: adapters[n])

        got, src = mod.fetch_daily_robust("001369", count=250)
        assert src == "sina", f"主源抛异常时未降级，返回 {src}"
        assert len(got) == 3
        assert "sina" in calls, "备源根本没被调用"

    def test_both_sources_fail_reports_both(self, monkeypatch):
        """★ 两源都失败时，报错必须包含**两边**的原因。

        只报最后一个源的错误会把「主源挂了」误导成「这只票没数据」。
        """
        mod = _load_sync_module()

        class A:
            def __init__(self, msg):
                self.msg = msg

            def fetch_daily(self, code, count=250):
                raise RuntimeError(self.msg)

        adapters = {"tencent": A("HTTP 501"), "sina": A("HTTP 502")}
        monkeypatch.setattr(mod, "bootstrap", lambda: None)
        monkeypatch.setattr(mod, "get_adapter", lambda n: adapters[n])

        with pytest.raises(Exception) as ei:
            mod.fetch_daily_robust("600519", count=250)
        msg = str(ei.value)
        assert "501" in msg and "502" in msg, f"报错未含两源原因: {msg}"
        assert "tencent" in msg and "sina" in msg

    def test_tencent_ok_returns_tencent(self, monkeypatch):
        """主源正常时不应白跑备源。"""
        mod = _load_sync_module()
        bars = [_bar(f"2026-09-{d:02d}") for d in range(1, 31)] * 4
        calls: list[str] = []

        class A:
            def __init__(self, name, result):
                self.name, self.result = name, result

            def fetch_daily(self, code, count=250):
                calls.append(self.name)
                return list(self.result)

        adapters = {"tencent": A("tencent", bars), "sina": A("sina", [])}
        monkeypatch.setattr(mod, "bootstrap", lambda: None)
        monkeypatch.setattr(mod, "get_adapter", lambda n: adapters[n])

        got, src = mod.fetch_daily_robust("600519", count=250)
        assert src == "tencent" and len(got) >= 60
        assert "sina" not in calls, "主源正常仍去打备源（浪费且可能触发限流）"


class TestCountLowerBound:
    """★ `--count` 低于 MIN_USABLE 会静默丢掉前复权口径。"""

    def _sync_daily_capture(self, monkeypatch, mod, count):
        """跑 sync_daily 但拦掉网络与写库，抓取实际使用的 count。"""
        used: dict = {}

        def fake_fetch(code, count=250):
            used["count"] = count
            return [_bar("2026-10-09")], "tencent"

        monkeypatch.setattr(mod, "fetch_daily_robust", fake_fetch)
        monkeypatch.setattr(mod.duck, "upsert_kline_daily", lambda b: None)
        monkeypatch.setattr(mod.duck, "set_watermark", lambda *a, **k: None)
        monkeypatch.setattr(mod.duck, "table_counts", lambda: {"kline_daily": 0})
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
        mod.sync_daily(["600519"], count=count, batch=1)
        return used["count"]

    def test_count_below_min_usable_is_raised(self, monkeypatch):
        mod = _load_sync_module()
        got = self._sync_daily_capture(monkeypatch, mod, count=12)
        assert got >= 60, f"--count=12 未被提升，实际用了 {got}"

    def test_count_above_min_usable_untouched(self, monkeypatch):
        mod = _load_sync_module()
        got = self._sync_daily_capture(monkeypatch, mod, count=250)
        assert got == 250

    def test_guard_present_in_source(self):
        """CLI 层也应有提示，避免用户以为--count 生效了。"""
        src = SYNC_PY.read_text(encoding="utf-8")
        assert "MIN_USABLE" in src
        assert "静默降级" in src or "不复权" in src


# ═══════════════════════════════════════════════ 静态守护
class TestSourceStructure:
    def test_syntax_valid(self):
        ast.parse(SYNC_PY.read_text(encoding="utf-8"))

    def test_each_source_wrapped_in_try(self):
        """★ 两个源都必须各自被 try 包住，而不是只有第一个。"""
        tree = ast.parse(SYNC_PY.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "fetch_daily_robust")
        # fetch_daily 的调用点数量：应在_try 内部（1 处定义）
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "fetch_daily"]
        assert len(calls) == 1, (
            f"fetch_daily 应只在 _try 内出现一次，实际 {len(calls)} 处"
            "⇒ 有源没有被 try 包住")

    def test_no_bare_adapter_call_in_main_path(self):
        """函数体内不得出现裸的 get_adapter(...).fetch_daily(...)"""
        src = SYNC_PY.read_text(encoding="utf-8")
        assert 'get_adapter("tencent").fetch_daily(' not in src, (
            "主源调用绕过了 try 包装 ⇒ 异常会直接上抛，降级不执行")

    def test_min_usable_defined(self):
        src = SYNC_PY.read_text(encoding="utf-8")
        assert "MIN_USABLE = 60" in src


# ═══════════════════════════════════════════════ 变异敏感性
class TestMutationSensitivity:
    """★ 只有真变异过，才知道这些断言不是摆设。"""

    def test_assert_tencent_called_first(self, monkeypatch):
        """反例：若主源被跳过（先试备源），本断言应失败。"""
        mod = _load_sync_module()
        order: list[str] = []

        class A:
            def __init__(self, name):
                self.name = name

            def fetch_daily(self, code, count=250):
                order.append(self.name)
                if self.name == "tencent":
                    raise RuntimeError("boom")
                return [_bar("2026-10-09")]

        monkeypatch.setattr(mod, "bootstrap", lambda: None)
        monkeypatch.setattr(mod, "get_adapter", lambda n: A(n))
        mod.fetch_daily_robust("600519", count=250)
        assert order == ["tencent", "sina"], f"实际调用顺序 {order}"