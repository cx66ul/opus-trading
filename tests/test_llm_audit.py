# -*- coding: utf-8 -*-
"""M3 · LLM 调用审计与成本统计守护（规格 2.4 要求 4/5 + M3 验收项）。

★ 本文件守护的是三条容易悄悄坏掉的性质：
  1. **审计不能虚报**：降级/失败路径若沿用上一次成功调用的 token，
     成本统计会**每条降级都虚报**，数字看着合理、没有任何报错。
  2. **铁律二不被绕过**：`llm_audit.ts` 是全项目唯一允许读墙钟的表，
     但必须由**入口脚本**取值后往下传，领域层不得自己调 now()。
  3. **自检输出不自相矛盾**：同一事实只探一次，否则会出现
     「服务不可达 但 模型已就绪」。

⚠ 判据设计沿用本项目已踩的坑：每条静态守护都配反向用例；
  变异测试是验证「测试是否真阳性」的唯一可靠手段。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LLM = ROOT / "backend" / "domain" / "llm.py"
SQLITE = ROOT / "backend" / "data" / "sqlite.py"
PARSE = ROOT / "scripts" / "parse_news.py"
WEBAPI = ROOT / "backend" / "api" / "webapi.py"


# ══════════════════════════════════════════════════════ 工具
def _strip_docstrings(src: str) -> str:
    """剥离 docstring 后再 parse。

    ★ 必须做：docstring 里出现 `datetime.now()` 是**说明**，不是行为。
      不剥离会让守护把自己的注释判成违规（本项目已踩过）。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body[0].value.value = ""
    return ast.unparse(tree)


def _calls_in(src: str, fname: str) -> list[ast.Call]:
    """返回源码里对 `fname(...)` 的调用节点（已剥docstring）。"""
    tree = ast.parse(_strip_docstrings(src))
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == fname):
            out.append(n)
    return out


# ══════════════════════════════════════════════════════ ① DDL 契约
class TestAuditTableContract:
    """llm_audit 表必须真的存在，且字段覆盖规格 2.4 要求 4 的六项。"""

    REQUIRED = ("provider", "model", "prompt_ver", "elapsed_ms",
                "prompt_tokens", "completion_tokens", "cost_cny")

    def test_table_exists_in_ddl(self):
        src = SQLITE.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS llm_audit" in src, \
            "llm_audit 表未建（规格 2.4 要求 4「每次调用记录…」）"

    def test_all_spec_fields_present(self):
        """规格明文要求：provider / model / prompt 版本 / 耗时 / token / 成本。"""
        src = SQLITE.read_text(encoding="utf-8")
        i = src.index("CREATE TABLE IF NOT EXISTS llm_audit")
        block = src[i:src.index(";", i)]
        for col in self.REQUIRED:
            assert col in block, f"llm_audit 缺规格要求的字段：{col}"

    def test_table_really_created_in_db(self):
        """★ DDL 写了不等于建了—— 必须真在内存库里跑一遍 DDL 再查。

        ★ 不能直接 import 真实连接：那是项目库，测试里建表会污染数据。
        """
        import sqlite3

        ddl = SQLITE.read_text(encoding="utf-8")
        body = ddl.split('DDL = """')[1].split('"""')[0]
        con = sqlite3.connect(":memory:")
        con.executescript(body)
        r = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='llm_audit'").fetchone()
        assert r is not None, "DDL 执行后仍无 llm_audit 表"
        cols = {x[1] for x in con.execute("PRAGMA table_info(llm_audit)")}
        missing = set(self.REQUIRED) - cols
        assert not missing, f"实际建表缺字段：{missing}"

    def test_cost_summary_has_fallback_column(self):
        """★ `n_fallback` 必须来自 SQL 而不是 Python 端猜。
        猜不出来就会出现「降级次数恒为 0」这种静默失真。"""
        src = SQLITE.read_text(encoding="utf-8")
        assert "n_fallback" in src
        assert "stage = 'fallback'" in src, "n_fallback 未按 stage 统计"


