# -*- coding: utf-8 -*-
"""分钟线同步进度工具测试。

★ 为什么这组测试重要：
  同步进程持有 DuckDB 写锁时，所有常规读库手段全部失效
  （connect 抛 IOException / cp 报 Device busy / open WAL 报 PermissionError）。
  所以本工具**只能**靠文件字节数 + 未被锁的 SQLite + 实测换算系数来估算。
  这些系数一旦被「顺手改个整数」，进度就会算错几个百分点甚至超过 100%，
  而它**不会报错** —— 只能靠测试锁住。

运行： pytest tests/test_minute_progress.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "mp", ROOT / "scripts" / "minute_progress.py")
mp = importlib.util.module_from_spec(_spec)
sys.modules["mp"] = mp
_spec.loader.exec_module(mp)


# ================================================================ 换算系数
class TestCoefficients:
    def test_bytes_per_row_is_measured_value(self):
        """★★ 81.8 字节/行 是实测值（48,200 行真实结构数据）。

        拍成 100 或 120 会让进度偏差 20%~45%，且**不会报错**。
        """
        assert mp.BYTES_PER_ROW == pytest.approx(81.8, abs=0.5)

    def test_rows_per_code_matches_tencent_reality(self):
        """★ m30/m60 各 482 根 —— count=720 也只给 482（接口硬限制）。"""
        assert mp.ROWS_PER_CODE == 964

    def test_coefficients_are_positive(self):
        """★ 系数为 0 或负会让进度变成 0% 或负数（静默错误）。"""
        assert mp.BYTES_PER_ROW > 0
        assert mp.ROWS_PER_CODE > 0

    def test_coefficients_consistent_with_tencent_limit(self):
        """★★ 交叉约束：ROWS_PER_CODE 必须 = 2 × 482。

        腾讯 m30 与 m60 的单次返回上限都是 482 根，
        若接口将来变了，这里应该失败而不是默默算错。
        """
        assert mp.ROWS_PER_CODE % 2 == 0
        assert mp.ROWS_PER_CODE / 2 == 482


# ================================================================ 采样
class TestSample:
    def test_returns_expected_keys(self):
        r = mp.sample_progress()
        for k in ("ts", "db_bytes", "wal_bytes", "target_codes",
                  "codes_done", "pct", "codes_left"):
            assert k in r, f"缺少 {k}"

    def test_no_crash_when_files_missing(self, monkeypatch, tmp_path):
        """★ 文件不在时返回 error 而不是抛异常 —— 这是监控工具，崩了就白装。"""
        monkeypatch.setattr(mp, "DB", tmp_path / "nope.duckdb")
        monkeypatch.setattr(mp, "WAL", tmp_path / "nope.wal")
        r = mp.sample_progress()
        assert "error" in r

    def test_target_count_zero_is_error(self, monkeypatch):
        """★ 目标数为 0（名单读不到）时必须报错，不能报 0%。"""
        monkeypatch.setattr(mp, "target_count", lambda: 0)
        r = mp.sample_progress()
        assert "error" in r

    def test_pct_never_exceeds_100(self, monkeypatch):
        """★★ 进度不得超过 100%（否则读起来像已完成）。"""
        monkeypatch.setattr(mp, "target_count", lambda: 100)
        monkeypatch.setattr(mp, "db_bytes", lambda: (mp.BASE_DB_BYTES + 10**9, 0))
        r = mp.sample_progress()
        assert r["pct"] <= 100.0

    def test_negative_added_bytes_clamped(self, monkeypatch):
        """★ 基线大于当前文件（进程重启过）时不能算出负进度。"""
        monkeypatch.setattr(mp, "target_count", lambda: 4601)
        monkeypatch.setattr(mp, "db_bytes",
                            lambda: (mp.BASE_DB_BYTES - 50_000_000, 0))
        r = mp.sample_progress()
        assert r["codes_done"] >= 0
        assert r["pct"] >= 0.0

    def test_codes_left_consistent(self, monkeypatch):
        monkeypatch.setattr(mp, "target_count", lambda: 4601)
        monkeypatch.setattr(mp, "db_bytes",
                            lambda: (mp.BASE_DB_BYTES + 100 * mp.BYTES_PER_ROW
                                     * mp.ROWS_PER_CODE, 0))
        r = mp.sample_progress()
        assert r["codes_done"] == pytest.approx(100, abs=2)
        assert r["codes_left"] == pytest.approx(4501, abs=2)


# ================================================================ 速率
class TestRate:
    def test_rate_computed(self):
        prev = {"total_bytes": 0}
        cur = {"total_bytes": int(mp.BYTES_PER_ROW * mp.ROWS_PER_CODE * 100)}
        # 1 小时推进 100 只 -> 100 只/小时
        r = mp.rate_per_hour(prev, cur, 3600.0)
        assert r == pytest.approx(100, rel=0.02)

    def test_rate_none_on_zero_dt(self):
        assert mp.rate_per_hour({"total_bytes": 0},
                                {"total_bytes": 100}, 0) is None

    def test_rate_none_when_shrank(self):
        """★ 字节数变少 ⇒ 进程重启过，返回 None 而非负速率。"""
        assert mp.rate_per_hour({"total_bytes": 100},
                                {"total_bytes": 0}, 60) is None

    def test_rate_none_when_exactly_zero_progress(self):
        """★★ 区分 `<=0` 与 `<0` 的**唯一**有效输入是 d_bytes == 0。

        ★ 这条是上一版的补充（教训）：
          我原以为「跨 checkpoint」能测出 `<=0` 与 `<0` 的差别，
          变异测试却没抓到 —— 因为 checkpoint 实测让总量**下降 1.78%**
          （负数），而 `<0` 和 `<=0` 对负数行为完全一样，**测不出差别**。
          只有「字节数完全没变」这一情形才能区分：
            <= 0 -> None（正确：无进度不该报速度）
            <  0 -> 0.0 只/小时（错误：会被读成「卡住了」）
        """
        r = mp.rate_per_hour({"total_bytes": 1_000_000},
                             {"total_bytes": 1_000_000}, 60.0)
        assert r is None, \
            f"零推进应返回 None（不能报 0 只/小时），实得 {r}"

    def test_wal_checkpoint_shrinks_total_temporarily(self):
        """★★ 实测：checkpoint 会让「DB+WAL 之和」**短暂下降**。

        2026-10-09 09:21 那次实测：
          checkpoint 前  DB 355,217,408 + WAL 15,592,202 = 370,809,610
          checkpoint 后  DB 361,508_864 + WAL  2,711,888 = 364,220,752
          差 **-6,588,858 字节（-1.78%）**

        原因：WAL 里是未压缩的原始行组，checkpoint 时 DuckDB 压缩后写入
        主文件并清空 WAL ⇒ 总量必然下降。

        ⇒ 跨越 checkpoint 的窗口必须返回 None，否则会把「压缩」当成减速。
        """
        before = 355_217_408 + 15_592_202
        after = 361_508_864 + 2_711_888
        assert after < before, \
            "实测 checkpoint 后总量会下降（本用例的前提）"
        r = mp.rate_per_hour({"total_bytes": before},
                             {"total_bytes": after}, 60.0)
        assert r is None, \
            f"跨越 checkpoint 应返回 None，实得 {r}（会把 -1.78% 误当减速）"

    def test_rate_across_multiple_samples_is_robust(self):
        """★ 真实用法是 --watch 连续采样，不该靠单点差值。"""
        import statistics
        totals = [0, 30_000_000, 60_100_000, 55_000_000,   # 含一次 checkpoint
                  90_300_000, 120_500_000]
        rates = []
        for a, b in zip(totals, totals[1:]):
            r = mp.rate_per_hour({"total_bytes": a},
                                 {"total_bytes": b}, 60.0)
            if r is not None:
                rates.append(r)
        # 有效速率应为正，且数量少于总窗口数（checkpoint 那个被正确跳过）
        assert rates and all(r > 0 for r in rates)
        assert len(rates) < len(totals) - 1


# ================================================================ 口径
class TestMethodology:
    def test_uses_sqlite_not_duckdb(self):
        """★★ 必须走 SQLite —— 它不被 DuckDB 写锁阻塞。

        实测踩过：同步期间 `cp hawk.duckdb` 报 Device or resource busy、
        `open(...wal)` 报 PermissionError、duckdb.connect 抛 IOException。
        ★ 判据是「**代码里不 import / 不调用 duckdb**」，
          而不是「源码里不出现 duckdb 字样」——
          文档必须记录 `duckdb.connect` 会抛 IOException 这个症状，
          路径名 `hawk.duckdb` 也到处都是。所以用 AST 判定真正加载了没有。
        """
        import ast
        tree = ast.parse((ROOT / "scripts" / "minute_progress.py")
                         .read_text(encoding="utf-8"))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        assert "duckdb" not in mods, \
            f"进度工具不得加载 duckdb（会被写锁阻塞），实际 import: {mods}"
        assert "sqlite3" in mods
        assert "statistics" in mods, "watch 模式需 statistics 取中位数"

    def test_docstring_records_lock_symptoms(self):
        """★ 文档必须记下「锁住时哪些手段失效」，否则后人会重新踩一遍。"""
        src = (ROOT / "scripts" / "minute_progress.py").read_text(
            encoding="utf-8")
        for sym in ("Device or resource busy", "PermissionError",
                    "IOException"):
            assert sym in src, f"文档未记录锁住的症状 {sym}"

    def test_target_sql_matches_sync_script(self):
        """★ 目标名单口径必须与 sync_minutes.py --all 完全一致。

        不同步就会算出「完成度 100% 但实际只同步了一部分」。
        """
        sync_src = (ROOT / "scripts" / "sync_minutes.py").read_text(
            encoding="utf-8")
        for frag in ("is_st=0", "is_suspend=0",
                     "float_mv BETWEEN 2e9 AND 2e11"):
            assert frag in mp.TARGET_SQL, f"进度口径缺 {frag}"
            assert frag in sync_src, f"同步脚本已变更 {frag}，需同步更新进度工具"

    def test_docstring_states_estimate_not_exact(self):
        """★ 必须写明这是**估算**及其误差来源，不能让人当精确值用。"""
        src = (ROOT / "scripts" / "minute_progress.py").read_text(
            encoding="utf-8")
        assert "估算" in src
        assert "误差" in src or "局限" in src

    def test_warns_about_baseline_calibration(self):
        """★ 基线是手工维护的一次性值，进程重启后会算错 —— 必须在输出里提示。

        提醒放在 `main()` 而非 `fmt()`：`--watch` 模式每 60 秒刷一次，
        放进每行会让输出被同一句提示刷屏。
        """
        import io
        import contextlib
        sys_argv = sys.argv
        sys.argv = ["minute_progress"]          # main() 会 argparse，必须隔离
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                mp.main()
        finally:
            sys.argv = sys_argv
        out = buf.getvalue()
        assert "基线" in out and ("重新校准" in out or "重启" in out), \
            "单次输出必须提示基线需与本次进程对应"
        src = (ROOT / "scripts" / "minute_progress.py").read_text(
            encoding="utf-8")
        assert "BASE_DB_BYTES" in src

    def test_running_detected_via_wal_mtime(self):
        """★ 判活跃用 WAL mtime（进程列表在沙箱里查不到）。"""
        import inspect
        src = inspect.getsource(mp.is_running)
        assert "mtime" in src
        assert "tasklist" not in src
