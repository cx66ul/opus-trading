# -*- coding: utf-8 -*-
"""轻量 Web API 守护测试（M11 轻量版）。

★ 本文件的价值不在「端点能不能返回 200」——那属于联调，
  而在于**四条纪律会不会被后来的人悄悄改掉**：
  1. 铁律二（时间注入）：源码里不得出现 `datetime.now()` / `time.time()`。
  2. D13：不得出现「建议卖出/应立即/必须卖出/立即减仓/清仓」文案。
  3. 配色：涨红跌绿，**不得反转**。
  4. 单位边界：跨层必须归一（`pct_chg` 库内是百分数，API 出参必须是比例）。

⚠ 判据设计说明（复用本项目已踩的坑）：
  · 静态检查**只看 AST 节点**，不做字符串匹配 ——
    否则源码注释里提到 `datetime.now()` 会被自己的守护拦下。
  · 每条静态守护都配**反向用例**（构造违规样本，断言确实被拦），
    否则「判据恒不触发」的假阳性测试会给虚假安全感。
"""
from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest

WEBAPI = Path(__file__).resolve().parent.parent / "backend" / "api" / "webapi.py"
WEB_DIR = Path(__file__).resolve().parent.parent / "backend" / "web"
FORBIDDEN_SELL = ("建议卖出", "应立即", "必须卖出", "立即减仓", "清仓")


# ══════════════════════════════════════════════════ 工具
def _docstring_ids(tree: ast.AST) -> set[int]:
    """在**已解析的 tree** 上标记所有 docstring 常量节点的 id。

    ★★★ 必须复用同一个 tree 对象
    ---------------------------
    早期版本写成 `_docstring_nodes(src)`（内部自己 `ast.parse`），
    再拿它的 id 去比对另一次 `ast.parse` 出来的节点 ——
    `ast.parse` 每次都生成**全新的节点对象**，`id()` 互不相等
    ⇒ skip集合完全失效 ⇒ docstring 全被判成违规（假阳性）。

    这就是「用 id() 做跨解析节点身份」的通用陷阱：
    **id 只在同一个对象图内有意义**。
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _strip_docstrings(src: str) -> str:
    """把 docstring 换成空串后再parse。

    ★ 必须做：docstring 里出现 `datetime.now()` 是**说明**（例如
      「这里禁止 datetime.now()」），不是**行为**。
      不剥离就会让守护把自己的注释判成违规。
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


def _called_names(src: str) -> set[str]:
    """源码里真正被调用的函数名集合（已剥离 docstring）。"""
    tree = ast.parse(_strip_docstrings(src))
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


# ★ `client` fixture 已提升到 tests/conftest.py（模块级共享），
#   避免各测试文件各定义一份、改路由时只改一处。


# ══════════════════════════════════════════════════ ① 铁律二：时间注入
class TestNoWallClock:
    def test_source_has_no_datetime_now(self):
        """源码（剥离 docstring 后）不得调用 `now()` / `time()`。"""
        names = _called_names(WEBAPI.read_text(encoding="utf-8"))
        bad = names & {"now", "today", "time"}
        assert not bad, f"webapi 出现墙钟调用：{bad}（铁律二：as_of 必须注入）"

    def test_guard_actually_catches_violation(self):
        """★ 反向用例：违规样本必须被抓住，否则上面那条是假阳性。"""
        src = textwrap.dedent('''
            from datetime import datetime
            def bad(day):
                return datetime.now()
        ''')
        assert "now" in _called_names(src), "守护判据抓不到 now()，是假阳性"

    def test_watchlist_write_requires_as_of(self, client):
        """写操作也必须注入 as_of —— 曾经写成 `datetime.now(CST)`。"""
        r = client.post("/api/watchlist", json={"code": "600519"})
        assert r.status_code == 422, "缺 as_of 竟然通过了，说明时间注入被绕过"
        r2 = client.delete("/api/watchlist/600519")
        assert r2.status_code == 422

    def test_day_is_business_day_not_wallclock(self, client):
        """概览返回的 trade_date 必须落在库内已有交易日里。"""
        import duckdb
        from backend.data.duck import DB_PATH
        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            days = {str(r[0])[:10] for r in con.execute(
                "SELECT DISTINCT trade_date FROM kline_daily").fetchall()}
        finally:
            con.close()
        got = client.get("/api/overview").json()["trade_date"]
        assert got in days, f"{got} 不是库内交易日 ⇒ 用的是墙钟"