# ══════════════════════════════════════════════════════ ② 铁律二
class TestNoWallClockInDomain:
    """★ llm.py 是领域层，**不得自己取系统时间**。

    墙钟只能在入口脚本（parse_news.py）取一次后作为参数传下来。
    判据用 AST、且剥掉 docstring —— 否则本文件自己的注释会被判违规。
    """

    def test_llm_module_no_wallclock_calls(self):
        src = _strip_docstrings(LLM.read_text(encoding="utf-8"))
        for bad in ("datetime.now", "datetime.today", "time.time"):
            assert bad not in src, \
                f"llm.py 出现 {bad}（领域层禁读墙钟，铁律二）"

    def test_wallclock_is_a_parameter_not_a_default(self):
        """`wallclock` 若给了 `=datetime.now()` 之类的默认值，
        等于把违规藏进签名里 —— 判据必须检查它没有可疑默认值。"""
        tree = ast.parse(_strip_docstrings(LLM.read_text(encoding="utf-8")))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            args = fn.args.args + fn.args.kwonlyargs
            defaults = fn.args.defaults + [d for d in fn.args.kw_defaults if d]
            for a, d in zip(args[len(args) - len(defaults):], defaults):
                if a.arg == "wallclock":
                    seg = ast.unparse(d)
                    assert "now" not in seg and "time()" not in seg, \
                        f"{fn.name} 的 wallclock 默认值取了系统时间：{seg}"

    def test_entry_script_passes_wallclock(self):
        """入口脚本必须**显式传** wallclock，否则审计表墙钟列会全等于 as_of。"""
        src = PARSE.read_text(encoding="utf-8")
        assert "wallclock=wallclock" in src, "入口脚本未传 wallclock"

    def test_sqlite_audit_no_wallclock(self):
        """log_llm_audit 的 ts 由调用方注入 —— 这里再取一次就成了第二个墙钟源。"""
        assert "datetime" not in _strip_docstrings(SQLITE.read_text(encoding="utf-8"))


def _fallback_audit_kwargs() -> dict:
    """定位 `tag_news` 里 stage="fallback" 那次 audit 调用的关键字参数。

    ★ 用 AST 定位而不是字符串偏移 ——
      早先用 `src.rindex("self.audit(", 0, i) + i + 60` 截取，
      结果 60 字符**刚好截在 usage 参数前面** ⇒ 判据恒失败，
      而且报的是「变异未生效」，看起来像源码不对，实际是判据切错了。
    """
    tree = ast.parse(LLM.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "tag_news"):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "audit"):
                kw = {k.arg: k.value for k in node.keywords}
                if ast.literal_eval(kw.get("stage", ast.Constant(""))) == "fallback":
                    return {k: ast.unparse(v) for k, v in kw.items()}
    raise AssertionError("tag_news 里找不到 stage='fallback' 的 audit 调用")


# ══════════════════════════════════════════════════════ ③ 审计不虚报
class TestNoPhantomUsage:
    """★★ 降级路径不得沿用上一次成功调用的 token/耗时。

    实测事故（2026-10-10）：一次 450/50 token、2071ms 的调用之后走
    `rule_based_fallback`，审计行把那些数字原样记到 fallback 上。
    ⇒ 成本统计**每条降级都虚报**，且数字漂亮、无任何报错。
    """

    def test_fallback_audit_passes_explicit_empty_usage(self):
        kw = _fallback_audit_kwargs()
        assert "usage" in kw, \
            ("fallback 审计未显式传 usage ⇒ 会继承 last_usage 的旧值"
             "（虚报 token 与耗时）")
        assert "LLMUsage(" in kw["usage"], "传入的 usage 必须是显式新建的 LLMUsage"

    def test_fallback_usage_is_zero_not_inherited(self):
        """反向用例：显式新建的 usage 里不能带 token。"""
        usage = _fallback_audit_kwargs().get("usage", "")
        assert "prompt_tokens" not in usage and "completion_tokens" not in usage, \
            "fallback 的 usage竟然带了 token（规则兜底根本没调模型）"

    def test_usage_defaults_to_zero(self):
        """LLMUsage 默认 token 必须是 0 —— 靠默认值保证「不写就是零」。"""
        tree = ast.parse(LLM.read_text(encoding="utf-8"))
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "LLMUsage")
        # ★ 字段是 `x: int = 0` 形式 ⇒ AnnAssign。
        #   早先误判成「有 value 就是非零默认」，实际要先看 value 是不是
        #   Constant 0 —— dataclass 里 `field(default_factory=...)` 也是有 value 的。
        fields: dict[str, ast.AST | None] = {}
        for st in cls.body:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                fields[st.target.id] = st.value
        for name in ("prompt_tokens", "completion_tokens", "elapsed_ms"):
            assert name in fields, f"LLMUsage 缺字段 {name}"
            v = fields[name]
            assert v is None or (isinstance(v, ast.Constant) and v.value == 0), \
                f"LLMUsage.{name} 默认值不是 0（缺省必须是零用量）"

    def test_audit_write_failure_does_not_raise(self):
        """★ 审计写失败绝不能把打标结果丢掉 —— LLM 已经返回了结果。"""
        src = _strip_docstrings(LLM.read_text(encoding="utf-8"))
        i = src.index("def audit(")
        j = src.index("\n    def ", i + 10)
        seg = src[i:j]
        assert "except Exception" in seg, "审计写入未兜异常"
        assert "log_llm_audit" in seg and "raise" not in seg.replace(
            "raise_for_status", ""), "审计失败路径里有 raise"


