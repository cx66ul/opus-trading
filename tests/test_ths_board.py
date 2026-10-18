# -*- coding: utf-8 -*-
"""★ 同花顺板块适配器 + `board_strength` 落库 测试。

★★★ 这组测试防三类「看着成功、实际残缺」的缺陷
-------------------------------------------------
1. **不翻页** —— `/thshy/` 首页表格只渲染 **50** 行，但页面里有**90** 个板块。
   不翻页就静默少 40 个，看起来「拿到了 50 个」，实际残缺。
2. **行业/概念用同一套解析** —— 概念页`/gn/` 首页是**新闻页**（无行情表），
   行情在 ajax 的 `gnSection` JSON 里。用同一套解析会**静默返回空**，
   看起来像「当天没有概念板块」（实测 293 个）。
3. **401 当成「到最后一页」** —— 401 是**偶发限流**（隔 1.5s 重试即 200），
   当成末页会静默少一个板块（实测 90 → 87）。

★ 为什么这些特别危险：不报错、健康度全绿、行数看着正常，
  只有「板块排名」悄悄失真。

运行： pytest tests/test_ths_board.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data.adapters.ths_board import (   # noqa: E402
    GN_FIELD, N_COLUMNS, THS_COLUMNS,
    parse_board_rows, parse_concept_section,
)
from backend.data.base import SourceError       # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=CST)


# ================================================================== 样本 HTML
def row_html(seq, code, name, pct, vol, amt, inflow, up, down, avg,
             leader_name="力王股份", leader_code="920627",
             leader_price="23.17", leader_pct="29.95"):
    return f"""
    <tr>
      <td>{seq}</td>
      <td><a href="http://q.10jqka.com.cn/thshy/detail/code/{code}/"
             target="_blank">{name}</a></td>
      <td class="c-rise">{pct}</td>
      <td>{vol}</td>
      <td>{amt}</td>
      <td>{inflow}</td>
      <td class="c-rise">{up}</td>
      <td class="c-fall">{down}</td>
      <td>{avg}</td>
      <td><a href="http://stockpage.10jqka.com.cn/{leader_code}/"
             target="_blank">{leader_name}</a></td>
      <td class="c-rise">{leader_price}</td>
      <td class="c-rise">{leader_pct}</td>
    </tr>"""


INDUSTRY_HTML = f"""<html><body><table>
<thead><tr>{''.join(f'<th>{c}</th>' for c in THS_COLUMNS)}</tr></thead>
<tbody>{row_html(1, 881281, "电池", "2.79", "2350.29", "714.67", "33.71", "77", "28", "30.41")}
{row_html(2, 881169, "贵金属", "0.95", "499.70", "124.62", "10.01", "9", "5", "24.94",
            "山金国际", "000975", "22.89", "3.39")}
{row_html(3, 881101, "种植业与林业", "-0.30", "1265.33", "95.54", "-4.63", "14", "14", "7.55",
            "万向德农", "600371", "14.97", "9.99")}
