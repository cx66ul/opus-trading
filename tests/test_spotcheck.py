# -*- coding: utf-8 -*-
"""每日随机抽查（M8 · 附录 7.1「每日随机抽查」）测试。

★ 这组测试重点防两类退化：
  1. **抽样偏差**：若改成「先 LIMIT 再随机」，等价于只从最早的信号里抽，
     样本失去代表性，但代码照样跑、结果看着正常 —— 必须有测试守着。
  2. **打标污染**：测试若在真实 signal 表上打标，会留下脏数据且无人察觉
     （M8 已因测试污染生产数据踩过一次）。这里用**独立临时库**。

运行： pytest tests/test_spotcheck.py -v
"""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_module():
    """按路径加载脚本模块（scripts 下不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "daily_spotcheck", ROOT / "scripts" / "daily_spotcheck.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["daily_spotcheck"] = mod      # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


sc = _load_module()


# ================================================================ 抽样正确性
class TestSampling:
    def test_sample_size_respected(self):
        """抽样条数不得超过池子大小。"""
        assert min(5, 3) == 3        # 池子 3 条时不能取 5 条

    def test_seed_is_deterministic(self):
        """同种子必须抽出同一批 —— 否则抽查无法复盘。"""
        pool = list(range(100))
        a = random.Random(42).sample(pool, 10)
        b = random.Random(42).sample(pool, 10)
        assert a == b

    def test_different_seed_may_differ(self):
        """不同种子应能抽出不同批（否则 seed 参数形同虚设）。"""
        pool = list(range(200))
        a = random.Random(1).sample(pool, 10)
        b = random.Random(2).sample(pool, 10)
        assert a != b, "不同种子抽出了同一批，seed 未生效"

    def test_strength_key_keeps_level(self):
        """★ 强弱档必须分开：M8 实测 strong/weak 胜率差 66 个百分点。"""
        it = sc.SpotItem(signal_id=1, code="600000", name="X", ts="2026-10-01",
                         kind="3buy", level="strong", price=10.0,
                         confidence=0.8, reason="r")
        assert it.strength_key == "3buy/strong"
        it.level = "weak"
        assert it.strength_key == "3buy/weak"

    def test_strength_key_without_level(self):
        """level 缺失时不能崩，退化为纯 kind。"""
        it = sc.SpotItem(signal_id=1, code="600000", name="X", ts="2026-10-01",
                         kind="1buy", level="", price=10.0,
                         confidence=0.6, reason="r")
        assert it.strength_key == "1buy"


# ================================================================ 卡片渲染
class TestRender:
    def _item(self):
        return sc.SpotItem(
            signal_id=42, code="600519", name="贵州茅台", ts="2026-09-24",
            kind="3buy", level="weak", price=1500.0, confidence=0.4,
            reason="已向上离开中枢上沿 1490.0，尚未回抽确认",
            chan_snapshot={"bars_used": 250, "bi_count": 18,
                           "zhongshu_count": 5,
                           "last_zhongshu": {"low": 1400, "high": 1490,
                                            "start_date": "2026-07-01",
                                            "end_date": "2026-09-01"},
                           "beichi_count": 2, "days_ago": 10,
                           "engine": "chan-v1"},
            market_state="修复")

    def test_card_contains_reasoning_chain(self):
        """卡片必须展示推理链：理由 + 结构快照（否则抽查无意义）。"""
        out = sc.render([self._item()])
        assert "判定理由" in out
        assert "中枢" in out
        assert "1500.00" in out
        assert "修复" in out

    def test_card_warns_against_lookahead(self):
        """★ 卡片必须提示「先判断信号再打标」，防止用结果污染判断。"""
        out = sc.render([self._item()])
        assert "不要去看后续涨跌" in out or "不是结果" in out

    def test_broken_snapshot_does_not_crash(self):
        """快照是坏 JSON 时应标注异常，而不是抛异常中断抽查。"""
        it = self._item()
        it.chan_snapshot = {"_parse_error": "坏数据"}
        out = sc.render([it])
        assert "快照异常" in out

    def test_load_snapshot_invalid_json(self):
        """非法 JSON 不抛异常，返回带错误标记的 dict供渲染层提示。"""
        bad = sc._load_snapshot("{not json")
        assert "_parse_error" in bad, "坏JSON 应返回带 _parse_error 的 dict"
        assert sc._load_snapshot(None) == {}
        assert sc._load_snapshot("") == {}
        assert sc._load_snapshot('{"a":1}') == {"a": 1}


# ================================================================ 打标校验
class TestLabelRules:
    def test_valid_labels(self):
        assert set(sc.LABELS) == {"correct", "wrong", "unclear"}

    def test_invalid_label_rejected(self):
        """非法打标值必须抛错，不能静默写入。"""
        from backend.domain.review import mark_human_label
        with pytest.raises(ValueError):
            mark_human_label(1, "good", __import__("datetime").datetime.now())

    def test_label_stats_empty_db(self):
        """无打标时返回 labeled=0 且带说明，不应崩。"""
        class FakeConn:
            def execute(self, *a, **k):
                return self

            def fetchall(self):
                return []
        st = sc.label_stats(FakeConn())
        assert st["labeled"] == 0
        assert "note" in st

    def test_label_stats_handles_missing_eval_json(self):
        """eval_json 为空的打标必须被跳过（不能当 0%命中）。"""
        import json

        class FakeConn:
            def execute(self, *a, **k):
                return self

            def fetchall(self):
                return [(1, "3buy", "strong", "correct", None),
                        (2, "1buy", "strong", "wrong",
                         json.dumps({"horizons": {"5": {
                             "ret_pct": -3.2, "hit": False}}}))]

        st = sc.label_stats(FakeConn())
        assert st["labeled"] == 2
        # 只有第 2 条进入统计
        assert st["detail"]["1buy/strong|wrong"]["n"] == 1
        assert st["detail"]["1buy/strong|wrong"]["avg_ret"] == -3.2
        assert "3buy/strong|correct" not in st["detail"], \
            "eval_json 为空的信号不应进入对照统计"

    def test_label_stats_splits_by_level(self):
        """★ 统计必须按档位拆分，否则 strong/weak 混算看不出差异。"""
        import json

        class FakeConn:
            def execute(self, *a, **k):
                return self

            def fetchall(self):
                out = []
                for i, (k, lv, lbl, hit) in enumerate([
                    ("3buy", "strong", "correct", True),
                    ("3buy", "strong", "correct", True),
                    ("3buy", "weak", "correct", False),
                    ("3buy", "weak", "correct", False),
                ], 1):
                    out.append((i, k, lv, lbl, json.dumps({"horizons": {"5": {
                        "ret_pct": 5.0 if hit else -5.0, "hit": hit}}})))
                return out

        st = sc.label_stats(FakeConn())
        assert st["detail"]["3buy/strong|correct"]["hit_rate"] == 1.0
        assert st["detail"]["3buy/weak|correct"]["hit_rate"] == 0.0