# ══════════════════════════════════════════════════════ ④ 自检不自相矛盾
class TestConnectivitySelfCheck:
    """★ 同一事实只探一次 —— 分开探必然出现矛盾的输出。"""

    def test_no_contradictory_reachable_and_loaded(self):
        """`model_loaded=True` ⇒ 必须 `reachable=True`。"""
        for path, key in ((LLM, "connectivity_check"), (WEBAPI, "api_llm_health")):
            src = path.read_text(encoding="utf-8")
            i = src.index(key)
            seg = src[i:i + 1600]
            # 找 available() 赋值给 model_loaded 之后，是否又把 reachable 写死
            if 'out["model_loaded"] = self.available()' in seg:
                after = seg[seg.index('out["model_loaded"] = self.available()'):]
                assert '"reachable"] = True' in after, \
                    f"{path.name}: available() 成功却没把 reachable 置 True"

    def test_probe_is_opt_in(self):
        """真推理要 1~2 秒，必须是显式开关而不是默认动作。"""
        src = WEBAPI.read_text(encoding="utf-8")
        i = src.index("def api_llm_health")
        seg = src[i:src.index("\n@app.", i + 10)]
        assert 'probe: bool = Query(False' in seg, \
            "probe 必须默认 False（默认真推理会让自检端点变慢且可能卡死）"

    def test_health_returns_structure_not_bool(self):
        """规格 2.4 要求 5 要的是「自检页面」，布尔值撑不起页面。"""
        src = WEBAPI.read_text(encoding="utf-8")
        i = src.index("def api_llm_health")
        seg = src[i:src.index("\n@app.", i + 10)]
        assert "-> dict" in seg, "自检端点返回类型不是 dict"

    def test_health_requires_as_of(self):
        """铁律二：业务时点必填。"""
        src = WEBAPI.read_text(encoding="utf-8")
        i = src.index("def api_llm_health")
        seg = src[i:src.index("\n@app.", i + 10)]
        assert "as_of: str = Query(..." in seg, "自检端点的 as_of 不是必填"


# ══════════════════════════════════════════════════════ ⑤ 端点契约
class TestAuditEndpoints:
    def test_status_endpoint(self, client):
        r = client.get("/api/llm/status")
        assert r.status_code == 200
        d = r.json()
        for k in ("since", "as_of", "days", "rows", "totals", "cost_note"):
            assert k in d, f"/api/llm/status 缺字段 {k}"
        assert "本地" in d["cost_note"], "成本口径未说明"

    def test_totals_present_and_consistent(self, client):
        d = client.get("/api/llm/status").json()
        t, rows = d["totals"], d["rows"]
        assert t["n_calls"] == sum(r["n_calls"] for r in rows), \
            "totals.n_calls 与分组和不一致"
        assert t["n_tokens"] == sum(r["prompt_tokens"] + r["completion_tokens"]
                                    for r in rows), "totals.n_tokens 与分组和不一致"

    def test_health_endpoint(self, client):
        r = client.get("/api/llm/health", params={"as_of": "2026-10-08"})
        assert r.status_code == 200
        d = r.json()
        for k in ("provider", "model", "reachable", "model_loaded", "probed"):
            assert k in d, f"/api/llm/health 缺字段 {k}"
        if d["model_loaded"]:
            assert d["reachable"] is True, \
                "矛盾输出：模型已就绪却报服务不可达"

    def test_health_requires_as_of_param(self, client):
        assert client.get("/api/llm/health").status_code == 422

    def test_status_window_anchored_on_as_of(self, client):
        """★ 窗口必须以 as_of 为锚，不取系统日期 —— 回放历史才可复现。"""
        a = client.get("/api/llm/status",
                       params={"as_of": "2026-10-08"}).json()
        b = client.get("/api/llm/status",
                       params={"as_of": "2026-06-01"}).json()
        assert a["as_of"] != b["as_of"], "as_of 未透传到统计窗口"
        assert a["since"] > b["since"], "since 未随 as_of 变化"


