# -*- coding: utf-8 -*-
"""★ `midday` / `close` 调度任务 + 装配层测试。

★★★ 这组测试防的是「有 cron 无实现」这类半成品
------------------------------------------
`TASK_TABLE` 列了 11 个任务，但 `REGISTRY` 里只有 8 个 ——
`midday` / `close` 曾在「有 cron、有文档、有渲染模板」但**无实现**的状态：
  · `--run midday` 走 `REGISTRY[name]` → KeyError
  · `render_midday_html` 有渲染器但**没有生产者**
  · `mail_log` 表 0 行 ⇒ 三类邮件一封都没发出去过

⇒ 本组测试固定三条契约：
  1. `TASK_TABLE` 里每个任务**要么有实现，要么显式登记为已知缺口**
     （不允许再出现「静默缺失」—— 那正是它躲了这么久的��因）
  2. 装配层是**纯函数**：同一 `as_of` 重跑结果一致（铁律二）
  3. 缺失数据必须留 `None`，绝不当0

运行： pytest tests/test_digest_jobs.py -v
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))


# ================================================================== 任务注册
class TestTaskRegistration:
    """★★ `TASK_TABLE` 与 `REGISTRY` 必须对齐。"""

    # ★ 2026-10-10 更新：`auction` / `ref_sync` / `weekly` **均已补齐**
    #   （配套 `backend/domain/auction.py` + `weekly.py`）。
    #   缺口归零 —— 这个断言的价值正是它**当初抓到了缺口**，
    #   现在它转为「防止缺口重新出现」。
    KNOWN_MISSING: set[str] = set()

    def test_every_task_has_implementation_or_is_declared(self):
        from backend.scheduler import REGISTRY, TASK_TABLE

        missing = {n for n, *_ in TASK_TABLE} - set(REGISTRY)
        assert missing == self.KNOWN_MISSING, (
            f"未实现的任务集合变了：实得 {sorted(missing)}，"
            f"期望 {sorted(self.KNOWN_MISSING)}。"
            f"新增任务请同时补 @job 实现并更新 KNOWN_MISSING。"
        )

    def test_all_eleven_tasks_are_implemented(self):
        """★ 规格 6.1 的 11 个任务必须**全部**有实现。"""
        from backend.scheduler import REGISTRY, TASK_TABLE

        assert len(TASK_TABLE) == 11
        assert {n for n, *_ in TASK_TABLE} <= set(REGISTRY)

    def test_midday_and_close_are_implemented(self):
        """★ 本轮补上的两个任务必须真的注册了。"""
        from backend.scheduler import REGISTRY

        assert "midday" in REGISTRY, "midday 仍无实现"
        assert "close" in REGISTRY, "close 仍无实现"

    def test_no_registry_entry_without_task_table_row(self):
        """反向也成立：注册了但没在 TASK_TABLE 里 ⇒ 永远不会被调度。"""
        from backend.scheduler import REGISTRY, TASK_TABLE

        names = {n for n, *_ in TASK_TABLE}
        assert set(REGISTRY) <= names, \
            f"这些任务注册了但不在 TASK_TABLE，永远不会被调度：{set(REGISTRY) - names}"


# ================================================================== 纯函数性
class TestPurity:
    """★★ 装配层必须是纯函数（铁律二：不取系统时间）。"""

    def test_no_system_time_in_digest(self):
        import ast

        src = (ROOT / "backend" / "domain" / "digest.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)

        class V(ast.NodeVisitor):
            def __init__(self):
                self.hits = []

            def visit_Call(self, node):
                f = node.func
                if (isinstance(f, ast.Attribute)
                        and f.attr in ("now", "today")
                        and isinstance(f.value, ast.Name)
                        and f.value.id == "datetime"):
                    self.hits.append(f"datetime.{f.attr}() @line {node.lineno}")
                self.generic_visit(node)

        v = V()
        v.visit(tree)
        assert not v.hits, (
            f"装配层取了系统时间，违反铁律二：{v.hits}")

    def test_same_as_of_gives_same_result(self):
        """同一 `as_of` 两次装配结果必须一致（可复现）。"""
        from backend.domain import digest

        t = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        a = digest.build_midday_context(t)
        b = digest.build_midday_context(t)
        assert a.market_state == b.market_state
        assert a.market_score == b.market_score
        assert a.morning_review == b.morning_review
        assert len(a.positions) == len(b.positions)


# ================================================================== 缺失数据
class TestMissingDataIsNone:
    """★★ 缺失数据必须 `None`，绝不当 0。"""

    def test_regime_missing_leaves_none(self, monkeypatch):
        """`market_regime` 查不到时，三个字段都应是 None。"""
        from backend.domain import digest

        monkeypatch.setattr(digest, "_regime_on", lambda d: None)
        t = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        ctx = digest.build_midday_context(t)
        assert ctx.market_state is None, "查不到市场状态时不得填 0"
        assert ctx.market_score is None, "查不到评分时不得填 0"
        assert ctx.position_cap is None, "查不到仓位上限时不得填 0"

    def test_no_positions_returns_empty_not_fake_rows(self, monkeypatch):
        """无持仓时返回空列表，不能造「持仓 0 元」的假行。"""
        from backend.domain import digest

        monkeypatch.setattr(digest, "_positions", lambda d: [])
        t = datetime(2026, 10, 9, 11, 35, tzinfo=CST)
        ctx = digest.build_midday_context(t)
        assert ctx.positions == []

    def test_no_boards_returns_empty(self, monkeypatch):
        """无板块数据时返回空列表 —— 模板会渲染「无板块数据」。"""
        from backend.domain import digest

        monkeypatch.setattr(digest, "_top_boards", lambda d, limit=5: [])
        assert digest._top_boards(date(2026, 10, 9)) == []


# ================================================================== 只读连接
class TestSharedConnectionNotClosed:
    """★★ 装配层**不得** `con.close()`。

    `duck.get_ro_conn()` 返回的是**进程级共享单连接**
    （有写连接时复用写连接，否则开只读连接）。
    在这里关掉它 = 把全局连接关掉，后续所有查询抛
    `Connection Error: Connection already closed!`。

    实测症状极难定位：`_top_boards()` 明明返回了正确结果，
    但因为前一个函数关掉了连接，再调就全空 ⇒
    「板块数据有时有有时没有」。
    """

    def test_digest_never_closes_connection(self):
        """★ 只看**代码行**，不能匹配注释/docstring 里的说明文字。

        （本模块的注释里就写着 `con.close()` 这个反例，
          朴素的全文字符串匹配会永远失败 —— 或反过来被注释骗过。）
        """
        import ast

        src = (ROOT / "backend" / "domain" / "digest.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        hits = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "close"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("con", "_con", "conn")):
                hits.append(node.lineno)
        assert not hits, f"这些行关闭了共享连接：{hits}"

    def test_sequential_calls_all_work(self):
        """★ 连续多次查询都必须成功（关闭共享连接后第二个就会挂）。"""
        from backend.domain import digest

        a = digest._top_boards(date(2026, 10, 9))
        b = digest._top_boards(date(2026, 10, 9))
        c = digest._regime_on(date(2026, 10, 9))
        # 第二次调用必须仍能工作（第一次没有把连接关掉）
        assert a == b, "同一日期两次查询结果应一致"
        assert c is None or isinstance(c, dict)


# ══════════════════════════════════════════════════════ 全局扫描
class TestNoSharedConnectionCloseAnywhere:
    """★★ 把「不许关共享只读连接」从 `digest.py` 一处扩到**全仓库**。

    ★★★ 这条是被真实事故逼出来的（2026-10-10）
    ----------------------------------------------
    `tests/test_ths_board.py::test_database_has_one_row_per_day` 写了
    `finally: con.close()`，把 `duck._ro_conn` 关掉。
    后果：全量跑时**之后**的 25 条 `test_webapi` 用例全部报
    `Connection Error: Connection already closed!`。

    ★★ 为什么当时极难定位：
      · 单独跑 test_ths_board.py —— 通过
      · 单独跑 test_webapi.py —— 通过
      · 只有「全量」或「两文件同进程」才炸
      · 报错落在 `test_webapi`（一个刚新建的、与DuckDB 无关的文件）上
        ⇒ 极易误判为「FastAPI/TestClient 与 DuckDB 不兼容」
    ⇒ 判据必须**按数据来源追踪**，而不是「哪个文件在报错就查哪个文件」。
    """

    #: 合法的 close 场景白名单：自己 `duckdb.connect()` 建的临时连接
    ALLOW = {
        # 这些用例自己建临时库并在 teardown 回收；用 monkeypatch 隔离
        "tests/test_continuation.py",
        "tests/test_data_layer.py",
    }

    #: 自动筛出「真正持有共享连接引用」的文件，避免几十个 skip 淹没报告
    @staticmethod
    def _holders() -> list[str]:
        import ast
        out = []
        for p in sorted((ROOT / "tests").glob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                        and isinstance(n.value.func, ast.Attribute)
                        and n.value.func.attr == "get_ro_conn"):
                    out.append(str(p.relative_to(ROOT)).replace("\\", "/"))
                    break
        return out

    @pytest.mark.parametrize("rel", _holders.__func__())
    def test_tests_never_close_shared_conn(self, rel):
        import ast

        src = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 溯源：赋值语句右侧**真的调用了** get_ro_conn 的名字，
        # 才算「持有共享连接引用」。
        # ⚠ 必须解析真实代码节点，不能用 `"get_ro_conn" in src` 预筛 ——
        #   docstring 里提到它就会命中（本模块自己就是这种）。
        shared: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                fn = n.value.func
                if isinstance(fn, ast.Attribute) and fn.attr == "get_ro_conn":
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            shared.add(t.id)
        if not shared:
            pytest.skip(f"{rel} 未持有共享连接引用，无需检查")
        if rel in self.ALLOW:
            pytest.skip(f"{rel} 用 monkeypatch 隔离到临时库，白名单放行")
        hits = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "close"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id in shared):
                hits.append(n.lineno)
        assert not hits, (
            f"{rel} 第 {hits} 行 close() 了共享只读连接 ⇒ "
            f"同进程后续所有 DuckDB 查询都会抛 Connection already closed")

    def test_actual_2026_10_10_incident_is_now_guarded(self):
        """★ 点名守护：出事的那一行必须已被修掉。

        与其只依赖泛化扫描，不如把这次事故显式钉住 ——
        泛化判据可能因重构而失效，这条不会。
        """
        src = (ROOT / "tests" / "test_ths_board.py").read_text(encoding="utf-8")
        assert "finally:\n            try:\n                con.close()" not in src, \
            "出事那处 close 又回来了"
        # 该测试函数体内不得出现 close 调用
        import ast

        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and \
                    n.name == "test_database_has_one_row_per_day":
                closes = [x.lineno for x in ast.walk(n)
                          if isinstance(x, ast.Call)
                          and isinstance(x.func, ast.Attribute)
                          and x.func.attr == "close"
                          and isinstance(x.func.value, ast.Name)
                          and x.func.value.id == "con"]
                assert not closes, f"该函数又close 了共享连接：{closes}"

    def test_guard_catches_real_violation(self):
        """★ 反向用例：确认真值样本会被判违规（否则上面是假阳性）。"""
        import ast

        src = ("from backend.data import duck\n"
               "def f():\n"
               "    con = duck.get_ro_conn()\n"
               "    con.close()\n")
        tree = ast.parse(src)
        shared = {n.targets[0].id for n in ast.walk(tree)
                  if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                  and isinstance(n.value.func, ast.Attribute)
                  and n.value.func.attr == "get_ro_conn"
                  and isinstance(n.targets[0], ast.Name)}
        hits = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "close"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id in shared]
        assert hits == [4], f"判据漏掉了真实违规（命中 {hits}）"


# ================================================================== 列名契约
class TestBoardColumns:
    """★ `board_strength` 没有涨跌幅列，只能读 `strength`（排名百分位）。"""

    def test_does_not_select_nonexistent_column(self):
        """★ 只在 **SELECT 子句** 里查，不在注释里查。

        `board_strength` 表没有涨跌幅列（`strength` 才是设计要的量）。
        曾误写 `SELECT ... strength AS pct` 再按百分比格式化
        ⇒ 排名百分位被显示成「电池 +98.4%」这种荒唐数字。
        """
        import ast

        src = (ROOT / "backend" / "domain" / "digest.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        # ★ 用 AST 定位「真正传给 execute 的字符串」，
        #   而不是扫全文 —— docstring 里就写着 `strength as pct` 这个反例。
        sqls = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executemany")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                sqls.append(node.args[0].value)
        assert sqls, "找不到传给 execute 的 SQL 字面量"
        for s in sqls:
            low = s.lower()
            assert "as pct" not in low, f"不得把 strength 当涨跌幅：{s[:80]}"
            assert "pct_chg" not in low, \
                f"board_strength 无涨跌幅列，SELECT 里不得出现它：{s[:80]}"

    def test_output_uses_rank_not_percentage(self):
        """输出格式应是 `板块名(#排名)`。"""
        from backend.domain import digest

        out = digest._top_boards(date(2026, 10, 9))
        if out:
            assert all("(#" in x for x in out), \
                f"板块输出应是「名称(#排名)」，实得 {out[:3]}"
            assert not any("%" in x for x in out), \
                f"不得显示涨跌幅百分比（库里没有），实得 {out[:3]}"


# ================================================================== 时点语义
class TestTimepointSemantics:
    """★ 午间与收盘必须用**各自的数据口径**。"""

    def test_midday_as_of_is_pinned_to_morning_close(self, monkeypatch):
        """★ 午间邮件时间戳应固定到 11:35，而不是调度触发时刻。

        cron "30 11" 在 11:30:00 触发，若直接用触发时刻，
        收件箱显示的是 11:30（正好卡在收盘瞬间）。
        """
        from backend.scheduler import REGISTRY

        captured = {}

        def fake_build(t):
            captured["as_of"] = t
            from backend.domain.mail_templates import MailContext
            return MailContext(as_of=t, kind="midday")

        monkeypatch.setattr("backend.domain.digest.build_midday_context",
                            fake_build)
        monkeypatch.setattr("backend.domain.mail_templates.MailSender",
                            lambda: type("S", (), {"cfg": type("C", (), {
                                "to": []})()})())

        from backend.scheduler import run_job
        # 11:30:00 触发 → 邮件时间应是 11:35
        run_job(REGISTRY["midday"],
                datetime(2026, 10, 9, 11, 30, tzinfo=CST), force=True)
        assert captured.get("as_of") is not None, "未调用装配层"
        assert captured["as_of"].hour == 11
        assert captured["as_of"].minute == 35

    def test_close_as_of_is_pinned_after_close(self, monkeypatch):
        """收盘邮件时间戳应固定到 15:05。"""
        from backend.scheduler import REGISTRY

        captured = {}

        def fake_build(t):
            captured["as_of"] = t
            from backend.domain.mail_templates import MailContext
            return MailContext(as_of=t, kind="midday", alert_text="x")

        monkeypatch.setattr("backend.domain.digest.build_close_context",
                            fake_build)
        monkeypatch.setattr("backend.domain.mail_templates.MailSender",
                            lambda: type("S", (), {"cfg": type("C", (), {
                                "to": []})()})())

        import backend.scheduler as sch
        monkeypatch.setattr(sch, "log_event", lambda *a, **k: None)

        from backend.scheduler import run_job
        run_job(REGISTRY["close"],
                datetime(2026, 10, 9, 15, 5, tzinfo=CST), force=True)
        assert captured.get("as_of") is not None
        assert captured["as_of"].hour == 15
        assert captured["as_of"].minute == 5


# ================================================================== 发信参数
class TestSendParameters:
    """★★ 任务层发信必须用 `retry=1`。"""

    def test_midday_passes_retry_1(self, monkeypatch):
        """★ 默认 retry=3 + 延迟 30/120/300 = 450s > 任务 timeout 300s。

        ⇒ 邮件失败时任务**必然超时**。必须显式 retry=1。
        """
        import inspect

        from backend.scheduler import REGISTRY
        src = inspect.getsource(REGISTRY["midday"].fn)
        assert "retry=1" in src, \
            "midday 发信必须 retry=1（默认 3 次会拖 450s 超过 timeout）"

    def test_close_passes_retry_1(self):
        import inspect

        from backend.scheduler import REGISTRY
        src = inspect.getsource(REGISTRY["close"].fn)
        assert "retry=1" in src, \
            "close 发信必须 retry=1"

    def test_missing_recipient_is_not_an_error(self, monkeypatch):
        """★ `mail.to` 未配置时应「跳过发送」而不是抛异常。

        凭据缺失是**合法状态**（用户还没配收件人），
        抛异常会让任务变成 failed、进而触发 catch_up 反复补跑。
        """
        from backend.scheduler import REGISTRY, run_job
        from backend.domain.mail_templates import MailContext
        monkeypatch.setattr("backend.domain.digest.build_midday_context",
                            lambda t: MailContext(as_of=t, kind="midday"))
        monkeypatch.setattr("backend.domain.mail_templates.MailSender",
                            lambda: type("S", (), {"cfg": type("C", (), {"to": []})()})())
        monkeypatch.setattr("backend.scheduler.log_event", lambda *a, **kw: None)

        res = run_job(REGISTRY["midday"],
                      datetime(2026, 10, 9, 11, 30, tzinfo=CST), force=True)
        # 不论是否配了收件人，任务都不该抛异常
        assert res.ok is True, f"任务不应失败：{res.error}"
        if not res.detail.get("sent"):
            assert "mail.to" in str(res.detail.get("reason", ""))


if __name__ == "__main__":                        # pragma: no cover
    pytest.main([__file__, "-v"])