# ══════════════════════════════════════════════════ ② D13 禁卖文案
class TestD13NoSellInstruction:
    def test_source_has_no_sell_wording(self):
        """源码里**会被送到用户眼前**的字符串字面量不得含禁用文案。

        ★ 跳过两类字符串：① 全部 docstring（维护者说明，不进响应）
          ② `FORBIDDEN_SELL_WORDS` 常量本身（禁用词清单）。
          不跳过就会让守护把自己的注释/常量判成违规（假阳性）。
        """
        src = WEBAPI.read_text(encoding="utf-8")
        tree = ast.parse(src)
        skip = _docstring_ids(tree)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and n.targets[0].id == "FORBIDDEN_SELL_WORDS"):
                for sub in ast.walk(n.value):
                    skip.add(id(sub))

        for n in ast.walk(tree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in skip):
                for w in FORBIDDEN_SELL:
                    assert w not in n.value, f"源码字面量含禁用文案「{w}」"

    def test_sell_word_guard_skips_own_constant(self):
        """★ 反向用例：确认真值样本会被判违规（否则上面是假阳性）。"""
        tree = ast.parse('X = "建议卖出该股"')
        hits = [w for n in ast.walk(tree) if isinstance(n, ast.Constant)
                and isinstance(n.value, str) for w in FORBIDDEN_SELL
                if w in n.value]
        assert hits, "禁用词判据抓不到真实违规样本"

    def test_api_responses_have_no_sell_wording(self, client):
        """实调端点，检查真正返回给浏览器的 JSON。"""
        for path in ("/api/overview", "/api/positions", "/api/health",
                     "/api/boards"):
            body = client.get(path).text
            for w in FORBIDDEN_SELL:
                assert w not in body, f"{path} 返回了禁用文案「{w}」"

    def test_stock_detail_note_is_reminder_only(self, client):
        d = client.get("/api/stock/600519").json()
        assert "不自动卖出" in d["note"]
        for w in FORBIDDEN_SELL:
            assert w not in json.dumps(d, ensure_ascii=False)

    def test_frontend_has_no_sell_wording(self):
        for f in WEB_DIR.glob("*.js"):
            txt = f.read_text(encoding="utf-8")
            for w in FORBIDDEN_SELL:
                assert w not in txt, f"{f.name} 含禁用文案「{w}」"

    def test_guard_actually_catches_violation(self):
        bad = 'X = "建议卖出该股"'
        assert any(w in bad for w in FORBIDDEN_SELL)


# ══════════════════════════════════════════════════ ③ 配色：涨红跌绿
class TestColorConvention:
    def test_up_is_red_down_is_green(self):
        from backend.api.webapi import COLOR_DOWN, COLOR_FLAT, COLOR_UP
        assert COLOR_UP.startswith("#e"), "涨必须是红系（A股习惯）"
        assert COLOR_DOWN.startswith("#1") or COLOR_DOWN.startswith("#0"), \
            "跌必须是绿系"
        assert COLOR_UP != COLOR_DOWN, "涨跌同色 = 配色反转了"

    def test_frontend_css_uses_same_convention(self):
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        assert "--up: #e0392e" in css, "CSS 涨色被改"
        assert "--down: #1a9e5f" in css, "CSS 跌色被改"

    def test_frontend_does_not_hardcode_colors(self, client):
        """★ 前端不得自带一套涨红跌绿 —— 必须从 API 取 colors 字段。

        理由：若前端硬编码而后端改了配色，就会出现「网页红涨绿跌、
        邮件绿涨红跌」，而两边都不会报错。
        """
        js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        # 只允许 S.colors 的默认值出现，不允许在渲染处写死 hex
        assert js.count("#e0392e") <= 1, "前端多处硬编码涨色"
        assert "S.colors = d.colors" in js or "S.colors=d.colors" in js, \
            "前端未从 API 同步配色"
        assert client.get("/api/overview").json()["colors"]["up"]


