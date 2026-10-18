# -*- coding: utf-8 -*-
"""★ K 线标注渲染器测试（`scripts/render_kline.py`）。

★★★ 这个项目的定位边界（D13 锁定）
------------------------------------------------
Hawk 是**决策副驾**，不是自动交易机器人。
`overlay.Mark` 里卖点带 `advisory=True`、止损是「**参考位**」而非指令。
渲染器一旦输出「建议卖出」「应立即减仓」这类祈使表述，
系统就从「副驾」变成了「交易员」——
**这不是文案偏好，是项目定位的边界**，故用断言守护而非人工检查。

★ 为什么必须把这些写成测试
--------------------------
人工检查会漏。而这类越界往往在「顺手加一句提示」时发生，
不会有任何报错。本文件把「不得出现什么」固定下来。

运行： pytest tests/test_kline_ui.py -v
"""
from __future__ import annotations

import ast
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = ROOT / "scripts" / "render_kline.py"


@pytest.fixture(scope="module")
def mod():
    import importlib.util

    spec = importlib.util.spec_from_file_location("rk", SRC)
    m = importlib.util.module_from_spec(spec)
    sys.modules["rk"] = m
    spec.loader.exec_module(m)
    return m


# ================================================================== 假数据
class _Mark:
    def __init__(self, kind, date, price, level="strong",
                 signal_kind="3buy", confidence=0.8, advisory=False,
                 stop_basis="", stop_note="", reason="测试用标注"):
        # ★ 字段必须与 `overlay.Mark` 完全一致 ——
        #   少一个（如 reason）就会在渲染时 AttributeError，
        #   而错误信息指向渲染器内部，看不出是「测试替身缺字段」。
        self.kind, self.date, self.price = kind, date, price
        self.level, self.signal_kind = level, signal_kind
        self.confidence, self.advisory = confidence, advisory
        self.stop_basis, self.stop_note = stop_basis, stop_note
        self.reason = reason


class _Overlay:
    """最小的 `KlineOverlay` 替身（只含渲染器用到的字段）。"""

    def __init__(self):
        self.code, self.name, self.as_of = "000001", "测试", "2026-10-08"
        d0 = date(2026, 1, 1)
        self.bars = []
        px = 10.0
        for i in range(60):
            px *= 1.005
            self.bars.append({
                "date": (d0 + timedelta(days=i)).isoformat(),
                "open": px * 0.999, "high": px * 1.01,
                "low": px * 0.99, "close": px, "volume": 1e6 + i,
            })
        self.zhongshus = [{
            "idx": 0, "low": self.bars[10]["low"], "high": self.bars[30]["high"],
            "start_date": self.bars[10]["date"], "end_date": self.bars[30]["date"],
        }]
        self.bis = [{"dir": "up", "high": self.bars[30]["high"],
                     "low": self.bars[10]["low"],
                     "start_date": self.bars[10]["date"],
                     "end_date": self.bars[30]["date"]}]
        self.marks = [
            _Mark("buy", self.bars[50]["date"], self.bars[50]["close"],
                  signal_kind="3buy"),
            _Mark("sell", self.bars[40]["date"], self.bars[40]["close"],
                  signal_kind="3sell", advisory=True),
        ]
        self.position = "中枢上方"
        self.current_stop_refs = [
            _Mark("stop", self.bars[50]["date"], 9.5,
                  stop_basis="atr", stop_note="波动率自适应"),
            _Mark("stop", self.bars[50]["date"], 9.2,
                  stop_basis="zhongshu_low", stop_note="跌破中枢下沿"),
        ]
        self.disclaimer = "标注为系统分析结果，不构成投资建议；卖点为提醒而非指令。"


@pytest.fixture(scope="module")
def page(mod):
    return mod.render("000001", "测试", _Overlay())


# ================================================================== D13 边界
class TestAdvisoryBoundary:
    """★★★ 不得出现祈使式交易指令（D13）。"""

    FORBIDDEN = ["建议卖出", "应立即", "必须卖出", "立即减仓",
                 "清仓", "赶紧买", "务必买入", "全仓"]

    @pytest.mark.parametrize("word", FORBIDDEN)
    def test_page_has_no_imperative_trading_words(self, page, word):
        assert word not in page, \
            f"页面出现祈使式交易指令「{word}」—— 违反 D13（副驾定位）"

    def test_sell_mark_is_labelled_as_reminder(self, page):
        """卖点必须明确标注为「提醒」。"""
        assert "卖·提醒" in page, "卖点标签须含「提醒」"
        assert "提醒" in page and "指令" in page, "须说明是提醒而非指令"

    def test_disclaimer_present(self, page):
        """★ 免责声明必须出现在页面上（显眼位置）。"""
        assert "不构成投资建议" in page
        assert "由你决定" in page or "而非指令" in page

    def test_stop_refs_shown_as_options_not_choice(self, page):
        """★ 止损三档须**并列**展示，不替用户选。"""
        assert "并列" in page, "须说明三档并列"
        assert "atr" in page and "zhongshu_low" in page, "两档依据都要列出"

    def test_sell_marker_color_is_not_alarm_red(self, mod):
        """★ 卖点用灰蓝而非红色 —— 提醒不是警报。"""
        assert mod.C_SELL != mod.C_BUY, \
            "卖点与买点必须不同色：买点=行动信号，卖点=提醒"
        assert mod.C_SELL == "#5a7a9a", \
            f"卖点色应固定为灰蓝 #5a7a9a，实得 {mod.C_SELL}"