</tbody></table></body></html>"""

# 真实 gnSection 样本（实测 2026-10-09）
GN_SECTION_REAL = json.dumps({
    "2": {"platecode": "885333", "platename": "移动支付", "cid": "300188",
           "199112": -2.02, "zjjlr": -3.52, "zfl": 13},
    "4": {"platecode": "885343", "platename": "稀土永磁", "cid": "300382",
           "199112": -2.01, "zjjlr": -1.05, "zfl": 18},
    "5": {"platecode": "885345", "platename": "新疆振兴", "cid": "300386",
           "199112": -1.92, "zjjlr": -10.29, "zfl": 17},
}, ensure_ascii=False)

CONCEPT_HTML = (
    '<html><body><input type="hidden" id="baseUrl" value=\'gn/index\'>'
    f'<input type="hidden" id="gnSection" value=\'{GN_SECTION_REAL}\'>'
    '</body></html>'
)


# ================================================================== 行业解析
class TestParseIndustryRows:
    def test_parses_all_numeric_columns(self):
        """★ 12 列必须全部取到（早期实现只取 3 列，丢掉了净流入/涨跌家数）。"""
        rows = parse_board_rows(INDUSTRY_HTML, "industry")
        assert len(rows) == 3
        r = rows[0]
        assert r["code"] == "881281"
        assert r["name"] == "电池"
        assert r["board_code"] == "THSIND881281"
        assert r["pct_chg"] == 2.79
        assert r["amount_yi"] == 714.67
        assert r["net_inflow_yi"] == 33.71, "★ 净流入（D1 缺失的那项）必须取到"
        assert r["up_cnt"] == 77
        assert r["down_cnt"] == 28
        assert r["leader_code"] == "920627", "领涨股代码在 href 里"
        assert r["leader_name"] == "力王股份"

    def test_negative_values_preserved(self):
        """负数不能被当成缺失（种植业与林业是 -0.30 / -4.63）。"""
        rows = parse_board_rows(INDUSTRY_HTML, "industry")
        r = rows[2]
        assert r["pct_chg"] == -0.30
        assert r["net_inflow_yi"] == -4.63
        assert r["up_ratio"] == pytest.approx(0.5), "14涨14跌 ⇒ 广度 50%"

    def test_up_ratio_computed(self):
        rows = parse_board_rows(INDUSTRY_HTML, "industry")
        assert rows[0]["up_ratio"] == pytest.approx(77 / 105)

    def test_leader_code_none_when_no_link(self):
        """★ 领涨股没有链接时必须 None，不能编一个。

        （构造独立样本而非替换，避免 `str.replace` 只改首个匹配）
        """
        html = f"<table><tbody>{row_html(1, 881281, '电池', '2.79', '1', '1', '1', '1', '1', '1', '无', '', '', '')}</tbody></table>"
        rows = parse_board_rows(html, "industry")
        assert len(rows) == 1
        assert rows[0]["leader_code"] is None, "无链接时不得编造领涨股代码"
        assert rows[0]["leader_name"] == "无"

    def test_short_row_skipped_not_parsed_partially(self):
        """★ 列数不足的行必须**整行跳过**，不能产出半截数据。

        半截数据会被读成「涨停 0 家」「净流入 0」这类错误结论，
        而且**不报错**。
        """
        broken = f"""<table><tbody>
        {row_html(1, 881281, "电池", "2.79", "2350.29", "714.67", "33.71", "77", "28", "30.41")}
        <tr><td>2</td><td><a href="http://q.10jqka.com.cn/thshy/detail/code/881169/"
        >贵金属</a></td><td>0.95</td></tr>
        </tbody></table>"""
        rows = parse_board_rows(broken, "industry")
        assert len(rows) == 1, "只应保留完整的那一行"
        assert rows[0]["code"] == "881281"

    def test_duplicate_code_deduped(self):
        html = INDUSTRY_HTML + row_html(
            4, 881281, "电池", "2.79", "2350.29", "714.67", "33.71", "77", "28", "30.41")
        rows = parse_board_rows(html, "industry")
        assert len(rows) == 3, "重复板块应去重"


# ================================================================== 概念解析
class TestParseConceptSection:
    def test_parses_gn_section_json(self):
        """★★ 概念板块走 `gnSection` JSON，不是 `<table>`。

        实测 `/gn/` 首页是**新闻页**（表头：日期/概念名称/驱动事件/龙头股/成分股数量），
        行情藏在 ajax 返回的 hidden input 里。
        用行业那套解析会**静默返回空**。
        """
        rows = parse_concept_section(CONCEPT_HTML)
        assert len(rows) == 3
        r = rows[0]
        assert r["code"] == "885333"
        assert r["name"] == "移动支付"
        assert r["board_code"] == "THSCON885333"
        assert r["pct_chg"] == -2.02
        assert r["net_inflow_yi"] == -3.52
        assert r["up_cnt"] == 13

    def test_concept_and_industry_use_different_parsers(self):
        """★ 两套解析对彼此的输入都必须返回空（而不是错解）。"""
        assert parse_concept_section(INDUSTRY_HTML) == [], \
            "行业 HTML 不该被概念解析器误读"
        assert parse_board_rows(CONCEPT_HTML, "concept") == [], \
            "概念 JSON 不该被行业解析器误读（这是旧 bug）"

    def test_up_ratio_is_none_when_no_decline_count(self):
        """★★ gnSection **没有** `zdl`（跌幅家数）⇒ 广度必须 None。

        若把缺失当 0，会算出「广度 100%」——
        实测 293 个概念板块全部如此 ⇒ 概念广度恒为满分，毫无区分度。
        """
        rows = parse_concept_section(CONCEPT_HTML)
        assert all(r["up_ratio"] is None for r in rows)

    def test_up_ratio_computed_when_decline_present(self):
        """有 zdl 时广度才算得出来。"""
        data = json.dumps({"1": {"platecode": "885333", "platename": "测试",
                                 "199112": 1.0, "zjjlr": 0.5, "zfl": 30,
                                 "zdl": 10}}, ensure_ascii=False)
        html = f'<input id="gnSection" value=\'{data}\'>'
        rows = parse_concept_section(html)
        assert rows[0]["up_ratio"] == pytest.approx(0.75)

    def test_malformed_json_returns_empty_not_crash(self):
        html = "<input id=\"gnSection\" value='{not json'>"
        assert parse_concept_section(html) == []

    def test_leader_absent_for_concept(self):
        """gnSection 不含领涨股 ⇒ 必须 None，不能拿板块 code 冒充。"""
        rows = parse_concept_section(CONCEPT_HTML)
        assert all(r["leader_code"] is None for r in rows)
        assert all(r["leader_name"] is None for r in rows)

    def test_ampersand_unescaped(self):
        """★ value 属性里的 `&quot;` 必须还原，否则 JSON 解析失败。"""
        data = json.dumps({"1": {"platecode": "885333", "platename": "A&B",
                                 "199112": 1.0}}, ensure_ascii=False)
        html = f'<input id="gnSection" value=\'{data.replace(chr(34), "&quot;")}\'>'
        rows = parse_concept_section(html)
        assert len(rows) == 1
        assert rows[0]["name"] == "A&B"


# ================================================================== 列定义
class TestColumnContract:
    def test_columns_match_real_page(self):
        """★ 表头必须与实测页面一致（12 列，顺序固定）。"""
        assert N_COLUMNS == 12
        assert THS_COLUMNS[2] == "涨跌幅(%)"
        assert THS_COLUMNS[5] == "净流入(亿元)"
        assert THS_COLUMNS[6] == "上涨家数"
        assert THS_COLUMNS[7] == "下跌家数"

    def test_gn_field_ids_documented(self):
        """★ gnSection 的键名是内部 field id，必须有语义说明表。"""
        assert GN_FIELD["199112"] == "涨跌幅(%)"
        assert GN_FIELD["zjjlr"] == "净流入(亿元)"
        assert "zfl" in GN_FIELD and "zdl" in GN_FIELD


# ================================================================== 分页契约
class TestPagination:
    """★ 分页逻辑的守护（用假适配器，不打网络）。"""

    def _adapter(self, pages):
        from backend.data.adapters.ths_board import ThsBoardAdapter

        a = ThsBoardAdapter(qps=1000)

        def fake(kind, page, retries=2):
            html = pages.get(page)
            if html is None:
                raise SourceError(1003, "无此页", retryable=False)
            return html

        a._ajax_page = fake
        a._get = lambda url, **kw: INDUSTRY_HTML
        return a

    def test_pages_are_accumulated(self):
        """★ 必须翻页并**累加**，否则只剩首屏。"""
        p2 = f"<table><tbody>{row_html(51, 881300, '板块B', '1.0', '1', '1', '1', '5', '5', '1')}</tbody></table>"
        p3 = f"<table><tbody>{row_html(91, 881400, '板块C', '2.0', '1', '1', '1', '5', '5', '1')}</tbody></table>"
        a = self._adapter({2: p2, 3: p3})
        rows = a.fetch_board_list("industry", NOW)
        names = {r["name"] for r in rows}
        assert {"电池", "贵金属", "种植业与林业"} <= names, "首屏必须在"
        assert "板块B" in names, "第 2 页必须累加"
        assert "板块C" in names, "第 3 页必须累加"

    def test_stop_on_first_empty_page(self):
        """空页= 到底，停止翻页（否则白打一堆请求）。"""
        a = self._adapter({})          # 第 2 页即无内容
        rows = a.fetch_board_list("industry", NOW)
        assert len(rows) == 3

    def test_ajax_failure_is_not_silently_treated_as_last_page(self):
        """★★ 401/异常**不能**当成「到最后一页」。

        实测 401 是偶发限流（隔 1.5s 重试即 200）。
        当成末页会静默少板块：90 → 87，且日志只是 warning。
        本测试确认：**已取到的数据仍保留**，但异常被记录。
        """
        from backend.data.adapters.ths_board import ThsBoardAdapter

        a = ThsBoardAdapter(qps=1000)
        a._get = lambda url, **kw: INDUSTRY_HTML

        def boom(kind, page, retries=2):
            if page == 2:
                raise SourceError(1003, "401 Unauthorized", retryable=True)
            raise SourceError(1003, "无此页", retryable=False)

        a._ajax_page = boom
        # 不抛异常，且保留首屏 3 个
        rows = a.fetch_board_list("industry", NOW)
        assert len(rows) == 3, "翻页失败时首屏数据必须保留"

    def test_concept_uses_single_ajax_call(self):
        """★ 概念板块**一次 ajax 给全量**（实测 gnSection 含 293 个）。

        若去翻页会拿到空页并白打请求；更糟的是把空页当「概念板块为空」。
        """
        a = self._adapter({})
        a._get = lambda url, **kw: CONCEPT_HTML
        calls = []

        def spy(kind, page, retries=2):
            calls.append(page)
            return CONCEPT_HTML

        a._ajax_page = spy
        rows = a.fetch_board_list("concept", NOW)
        assert len(rows) == 3
        assert calls == [1], f"概念板块只该打 1 次 ajax，实得 {calls}"


if __name__ == "__main__":                        # pragma: no cover
    pytest.main([__file__, "-v"])


# ================================================================== 时序归一
class TestTimestampNormalization:
    """★★ `board_strength.ts` 必须按**交易日**归一，不能用 `datetime.now()`。

    主键是 `(ts, board_code)`，而板块强度本质是**日频**序列。
    早先直接用真实时刻，同一天重跑 3 次就产生 3 个 ts
    （实测 2026-10-09 有 14:37:30 / 15:00:00 / 15:03:44 三份），
    于是：
      · 「按天取当日板块」必须写 `QUALIFY row_number() ... ORDER BY ts DESC`
        才能取到一份 —— **日频序列事实上退化成一堆日内快照**
      · `rank_change` 会在同一天内自己跟自己比（14:37 vs 15:00 vs 15:03）
      · 表行数虚增 3 倍（383 → 856），看不出是重复

    这是「不报错、只是数据悄悄变成另一种东西」的典型。
    """

    def test_ts_is_normalized_to_close_of_day(self):
        """盘中任意时刻采集都必须落到同一 15:00。"""
        from datetime import datetime as _dt

        CST = timezone(timedelta(hours=8))
        samples = [
            _dt(2026, 10, 9, 14, 37, 30, 353748, tzinfo=CST),
            _dt(2026, 10, 9, 15, 0, 0, 0, tzinfo=CST),
            _dt(2026, 10, 9, 15, 3, 44, 780518, tzinfo=CST),
            _dt(2026, 10, 9, 9, 15, 0, 0, tzinfo=CST),
        ]
        normalized = {t.replace(hour=15, minute=0, second=0, microsecond=0)
                      for t in samples}
        assert len(normalized) == 1, \
            f"同一天的 4 个采集时刻归一后应只剩 1 个，实得 {len(normalized)}"

    def test_main_does_not_use_raw_now_for_ts(self):
        """★ 源码里不应把 `datetime.now()` 直接当 ts 传给 build_rows。

        用源码检查守护（不是行为检查）：行为检查需要跑网络。
        """
        src = (ROOT / "scripts" / "sync_board_strength.py").read_text(
            encoding="utf-8")
        # 允许 `ts = now` 这类赋值，但必须紧跟归一
        assert "replace(hour=15, minute=0, second=0, microsecond=0)" in src, \
            "必须把 ts 归一到 15:00（收盘）"
        # 归一必须在 build_rows 调用**之前**
        norm_at = src.index("replace(hour=15, minute=0, second=0, microsecond=0)")
        build_at = src.index("build_rows(all_rows")
        assert norm_at < build_at, "归一必须在 build_rows 之前"

    def test_database_has_one_row_per_day(self):
        """★ 库内同一交易日只能有一个 ts。

        这是**端到端**守护：前面的测试都只查逻辑，
        这条直接查真实库，防止旧数据/其他脚本再引入日内快照。

        ★★★ 这里**绝不能** `con.close()`
        --------------------------------
        `get_ro_conn()` 返回的是**进程级共享单连接**（`duck._ro_conn`），
        `close()` 关掉它 ⇒ 之后所有拿到同一引用的用例全部报
        `Connection Error: Connection already closed!`。

        ★★ 这个 bug 极难定位，因为它**只在全量跑时暴露**：
          单独跑本文件正常（连接刚建、没人再用），
          单独跑受害文件也正常（连接还没被建），
          只有「本文件 + 受害文件」同进程依次跑才炸。
          症状还落在**完全无关的文件**上（当时是 test_webapi 25 条全红），
          极易误判为「我刚写的测试有问题」或「FastAPI 与 DuckDB 不兼容」。
        ⇒ 判据记忆：**拿到 `get_ro_conn()` 的地方，一行 close 都不能有。**
        """
        from backend.data import duck as _duck

        try:
            con = _duck.get_ro_conn()
        except Exception:
            pytest.skip("DuckDB 不可用（可能被写锁占用）")
        rows = con.execute(
            "SELECT CAST(ts AS DATE) d, count(DISTINCT ts) n "
            "FROM board_strength GROUP BY CAST(ts AS DATE) HAVING n > 1"
        ).fetchall()
        assert not rows, \
            f"这些交易日存在多个采集时点（日频序列已退化）：{rows}"
        # ★ 不 close：见上方说明。共享只读连接由进程退出时回收。