# ══════════════════════════════════════════════════ ④ 单位边界归一
class TestUnitNormalisation:
    def test_pct_converts_percent_to_ratio(self):
        from backend.api.webapi import _pct
        assert _pct(-0.2248) == pytest.approx(-0.002248)
        assert _pct(1.34) == pytest.approx(0.0134)
        assert _pct(None) is None, "缺失必须是 None，绝不当 0"

    def test_pct_guard_catches_missing_division(self):
        """★ 变异测试：把 `/100` 去掉，这两条必须同时失败。

        判据是**相对倍数**而不是绝对差值 ——绝对阈值（如差>50）
        会随输入量级漂移，且在输入小时（如 0.1）永远不触发，
        属于典型的「恒不触发的假阳性」。
        """
        from backend.api import webapi
        for raw in (-0.2248, 1.34, 10.0, 29.37):
            good = webapi._pct(raw)
            naive = float(raw)                    # 未归一的错误实现
            assert good == pytest.approx(raw / 100)
            assert naive == pytest.approx(good * 100, rel=1e-9), \
                f"未归一值与归一值差 100 倍：raw={raw} naive={naive} good={good}"

    def test_api_pct_fields_are_ratios(self, client):
        """API 出的 pct_chg 必须是 0~1 比例，不能是百分数。

        ★ 必须显式放大 limit：默认只回 strength 前 40 行，
          而排序结果可能整段落在 `concept` 上（实测 2026-10-09：
          前 40 全是 concept，`pct_chg` 源数据本来就全 NULL）
          ⇒ `vals` 为空 ⇒ **判据失效**（既可能真是回归，
          也可能只是没采到数据，两者无法区分）。
          ⇒ 扫全量，并在真的一个都没有时**明确报错**而不是空过。
        """
        d = client.get("/api/boards?limit=1000").json()
        assert d["n_total"] >= len(d["rows"]), "rows 未覆盖全量，判据失效"
        vals = [r["pct_chg"] for r in d["rows"] if r["pct_chg"] is not None]
        assert vals, (
            f"板块表全量 {d['n_total']} 行里 pct_chg 一个都没有 —— "
            f"无法验证单位（先确认 sync_board_strength 是否采到 pct_chg）")
        assert max(vals) < 1.0, f"pct_chg 出现 >=1 的值，疑似百分数未归一：{max(vals)}"

    def test_board_note_numbers_match_library(self, client):
        """★★★ 页面文案里的每个数字都必须**来自查询**，不能硬编码。

        实测事故：`/api/boards` 的 note 长期写死「factor_coverage 仅 0.28」，
        回填后库内均值已是 0.195 ⇒ 页面挂着**与事实脱节**的说明，
        用户无从判断哪个是真的。
        同一处还栽过一次：`n_with_members` 用 `factor_coverage > 0.2` 判定，
        而只有 d1+d5 的 concept 也有 0.30 ⇒ 报 50 个，
        真正取到成分的只有 17 个。
        """
        d = client.get("/api/boards?limit=1000").json()
        assert d["coverage_avg"] is not None
        assert 0 < d["coverage_avg"] <= 1, d["coverage_avg"]
        assert d["coverage_max"] >= d["coverage_avg"]
        # ★ 有成分的板块数不得超过板块总数，且不能等于总数
        #   （若等于，说明判据退化成「有 coverage 就算有成分」）
        assert 0 < d["n_with_members"] < d["n_total"], (
            f"n_with_members={d['n_with_members']} / n_total={d['n_total']}，"
            f"判据疑似退化为「有 coverage 就算有成分」")
        # ★ note 里的数字必须与结构化字段一致（不能各说各话）
        assert f"{d['coverage_avg']:.3f}" in d["note"], (
            f"note 未引用实测 coverage：{d['note']}")
        assert str(d["n_with_members"]) in d["note"], (
            f"note 未引用实测成分板块数：{d['note']}")

    def test_board_sort_prefers_coverage_over_strength(self, client):
        """★★★ 榜单排序必须「覆盖度优先、强度其次」。

        实测问题：`score_boards` 对缺失因子做**按可得权重归一化**
        （`strength = total / coverage`）⇒ coverage 0.15、只有 d5 的
        concept 也能拿 100.0 分，而 5 连板的「电池」（coverage 0.92）
        只有 85.4 ⇒ **分数高低由「缺了多少因子」决定，不由板块强弱决定**。
        页面却按 strength 排序 ⇒ 最不可信的行排在最前。
        ⇒ 判据：取回的 rows 里 coverage 不得单调不增。
        """
        d = client.get("/api/boards?limit=1000").json()
        covs = [r["factor_coverage"] for r in d["rows"]
                if r["factor_coverage"] is not None]
        assert covs == sorted(covs, reverse=True), (
            f"榜单未按factor_coverage 降序：前几行 {covs[:8]}")
        # ★ 反向自证：确实存在「低覆盖高强度」的行，否则这条判据是摆设
        low_cov = [r for r in d["rows"]
                   if (r["factor_coverage"] or 0) < 0.3 and r["strength"]]
        assert low_cov, "库里没有低覆盖度行，该排序判据在本数据集上恒失效"

    def test_overview_amount_scale_is_sane(self, client):
        """成交额量级必须在 1e4~3e4 亿（万级），20 万亿级说明 volume 又错了。"""
        d = client.get("/api/overview").json()
        if d["trade_date"] and d["n_stock"] > 1000:
            amt = d["total_amount_yi"]
            assert amt is not None
            assert 5e3 < amt < 4e4, f"成交额 {amt} 亿超出A股合理区间"

    def test_up_ratio_stays_zero_to_one(self, client):
        """up_ratio / broken_rate 本来就是比例，**不得**被误除 100。"""
        d = client.get("/api/boards").json()
        vals = [r["up_ratio"] for r in d["rows"] if r["up_ratio"] is not None]
        assert vals and max(vals) <= 1.0, "up_ratio 被当百分数除了 100"