# ================================================================== 配色
class TestColorConvention:
    """★ 涨红跌绿（中国习惯，全局约定不得反转）。"""

    def test_up_is_red_down_is_green(self, mod):
        """★ 涨红跌绿：涨的 **R** 分量高，跌的 **G** 分量高。"""
        ru = int(mod.C_UP[1:3], 16)      # 涨的 red
        gu = int(mod.C_UP[3:5], 16)      # 涨的 green
        rd = int(mod.C_DOWN[1:3], 16)    # 跌的 red
        gd = int(mod.C_DOWN[3:5], 16)    # 跌的 green
        assert ru > gu, f"涨应为红色系（R>G）：{mod.C_UP}"
        assert gd > rd, f"跌应为绿色系（G>R）：{mod.C_DOWN}"

    def test_colors_are_documented(self, mod):
        src = SRC.read_text(encoding="utf-8")
        assert "涨红跌绿" in src, "须注明配色约定及其来源"


# ================================================================== 渲染正确性
class TestRendering:
    def test_produces_valid_html(self, page):
        assert page.startswith("<!doctype html>")
        assert "<svg" in page and "</svg>" in page
        assert page.count("<html") == 1

    def test_kline_count_matches_bars(self, mod, page):
        """★ 每根 K 线都要画出来。"""
        ov = _Overlay()
        html = mod.render("X", "X", ov)
        # 阳线空心 + 阴线实心，两者总数 = bar数
        n_rect = len(re.findall(r"<rect x=", html))
        assert n_rect >= len(ov.bars), \
            f"应至少画 {len(ov.bars)} 根 K 线，实得 {n_rect} 个 rect"

    def test_marks_rendered_with_dates(self, page):
        for m in _Overlay().marks:
            assert m.date in page, f"标注日期 {m.date} 未出现在页面上"

    def test_empty_bars_does_not_crash(self, mod):
        ov = _Overlay()
        ov.bars = []
        html = mod.render("X", "X", ov)
        assert "无 K 线数据" in html

    def test_date_axis_rendered(self, page):
        """日期轴要标出来（否则无法定位信号发生在哪）。"""
        assert re.search(r"20\d\d-\d\d", page), "页面应含日期文本"

    def test_html_is_escaped(self, mod):
        """★ 名称/依据里的尖括号必须转义（防注入）。"""
        ov = _Overlay()
        ov.name = "<script>alert(1)</script>"
        html = mod.render("X", ov.name, ov)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


# ================================================================== 结构契约
class TestBisContract:
    """★ `ov.bis` 是**已序列化的 dict**，没有 `start_price`。

    曾误写 `b["start_price"]` ⇒ KeyError。
    正确：靠 `dir` 拼端点（up→low起/high止，down→high起/low止）。
    """

    def test_no_start_price_access(self):
        """★ 只看**代码**，不看注释（本文件的docstring 里就写着该反例）。"""
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        # ★ 键可能通过 subscript（d["k"]）或 .get("k") 访问，
        #   两种都要收集 —— 只收 subscript 会漏掉 .get 形式
        #   （实测 start_date 正是用 .get 写的）。
        keys: list[str] = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and isinstance(n.slice.value, str)):
                keys.append(n.slice.value)
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "get" and n.args
                  and isinstance(n.args[0], ast.Constant)
                  and isinstance(n.args[0].value, str)):
                keys.append(n.args[0].value)
        assert "start_price" not in keys, \
            f"ov.bis 没有 start_price；端点应由 dir + high/low 推出（已见键：{keys}）"
        assert "start_date" in keys, "应使用 start_date/end_date 做 x 轴定位"

    def test_dir_drives_endpoints(self, mod):
        """dir='up' 与 'down' 必须算出不同的端点。"""
        ov = _Overlay()
        d0 = ov.bars[10]["date"]
        d1 = ov.bars[30]["date"]
        lo, hi = ov.bars[10]["low"], ov.bars[30]["high"]
        idx_of = {b["date"]: i for i, b in enumerate(ov.bars)}
        up = mod._bis_points([{"dir": "up", "high": hi, "low": lo,
                               "start_date": d0, "end_date": d1}], idx_of)
        down = mod._bis_points([{"dir": "down", "high": hi, "low": lo,
                                 "start_date": d0, "end_date": d1}], idx_of)
        assert up[0][1] != down[0][1], "上下笔的起点价必须不同"


class TestNoSystemTime:
    """★ 铁律二：渲染器不取系统时间（as_of 由调用方传入）。"""

    def test_no_datetime_now(self):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        hits = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("now", "today")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "datetime"):
                hits.append(node.lineno)
        # main() 里的 as_of 兜底用 as_of 参数，不取系统时间
        assert not hits, f"渲染器不得取系统时间（铁律二）：line {hits}"


if __name__ == "__main__":                        # pragma: no cover
    pytest.main([__file__, "-v"])