# ══════════════════════════════════════════════════════ ⑥ 反向用例
def test_guard_catches_phantom_usage_mutation(tmp_path):
    """★★ 真变异：把源码里fallback 的显式 usage 删掉一整行，
    再跑同一条判据 —— 必须真的报出来。

    ★★ 为什么必须「真改文件再跑判据」而不是在内存里造个 dict
    ------------------------------------------------------
    早先版本在内存里 `del kw["usage"]` 再断言「usage 不在」——
    那是**自证**：断言的永远成立，与判据是否有效毫无关系。
    本项目已多次栽在「假阳性测试」上：只有真变异能区分
    「守护在工作」与「守护压根没检查」。
    """
    import shutil
    import subprocess

    kw = _fallback_audit_kwargs()
    assert "usage" in kw, "变异前提不成立：源码里本来就没有 usage"

    src_path = LLM
    backup = tmp_path / "llm.py.bak"
    shutil.copy(src_path, backup)
    try:
        s = src_path.read_text(encoding="utf-8")
        target = '                   usage=LLMUsage(ok=False, err="规则兜底，未调用模型"))\n'
        assert target in s, "变异锚点未命中（源码变了，判据需重写）"
        src_path.write_text(s.replace(target, "                   )\n"),
                            encoding="utf-8")

        # 变异后判据必须不成立
        try:
            mutated_kw = _fallback_audit_kwargs()
            assert "usage" not in mutated_kw, "变异后 usage 仍在 —— 变异无效"
        except AssertionError:
            pass                      # 找不到调用也算判据触发，可接受
    finally:
        shutil.copy(backup, src_path)          # ★ 无论成败都必须还原

    # 还原后判据必须重新成立（验证还原干净）
    assert "usage" in _fallback_audit_kwargs(), "还原后判据仍不成立"

    # ★ 顺带确认整个测试文件仍能收集（还原必须彻底）。
    #   用 sys.executable 而不是 "python" —— 后者会解析到系统 Python，
    #   本机系统解释器**没装 pytest**，于是报 "No module named pytest"，
    #   看起来像还原失败，其实是解释器选错了。
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)),
         "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"还原后测试无法收集：{r.stdout[-500:]}{r.stderr[-300:]}"


def test_guard_catches_missing_ledger_columns():
    """反向用例：抽掉 cost_cny 字段，DDL 契约必须报警。"""
    src = SQLITE.read_text(encoding="utf-8")
    i = src.index("CREATE TABLE IF NOT EXISTS llm_audit")
    block = src[i:src.index(";", i)]
    mutated = block.replace("cost_cny REAL DEFAULT 0,", "")
    assert "cost_cny" not in mutated, "变异未生效"
    assert not set(TestAuditTableContract.REQUIRED) <= set(mutated.split())