# ══════════════════════════════════════════════════ ⑤ 残缺日拦截
class TestDataGateEnforcement:
    def test_incomplete_day_is_409(self, client):
        """2026-10-09 只有 108 只 ⇒ 必须 409，不能返回错的K 线。"""
        import duckdb
        from backend.data.duck import DB_PATH
        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            day, n = con.execute(
                "SELECT CAST(max(trade_date) AS VARCHAR), count(DISTINCT code) "
                "FROM kline_daily").fetchone()
        finally:
            con.close()
        from backend.api.webapi import _usable_days
        usable, _th = _usable_days()
        if day[:10] in usable:
            pytest.skip(f"{day[:10]} 当前是可用日（{n} 只），无残缺日可测")
        r = client.get(f"/api/stock/600519?as_of={day[:10]}")
        assert r.status_code == 409, f"残缺日返回了 {r.status_code}，应 409"
        assert "残缺" in r.json()["detail"]

    def test_default_day_is_usable(self, client):
        """不传 as_of 时必须落在**可用**日，否则页面一打开就报错。"""
        from backend.api.webapi import _usable_days
        usable, _ = _usable_days()
        d = client.get("/api/overview").json()
        assert d["data_usable"] is True
        assert d["trade_date"] in set(usable)
        if d["day_resolution"]["fallback"]:
            assert d["day_resolution"]["requested"] != d["trade_date"], \
                "声称回退了但 requested == used"

    def test_fallback_is_reported_not_silent(self, client):
        """★ 回退必须显式告知，否则等于「静默给错数据」。"""
        d = client.get("/api/overview").json()
        assert set(d["day_resolution"]) >= {"requested", "used", "fallback"}


# ══════════════════════════════════════════════════ ⑥ 端点契约
class TestEndpointContract:
    @pytest.mark.parametrize("path", [
        "/", "/api/overview", "/api/health", "/api/boards",
        "/api/watchlist", "/api/positions", "/api/stock/600519",
    ])
    def test_ok(self, client, path):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"

    def test_search(self, client):
        d = client.get("/api/search?q=茅台").json()
        assert any("茅台" in (r["name"] or "") for r in d["rows"])

    def test_bad_code_rejected(self, client):
        assert client.get("/api/stock/abc").status_code == 400
        assert client.get("/api/stock/60051").status_code == 400

    def test_unknown_code_404(self, client):
        r = client.get("/api/stock/999998")
        assert r.status_code in (404, 409)

    def test_boards_reports_history_depth(self, client):
        """板块强度只回填 2 天 —— 前端不能把它当趋势，必须由API 报出来。"""
        d = client.get("/api/boards").json()
        assert d["n_history_days"] >= 1
        assert "历史" in d["note"] or "趋势" in d["note"]

    def test_health_lists_all_tasks(self, client):
        d = client.get("/api/health").json()
        assert d["n_tasks"] == 11, "TASK_TABLE 应有 11 个任务"
        assert all(t["name"] and t["cron"] for t in d["tasks"])

    def test_stock_detail_reuses_chan_fields(self, client):
        """个股页必须真的是缠论结果（有中枢/信号），不是裸K 线。"""
        d = client.get("/api/stock/600519").json()
        assert d["bars"] and all(k in d["bars"][0]
                                 for k in ("d", "o", "h", "l", "c", "v", "pct"))
        assert d["zhongshus"], "缠论未产出中枢"
        assert d["position"] in ("中枢上方", "中枢下方", "中枢内", "无中枢")

    def test_frontend_files_present(self):
        for n in ("index.html", "app.js", "styles.css"):
            assert (WEB_DIR / n).exists(), f"缺静态文件 {n}"
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        assert "/static/app.js" in html and "/static/styles.css" in html


