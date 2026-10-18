# -*- coding: utf-8 -*-
"""决策文件完整性测试。

★ 为什么需要：decisions.yaml 是**锁定需求**的单一事实来源
  （提示词第十二章「不许猜」）。它坏掉时不会让程序报错 ——
  读取方可能静默拿到 None 或跳过，于是「已确认的决策」凭空消失。
  实测踩过：编辑时把两行合并成一行，YAML 立刻语法错误。

运行： pytest tests/test_decisions.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONF = ROOT / "config" / "decisions.yaml"


@pytest.fixture(scope="module")
def conf() -> dict:
    if not CONF.exists():
        pytest.skip("decisions.yaml 不存在")
    return yaml.safe_load(CONF.read_text(encoding="utf-8"))


class TestDecisionsFile:
    def test_yaml_parses(self):
        """★ 最基本的：文件必须能解析。

        合并两行这种编辑失误会让整个文件无法加载，
        而所有「已确认决策」随之消失。
        """
        data = yaml.safe_load(CONF.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_has_decisions(self, conf):
        assert "decisions" in conf, "缺少 decisions 段"
        assert conf["decisions"], "decisions 段为空"

    def test_each_decision_complete(self, conf):
        """每条决策必须四要素齐全，便于追溯「为什么这么定」。"""
        for key, d in conf["decisions"].items():
            assert isinstance(d, dict), f"{key} 不是 dict"
            for field in ("topic", "choice", "detail", "status"):
                assert field in d, f"{key} 缺少 {field}"
                assert d[field], f"{key}.{field} 为空"

    def test_status_not_empty(self, conf):
        """★ status 必须标明决策是否真的定了。

        「待用户提供」与「已定」对后续开发的影响完全不同 ——
        混在一起会让人以为阻塞已解除。
        """
        for key, d in conf["decisions"].items():
            assert isinstance(d["status"], str) and d["status"].strip()
            if d["status"] == "已定":
                assert "已定" in d["status"]

    def test_d9_locked(self, conf):
        """★ D9（策略权重）用户 2026-09-09 二次确认：回测自动定权。"""
        d9 = conf["decisions"].get("D9")
        assert d9 is not None, "缺 D9"
        assert "自动定权" in d9["choice"]
        assert "不做手工" in d9["choice"] or "不做手工调权" in d9["choice"]

    def test_d13_no_auto_sell(self, conf):
        """★★ D13（A+C 组合）：系统不自动卖出，卖点只标注。

        这是产品定位级的决策 —— 一旦被改成自动卖出，
        系统就从「副驾」变成「交易员」。
        """
        d13 = conf["decisions"].get("D13")
        assert d13 is not None, "缺 D13"
        assert "不自动卖出" in d13["choice"] or "不自动卖" in d13["choice"]
        assert "标注" in d13["choice"], "必须强调「K线标注卖点」"
        assert "参考" in d13["choice"], "止损必须定位为「参考位」"

    def test_d13_mentions_all_three_stop_bases(self, conf):
        """止损三种取法都要写进决策，方便追溯为什么不给单一值。"""
        d13 = conf["decisions"]["D13"]
        for kw in ("中枢下沿", "前一笔低点", "ATR"):
            assert kw in d13["detail"], f"D13 未记录止损取法 {kw}"

    def test_d14_stop_five_percent(self, conf):
        """★ D14：止损固定 5%（用户 2026-10-09 拍定，不做自动寻优）。"""
        d14 = conf["decisions"].get("D14")
        assert d14 is not None, "缺 D14"
        assert "5%" in d14["choice"]

    def test_d14_records_why_not_walk_forward(self, conf):
        """★ 必须记录「为什么不能用 walk_forward 做缠论样本外」。

        这是最容易被人「顺手改回去」的地方 ——���
        切片重跑会让缠论成交从 2 笔掉到 0 笔（片段太短），
        得到「样本外无法验证」的假结论。
        """
        d14 = conf["decisions"]["D14"]
        assert "walk_forward" in d14["detail"]
        assert "切片" in d14["detail"]

    def test_d14_records_instability_finding(self, conf):
        """★ 必须记录「结果随样本量震荡」这个关键发现。

        60 只通过、200 只不通过、400 只又通过 ⇒ 无稳定优势。
        不记下来，后人会只取那个「通过」的档位当结论。
        """
        d14 = conf["decisions"]["D14"]
        assert "震荡" in d14["detail"] or "巧合" in d14["detail"]

    def test_d14_correction_recorded(self, conf):
        """★★ D14 的「unstable」结论已被 D17 修正 —— 必须记录，否则会被沿用。

        D14 测的是**绝对收益**（-0.19% ~ +1.00%，符号穿越零线）；
        D17 补上大盘基准后，**超额收益 7/7 档全为正**（+1.15% ~ +2.53%）
        ⇒ 「策略没有 edge」是口径造成的误判。
        """
        d17 = conf["decisions"].get("D17")
        assert d17 is not None, "缺 D17"
        assert "D14" in d17["detail"], "D17 必须指明它修正了 D14"
        assert "超额" in d17["detail"], "必须写明是超额口径修正了绝对收益口径"
        assert "7/7" in d17["detail"] or "全为正" in d17["detail"]

    def test_d17_records_no_lookahead_constraint(self, conf):
        """★ 必须记下「门控不能用段标签」—— 这是最容易改回去的地方。"""
        d17 = conf["decisions"]["D17"]
        assert "段标签" in d17["detail"]
        assert "Jaccard" in d17["detail"] or "jaccard" in d17["detail"]
        assert "84.9" in d17["detail"]

    def test_d17_records_gate_value_is_small(self, conf):
        """★ 必须记录「门控增益仅 0.14pp、不足以支撑上线」。

        否则后人会只看到「6/6 稳定性通过」就以为门控该上线，
        忽略它把胜率从 32% 砍到 18.8%。
        """
        d17 = conf["decisions"]["D17"]
        assert "0.14" in d17["detail"], "必须写明增益幅度"
        assert "胜率" in d17["detail"], "必须写明胜率代价"
        assert "不建议上线" in d17["choice"] or "暂不门控" in d17["status"]

    def test_pending_inputs_is_list(self, conf):
        assert isinstance(conf.get("pending_inputs", []), list)

    def test_resolved_is_list(self, conf):
        assert isinstance(conf.get("resolved", []), list)