class TestAsOfIsBusinessTime:
    """★★★ `llm_audit.as_of` 必须是**业务时点**，不是墙钟。

    实测事故（2026-10-10）：`scripts/parse_news.py` 里写
        as_of = datetime.now(CST)
        wallclock = datetime.now(CST)
    两列取值完全相同 ⇒ 审计表 `ts` 与 `as_of` 一起变成「最后一次运行的时刻」。

    ⇒ 后果：回放/复盘时，
      「按这条消息的事件时间重算」退化成「按今天重算」，
      **每条历史消息的审计都塌缩到同一天**，
      而且这个错误**没有任何报错** —— 两列都看着合理，
      甚至 `ts >= as_of` 这类自洽性断言都会通过。

    正确语义：`ts` = 墙钟（运维事实：推理耗时/token），
                `as_of` = 这条消息自己的事件时间。
    """

    SCRIPT = ROOT / "scripts" / "parse_news.py"

    def test_as_of_not_wallclock_alias(self):
        src = self.SCRIPT.read_text(encoding="utf-8")
        # ★ 判据不能是「源码里没有 now()」——入口脚本取墙钟是合法的特权。
        #   要抓的是「as_of 和 wallclock 取自同一次 now()」。
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
        assert fn is not None, "未找到 main()，判据本身失效"

        now_calls = []
        for n in ast.walk(fn):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)):
                name = n.targets[0].id
                if isinstance(n.value, ast.Call) and isinstance(
                        n.value.func, ast.Attribute) and n.value.func.attr == "now":
                    now_calls.append(name)

        assert "as_of" not in now_calls, (
            f"main() 里as_of 直接取墙钟（{now_calls}）"
            f" —— 审计的 as_of 必须是消息自己的事件时间")
        # wallclock 取墙钟是对的（它本就该是墙钟）
        assert "wallclock" in now_calls, "wallclock 未取墙钟，判据可能失效"

    def test_as_of_comes_from_row(self):
        """★ as_of 必须逐条来自 `raw_news.ts`。"""
        src = self.SCRIPT.read_text(encoding="utf-8")
        assert "row[\"ts\"]" in src or "row['ts']" in src, (
            "as_of 未从 raw_news.ts 取 —— 说明仍在用统一的一个时刻")
        # 取不到时必须告警，而不是静默退回 now()
        assert "fromisoformat" in src
        assert "退回墙钟" in src, "ts 解析失败时必须显式告警"

    def test_parse_one_still_takes_both(self):
        """★ `parse_one` 必须同时接收 as_of 与 wallclock（两列都要有来源）。"""
        tree = ast.parse(self.SCRIPT.read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "parse_one"), None)
        assert fn is not None, "未找到 parse_one()"
        params = [a.arg for a in fn.args.args]
        assert "as_of" in params and "wallclock" in params, params
        # ★ 且两者不得在函数内部互相赋值（否则等于把一列抄成另一列）
        body_names = {n.targets[0].id for n in ast.walk(fn)
                      if isinstance(n, ast.Assign) and len(n.targets) == 1
                      and isinstance(n.targets[0], ast.Name)}
        assert not ({"as_of", "wallclock"} & body_names), (
            f"parse_one 内部改写了 as_of/wallclock：{body_names}")


class TestAsOfAuditMutation:
    """变异验证：把 as_of 改回墙钟，判据必须失败。

    ★ 变异点选在 `main()` 的赋值处（重新引入 `as_of = datetime.now(CST)`），
      而不是改 `_as_of_of` 的函数体 ——
      后者会变成不可达的死代码，而判据扫的是 `main()` 的赋值语句
      ⇒ 扫不到 ⇒ **变异无效但断言看起来"通过"**，
      这正是「假变异」的典型（断言必须先证明自己会红）。
    """

    def test_mutation_detected(self):
        src = TestAsOfIsBusinessTime.SCRIPT
        text = src.read_text(encoding="utf-8")
        nl = chr(10)
        try:
            # 变异 A：让 main() 重新出现 `as_of = datetime.now(CST)`
            mutated = text.replace(
                "    wallclock = datetime.now(CST)",
                "    wallclock = datetime.now(CST)" + nl +
                "    as_of = datetime.now(CST)  # mutated",
                1)
            assert mutated != text, "变异A未生效"

            tree = ast.parse(mutated)
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == "main")
            now_calls = [
                n.targets[0].id for n in ast.walk(fn)
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr == "now"]
            # ★ 先证明这个判据**确实**能抓住 as_of 取墙钟
            assert "as_of" in now_calls, (
                "变异A没被判据抓住 —— 判据本身失效，不能用它守护真实代码")

            # 变异 B：调用处不再从 row 取业务时点
            #   ★ 必须**锚定整行**：`_as_of_of` 在文件里出现两次
            #   （嵌套函数定义 + 调用），`replace(x, 1)` 换到的是定义处
            #   ⇒ 调用点没变而断言以为变了 —— 又一次「假变异」。
            call_line = "res = parse_one(row, provider, codes, _as_of_of(row), wallclock)"
            assert call_line in text, "未找到 parse_one 调用行，变异B无从下手"
            mutated2 = text.replace(
                call_line,
                "res = parse_one(row, provider, codes, wallclock, wallclock)", 1)
            assert call_line not in mutated2, "变异B 未真正替换调用处，变异无效"
        finally:
            src.write_text(text, encoding="utf-8")

        # 还原必须干净
        assert "as_of = datetime.now(CST)  # mutated" not in src.read_text(
            encoding="utf-8")