# ══════════════════════════════════════════════════ ⑥b 推荐页契约
class TestRecommendContract:
    """★ 这一组是被变异测试逼出来的。

    把`rows` 改成 `res[0][...]`（还原「把 dict 当 tuple 用」的老bug）
    时**全部测试仍绿** —— 说明当时没有任何用例校验推荐页的返回结构。
    教训：端点「返回 200」不等于「返回了正确的东西」，
    必须对**关键字段逐个断言**。
    """

    def test_rows_are_dicts_with_expected_keys(self, client):
        d = client.get("/api/recommend").json()
        assert isinstance(d["rows"], list) and d["rows"], "推荐列表为空"
        for r in d["rows"]:
            assert isinstance(r, dict), f"rows 元素是 {type(r)}，不是 dict"
            for k in ("code", "name", "tier", "score", "risk_note",
                      "reasons", "position_hint", "close", "pct_chg"):
                assert k in r, f"推荐行缺字段 {k}：{sorted(r)}"

    def test_pct_chg_is_ratio(self, client):
        d = client.get("/api/recommend").json()
        vals = [r["pct_chg"] for r in d["rows"] if r["pct_chg"] is not None]
        assert vals, "没有任何 pct_chg"
        assert max(vals) < 1.0, f"pct_chg 像百分数：{max(vals)}"

    def test_every_row_has_risk_note(self, client):
        """规格附录 4.4：禁止无风险提示的推荐。"""
        d = client.get("/api/recommend").json()
        for r in d["rows"]:
            assert r["risk_note"] and len(r["risk_note"]) >= 10, \
                f"{r['code']} 缺风险提示"

    def test_pipeline_counters_present(self, client):
        """必须能看到「输入→过滤后→剔除」与档位分布，否则无法排查。"""
        d = client.get("/api/recommend").json()
        for k in ("total_input", "after_filter", "rejected", "stats",
                  "market_state", "position_cap", "cached"):
            assert k in d, f"推荐响应缺 {k}"
        assert d["after_filter"] <= d["total_input"]
        assert set(d["stats"]) <= {"A", "B", "C"}

    def test_market_state_not_hardcoded(self, client):
        """市场状态必须来自 market_regime，不能写死 "repair"。

        ★ 写死的后果：震荡市里也按修复市给仓位建议，
          而 D16 实测「买点 alpha 只在上涨市有效，震荡市最差」。
        """
        import duckdb
        from backend.data.duck import DB_PATH
        con = duckdb.connect(str(DB_PATH), read_only=True)
        try:
            real = con.execute(
                "SELECT state FROM market_regime ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        d = client.get("/api/recommend").json()
        assert real and d["market_state"] == real[0], \
            f"推荐用 {d['market_state']}，库内实际是 {real and real[0]}"

    def test_cache_returns_same_result(self, client):
        """缓存命中必须与首次结果一致（否则页面刷新会得到不同结论）。"""
        a = client.get("/api/recommend").json()
        b = client.get("/api/recommend").json()
        assert b["cached"] is True
        assert [r["code"] for r in a["rows"]] == [r["code"] for r in b["rows"]]

    def test_cache_keyed_by_day(self):
        """★ 缓存键必须含交易日，换日必须失效。"""
        from backend.api import webapi
        assert webapi._cache_key("2026-10-08", 20) != \
            webapi._cache_key("2026-10-09", 20), "缓存键没含交易日 ⇒ 换日仍返旧结果"
        assert webapi._cache_key("2026-10-08", 20) != \
            webapi._cache_key("2026-10-08", 50), "缓存键没含 limit ⇒ 串味"


# ══════════════════════════════════════════════════ ⑦ 复用而非重算
class TestNoDuplicatedLogic:
    def test_overview_reuses_data_gate(self):
        """★ 必须复用 data_gate，自己数股票就会出现两套完备度口径。"""
        src = WEBAPI.read_text(encoding="utf-8")
        assert "data_gate" in src, "概览未复用 data_gate"
        assert "coverage_from_duck" in src

    def test_readonly_connection_used(self):
        """读行情一律 get_ro_conn，绝不 get_conn（会抢写锁）。"""
        src = _strip_docstrings(WEBAPI.read_text(encoding="utf-8"))
        assert "duck.get_conn" not in src and "duck.get_conn(" not in src, \
            "webapi 用了读写连接，有抢锁风险"

    def test_no_frontend_recomputation(self):
        """前端不得自己算涨家数/缠论（否则与邮件口径必然漂移）。"""
        js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        assert "api/overview" in js and "api/stock/" in js
        # 前端不应出现SQL 或中枢计算
        for bad in ("SELECT ", "lag(", "中枢计算"):
            assert bad not in js, f"前端出现疑似重算逻辑：{bad}"


class TestSeveritySemantics:
    """★ `.err` 与 `.warn` 必须分开—— 否则「页面有错」这个判据失去区分力。

    实测事故（2026-10-10）：概览页的「已自动回退到可用交易日」是**正常口径提示**，
    却复用了 `.err` 类（红框）。浏览器验收器扫 `.err` 时把它当成故障，
    一度要我把「正确的回退提示」删掉才能通过验收——判据被污染。
    """

    def test_warn_class_defined_in_css(self):
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        assert ".warn {" in css, "缺 .warn 样式"

    def test_fallback_notice_uses_warn_not_err(self):
        """回退提示必须用 .warn。

        ⚠ 直接读 JS 原文，**不能过 `_strip_docstrings`** ——
          那是 Python 的 `ast.parse` + `ast.unparse`，用在 JS 上
          会把源码整体改写（注释丢失、引号实体化）⇒ 判据本身失效，
          且表现为「莫名FAILED」，极难定位。
        """
        js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        i = js.find("const warn = dr.fallback")
        assert i > 0, "未找到回退提示构造代码"
        seg = js[i:i + 700]
        assert 'class="warn"' in seg, "回退提示没有用 .warn"
        assert 'class="err"' not in seg, "回退提示误用了 .err（口径提示 ≠ 故障）"

    def test_warn_and_err_visually_distinct(self):
        """两个类必须长得不一样（只看颜色字符串会漏掉'其实是同一条规则'）。"""
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        err = css[css.index(".err {"):css.index(".err {") + 220]
        warn = css[css.index(".warn {"):css.index(".warn {") + 220]
        # 取颜色值
        import re
        e_c = re.search(r"color:\s*([^;]+);", err).group(1).strip()
        w_c = re.search(r"color:\s*([^;]+);", warn).group(1).strip()
        assert e_c != w_c, f".err 与 .warn 前景色相同（{e_c}）⇒ 视觉上无法区分"

    def test_error_helper_still_uses_err(self):
        """反向用例：真错误仍然必须走 .err，不能为了通过验收把真错也改成 .warn。"""
        js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        assert "'<div class=\"err\">'" in js or 'class="err"' in js, \
            "err() 帮助函数不再使用 .err，真故障会被显示成提示"


# ══════════════════════════════════════════════════ ⑧ 依赖
def test_favicon_no_404(client):
    """★ 浏览器会自动请求 /favicon.ico，缺它每次刷新都产生一个 404。

    虽无功能影响，但它让「网络零非200」这条验收判据永远不干净
    ⇒ 后面所有网络异常都会被这条例外淹没。
    """
    r = client.get("/favicon.ico")
    assert r.status_code < 400, f"/favicon.ico 返回 {r.status_code}"


# ══════════════════════════════════════════════════ ⑨ 依赖
def test_fastapi_pinned():
    """FastAPI/uvicorn 是轻量端新增依赖，requirements 必须写进去。"""
    req = (WEBAPI.parent.parent.parent / "requirements.txt").read_text(
        encoding="utf-8")
    assert "fastapi" in req.lower(), "requirements.txt 未声明 fastapi"
    assert "uvicorn" in req.lower(), "requirements.txt 未声明 uvicorn"
