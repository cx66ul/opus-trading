# -*- coding: utf-8 -*-
"""
Hawk · 策略接口 + 条件 DSL 测试（附录 D · M9）

★ 这组测试守护的是**三类已经真实踩过的坑**，每条都标了「实测」：
  1. 缺失数据必须判False —— 若默认 True，规则会在数据缺失时全部通过，
     回测虚高且无从察觉（附录 D.3 第1 条）。
  2. 布尔谓词不能走数值比较 —— 否则「排除ST / 排除涨停」这类
     硬性风控会**静默失效**（返回 False 但原因错了，规则照样放行）。
  3. custom 表达式必须走 AST 白名单 —— 规则来自语料蒸馏（M10），
     属于外部输入，`eval` 一行即可删库。

运行： pytest tests/test_dsl.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.domain.dsl import (                          # noqa: E402
    PREDICATES,
    EvalResult,
    evaluate,
    safe_eval,
    validate_rule,
)
from backend.domain.strategy import (                     # noqa: E402
    ChanlunBase,
    StrategyContext,
    StrategyDecision,
    TraderStrategy,
    all_strategies,
    get_strategy,
    register,
    unregister,
)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 8, 15, 0, tzinfo=CST)


def mk_ctx(**kw) -> StrategyContext:
    """构造上下文：默认所有字段都有值，测试可按需覆盖为 None。"""
    base = dict(
        code="600519", as_of=AS_OF,
        closes=[1400.0 + (i % 5) * 10 for i in range(60)],
        highs=[1410.0 + (i % 5) * 10 for i in range(60)],
        lows=[1390.0 + (i % 5) * 10 for i in range(60)],
        volumes=[1e6 * (1 + i % 3) for i in range(60)],
        dates=[f"2026-{1 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(60)],
        is_st=False, is_suspend=False, is_new=False,
        float_mv=1.8e11, total_mv=1.9e11, turnover=0.8,
        pct_chg_today=1.2, rps=88.0,
        market_state="修复", market_score=62.0,
        board_rank=5, board_strength=70.0, board_rank_change=2,
        chan_signals=[{"kind": "3buy", "level": "strong",
                       "date": "2026-10-08", "reason": "回抽不破上沿"}],
        chan_zhongshu_pos="above", beichi_types=["bottom_beichi"],
        chan_stage="bi",
        news_direction="bull", news_strength=4.0, news_within_days=1,
        limit_up_days=0,
    )
    base.update(kw)
    return StrategyContext(**base)


def rule_with(clause: dict, rid: str = "t") -> dict:
    return {"rule_id": rid, "name": "测试规则", "if": clause,
            "then": {"action": "BUY", "position": 0.3}}


# ================================================================ 缺失数据
class TestMissingData:
    """★ 附录 D.3 第 1 条：缺失 → False + warning，绝不默认 True。"""

    def test_missing_float_mv_is_false_not_true(self):
        ctx = mk_ctx(float_mv=None)
        r = evaluate(rule_with({"all_of": [
            {"float_mv": {"op": ">=", "value": 1e10}}]}), ctx)
        assert r.matched is False
        assert any(p.missing for p in r.preds)
        assert any("缺失" in w for w in r.warnings)

    def test_missing_rps_is_false(self):
        ctx = mk_ctx(rps=None)
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": ">=", "value": 80}}]}), ctx)
        assert r.matched is False

    def test_missing_inside_none_of_still_passes(self):
        """★ none_of 里缺失 → False ⇒ 整体 True。

        这是最容易出错的语义：风控规则常写成
        `none_of: [涨停]`。若「涨停数据缺失」被当成 True，
        就会把「不知道有没有涨停」误判成「涨停了」而拒绝买入 ——
        方向反了，风控从「保守」变成「一刀切」。
        """
        ctx = mk_ctx(limit_up_days=None)
        r = evaluate(rule_with({"none_of": [{"limit_up": {"days": 1}}]}), ctx)
        assert r.matched is True, "缺失判 False → none_of 应通过"

    def test_zero_is_not_missing(self):
        """★ 0 是合法值（今天没涨停），不能当缺失。

        若把 0 当缺失，`none_of: [limit_up]` 在「确实没涨停」时
        也判True，等于该风控永远不生效 —— 而且完全静默。
        """
        ctx = mk_ctx(limit_up_days=0)
        r = evaluate(rule_with({"none_of": [{"limit_up": {"days": 1}}]}), ctx)
        pred = [p for p in r.preds if p.name == "limit_up"][0]
        assert pred.missing is False, "0 不可被当作缺失"
        assert r.matched is True


# ================================================================ 布尔谓词
class TestBoolPredicates:
    """★ 实测坑：布尔谓词若走数值比较路径，风控会静默失效。"""

    def test_is_st_true_blocks(self):
        ctx = mk_ctx(is_st=True)
        r = evaluate(rule_with({"none_of": [{"is_st": {}}]}), ctx)
        assert r.matched is False, "ST 股必须被 none_of 拦住"
        pred = [p for p in r.preds if p.name == "is_st"][0]
        assert pred.missing is False, "is_st 有明确值，不该报缺失"
        assert pred.value is True

    def test_is_st_false_passes(self):
        ctx = mk_ctx(is_st=False)
        r = evaluate(rule_with({"none_of": [{"is_st": {}}]}), ctx)
        assert r.matched is True
        pred = [p for p in r.preds if p.name == "is_st"][0]
        assert pred.value is False, "非 ST 应判 False（不构成拦截）"

    def test_limit_up_one_day_blocks(self):
        ctx = mk_ctx(limit_up_days=1)
        r = evaluate(rule_with({"none_of": [{"limit_up": {"days": 1}}]}), ctx)
        assert r.matched is False, "近 1 日涨停应被拦"

    def test_limit_up_bool_not_compared_as_number(self):
        """★ 回归测试：limit_up 返回 bool，不能再被拿去比数值。

        旧实现框架会拿 actual=False 和 threshold=1 比 >=，
        bool 走数值路径必然「无法比较」→ 判 False + warning。
        结果虽然也是 False，但**原因错了**，
        且 UI 上显示为「数据缺失」，会把风控失效误诊成数据问题。
        """
        ctx = mk_ctx(limit_up_days=0)
        r = evaluate(rule_with({"all_of": [{"limit_up": {"days": 1}}]}), ctx)
        pred = [p for p in r.preds if p.name == "limit_up"][0]
        assert pred.missing is False
        assert pred.error == ""
        assert not any("无法比较" in w for w in r.warnings)

    def test_bare_key_shorthand(self):
        """YAML 裸键 `- is_st` 应能解析（不报 unpack 错误）。"""
        ctx = mk_ctx(is_st=True)
        r = evaluate(rule_with({"none_of": ["is_st"]}), ctx)
        assert r.matched is False

    def test_bool_value_shorthand(self):
        """`- is_st: true` 应能解析。"""
        ctx = mk_ctx(is_st=True)
        r = evaluate(rule_with({"none_of": [{"is_st": True}]}), ctx)
        assert r.matched is False


# ================================================================ 逻辑子句
class TestGroups:
    def test_all_of(self):
        ctx = mk_ctx(rps=90.0, float_mv=1e11)
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": ">=", "value": 80}},
            {"float_mv": {"op": ">=", "value": 1e10}}]}), ctx)
        assert r.matched is True

    def test_all_of_one_fails(self):
        ctx = mk_ctx(rps=10.0, float_mv=1e11)
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": ">=", "value": 80}},
            {"float_mv": {"op": ">=", "value": 1e10}}]}), ctx)
        assert r.matched is False

    def test_any_of(self):
        ctx = mk_ctx(rps=10.0, float_mv=1e11)
        r = evaluate(rule_with({"any_of": [
            {"rps": {"op": ">=", "value": 80}},
            {"float_mv": {"op": ">=", "value": 1e10}}]}), ctx)
        assert r.matched is True

    def test_none_of(self):
        ctx = mk_ctx(is_st=True, limit_up_days=0)
        r = evaluate(rule_with({"none_of": [
            {"is_st": {}}, {"limit_up": {"days": 1}}]}), ctx)
        assert r.matched is False

    def test_empty_all_of_is_true(self):
        """空 all_of 无约束 → True（不能因为没写条件就判死）。"""
        assert evaluate(rule_with({"all_of": []}), mk_ctx()).matched is True

    def test_empty_none_of_is_true(self):
        assert evaluate(rule_with({"none_of": []}), mk_ctx()).matched is True

    def test_empty_any_of_is_neutral(self):
        """★ 空/缺省 any_of 应视为「无附加要求」→ 不影响命中。

        附录 D.1 写明 any_of 可省略。若把省略当「空any_of=False」，
        再与 all_of 取 AND，所有没写 any_of 的规则**永不命中**，
        且 preds 全为 True、不报错 —— 极难定位（实测踩过）。
        """
        assert evaluate(rule_with({"any_of": []}), mk_ctx()).matched is True

    def test_all_and_none_combined(self):
        ctx = mk_ctx(rps=90.0, is_st=True)
        r = evaluate(rule_with({
            "all_of": [{"rps": {"op": ">=", "value": 80}}],
            "none_of": [{"is_st": {}}]}), ctx)
        assert r.matched is False


# ================================================================ 数值谓词
class TestNumericPredicates:
    def test_price_vs_ma(self):
        ctx = mk_ctx(closes=[100.0] * 20)
        assert ctx.price_vs_ma(5) == pytest.approx(0.0)
        ctx = mk_ctx(closes=[100.0] * 19 + [110.0])
        assert ctx.price_vs_ma(20) > 0.09

    def test_ma_insufficient_returns_none(self):
        """★ 数据不足必须 None，不能返回 0（缺失≠0）。"""
        ctx = mk_ctx(closes=[100.0, 101.0])
        assert ctx.ma(20) is None
        assert ctx.price_vs_ma(20) is None

    def test_vol_ratio_zero_denominator(self):
        """★ 分母趋零会产生无意义巨值（背驰比值 25.156 的教训）。

        均量为 0 时必须 None，而不是除零或返回极大值。
        """
        ctx = mk_ctx(volumes=[0.0] * 20)
        assert ctx.vol_ratio(5) is None

    def test_vol_ratio_normal(self):
        """分母不含当前这根（否则单根暴量会被自己摊平，阈值永不触发）。"""
        # 需 21 根：前 20 根作基准（均1e6）+ 当前 1 根
        ctx = mk_ctx(volumes=[1e6] * 20 + [2e6])
        assert ctx.vol_ratio(20) == pytest.approx(2.0)

    def test_vol_ratio_spike_can_exceed_threshold(self):
        """★ 回归：单根暴量必须能突破阈值。

        若分母含当前这根，N=5 时最大比值≈1.25，
        任何「暴量」阈值都形同虚设（实测发现）。
        """
        ctx = mk_ctx(volumes=[1e6] * 59 + [50e6])
        vr = ctx.vol_ratio(5)
        assert vr is not None and vr > 5.0, f"暴量应能突破阈值，实得 {vr}"

    def test_pct_change_n(self):
        ctx = mk_ctx(closes=[100.0] * 10)
        assert ctx.pct_change_n(5) == pytest.approx(0.0)
        ctx = mk_ctx(closes=[100.0] * 10 + [110.0])
        assert ctx.pct_change_n(10) == pytest.approx(0.1, abs=1e-6)

    def test_pct_change_n_insufficient(self):
        ctx = mk_ctx(closes=[100.0, 101.0, 102.0])
        assert ctx.pct_change_n(10) is None

    def test_between(self):
        ctx = mk_ctx(rps=85.0)
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": "between", "value": [80, 90]}}]}), ctx)
        assert r.matched is True

    def test_between_bad_threshold_does_not_crash(self):
        """★ between 写成标量时应报可读错误，不是 unpack 异常。"""
        ctx = mk_ctx(rps=85.0)
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": "between", "value": 80}}]}), ctx)
        assert r.matched is False
        assert any("between" in w for w in r.warnings)

    def test_in_operator(self):
        ctx = mk_ctx(market_state="发酵")
        r = evaluate(rule_with({"all_of": [
            {"market_state": {"in": ["启动", "发酵"]}}]}), ctx)
        assert r.matched is True
        ctx2 = mk_ctx(market_state="冰点")
        r2 = evaluate(rule_with({"all_of": [
            {"market_state": {"in": ["启动", "发酵"]}}]}), ctx2)
        assert r2.matched is False


# ================================================================ 缠论谓词
class TestChanPredicates:
    def test_chan_signal_strong_hit(self):
        ctx = mk_ctx(chan_signals=[
            {"kind": "3buy", "level": "strong", "date": "2026-10-08"}])
        r = evaluate(rule_with({"all_of": [
            {"chan_signal": {"type": ["3buy"], "level": "strong"}}]}), ctx)
        assert r.matched is True

    def test_chan_signal_weak_does_not_match_strong(self):
        """★ 弱三买不能匹配 level=strong（M8 实测胜率差 66 个百分点）。"""
        ctx = mk_ctx(chan_signals=[
            {"kind": "3buy", "level": "weak", "date": "2026-10-08"}])
        r = evaluate(rule_with({"all_of": [
            {"chan_signal": {"type": ["3buy"], "level": "strong"}}]}), ctx)
        assert r.matched is False

    def test_chan_signal_no_level_keeps_weak(self):
        """不指定 level 时不按强度过滤（避免悄悄把 weak 混进买点）。"""
        ctx = mk_ctx(chan_signals=[
            {"kind": "3buy", "level": "weak", "date": "2026-10-08"}])
        r = evaluate(rule_with({"all_of": [
            {"chan_signal": {"type": ["3buy"]}}]}), ctx)
        assert r.matched is True

    def test_chan_signal_empty_context(self):
        ctx = mk_ctx(chan_signals=[])
        r = evaluate(rule_with({"all_of": [
            {"chan_signal": {"type": ["3buy"]}}]}), ctx)
        assert r.matched is False

    def test_zhongshu_pos(self):
        for pos, expect in (("above", True), ("inside", False), ("below", False)):
            ctx = mk_ctx(chan_zhongshu_pos=pos)
            r = evaluate(rule_with({"all_of": [
                {"chan_zhongshu_pos": {"pos": "above"}}]}), ctx)
            assert r.matched is expect, f"pos={pos}"

    def test_beichi(self):
        ctx = mk_ctx(beichi_types=["bottom_beichi"])
        r = evaluate(rule_with({"all_of": [
            {"beichi": {"type": "bottom_beichi"}}]}), ctx)
        assert r.matched is True
        r2 = evaluate(rule_with({"all_of": [
            {"beichi": {"type": "top_beichi"}}]}), ctx)
        assert r2.matched is False


# ================================================================ custom 安全
class TestCustomSafeEval:
    """★ 规则来自语料蒸馏（M10），属外部输入 —— 禁止 eval 裸执行。"""

    def test_simple_expression(self):
        assert safe_eval("x > 1", {"x": 2.0}) is True
        assert safe_eval("x * 2", {"x": 3.0}) == 6.0

    def test_whitelist_functions(self):
        assert safe_eval("max(1, 2)", {}) == 2
        assert safe_eval("abs(-3)", {}) == 3

    def test_reject_import(self):
        with pytest.raises(ValueError):
            safe_eval("__import__('os').system('echo x')", {})

    def test_reject_attribute_access(self):
        """★ 属性访问是 __class__.__bases__ 逃逸的入口，必须禁。"""
        with pytest.raises(ValueError):
            safe_eval("x.__class__", {"x": 1})

    def test_reject_unknown_name(self):
        with pytest.raises(ValueError):
            safe_eval("open('/etc/passwd')", {})

    def test_reject_lambda(self):
        with pytest.raises(ValueError):
            safe_eval("lambda: 1", {})

    def test_reject_subscript_on_module(self):
        """★ `__builtins__` 必须在**求值前**被拒，而不是靠运行期报错。

        这是个很隐蔽的坑：若用 `x['__builtins__']` 且 x 是 int，
        会在运行期抛 TypeError（'int' object is not subscriptable），
        看起来「被拒绝了」，实际是**表达式根本执行不了**——
        用 dict 一试就会暴露。所以测试必须用能真正执行的下标场景。
        """
        with pytest.raises(ValueError, match="敏感名"):
            safe_eval("x['__builtins__']", {"x": {"a": 1}})

    def test_reject_dunder_string_constant(self):
        """凡是 dunder 属性名出现在字符串常量里，一律拒绝。"""
        for key in ("__class__", "__globals__", "__subclasses__",
                    "__reduce__", "__mro__"):
            with pytest.raises(ValueError, match="敏感名"):
                safe_eval(f"x['{key}']", {"x": {"a": 1}})

    def test_dangerous_check_is_not_just_runtime_error(self):
        """★ 确认拦截来自静态检查：合法对象也必须被拒。"""
        obj = {"__builtins__": {"__import__": __import__}}
        with pytest.raises(ValueError, match="敏感名"):
            safe_eval("x['__builtins__']", {"x": obj})

    def test_custom_predicate_in_rule(self):
        ctx = mk_ctx(rps=85.0, float_mv=1e11)
        r = evaluate(rule_with({"all_of": [
            {"custom": {"expr": "rps > 80 and float_mv > 1e10"}}]}), ctx)
        assert r.matched is True

    def test_custom_predicate_error_is_false_not_crash(self):
        ctx = mk_ctx()
        r = evaluate(rule_with({"all_of": [
            {"custom": {"expr": "1/0"}}]}), ctx)
        assert r.matched is False
        assert r.error or any("错误" in w for w in r.warnings)


# ================================================================ schema 校验
class TestValidateRule:
    def test_valid_rule(self):
        assert validate_rule(rule_with({"all_of": [
            {"rps": {"op": ">=", "value": 80}}]})) == []

    def test_missing_rule_id(self):
        assert any("rule_id" in e for e in validate_rule({"if": {"all_of": []}}))

    def test_unknown_predicate(self):
        errs = validate_rule(rule_with({"all_of": [{"nonexistent": {}}]}))
        assert any("未知谓词" in e for e in errs)

    def test_unknown_top_field(self):
        errs = validate_rule({**rule_with({"all_of": []}), "bogus": 1})
        assert any("bogus" in e for e in errs)

    def test_bad_action(self):
        errs = validate_rule({**rule_with({"all_of": []}),
                               "then": {"action": "HODL"}})
        assert any("action" in e for e in errs)

    def test_position_out_of_range(self):
        errs = validate_rule({**rule_with({"all_of": []}),
                               "then": {"action": "BUY", "position": 1.5}})
        assert any("position" in e for e in errs)

    def test_custom_syntax_error(self):
        errs = validate_rule(rule_with({"all_of": [
            {"custom": {"expr": "x ==="}}]}))
        assert any("语法错误" in e for e in errs)

    def test_custom_unsafe_expression(self):
        errs = validate_rule(rule_with({"all_of": [
            {"custom": {"expr": "__import__('os')"}}]}))
        assert any("不安全" in e for e in errs)

    def test_validate_reports_all_problems(self):
        """★ 校验器应一次报出所有问题，而非遇错即停。"""
        errs = validate_rule({
            "name": "无 id", "bogus": 1,
            "if": {"all_of": [{"nope": {}}]},
            "then": {"action": "XX", "position": 9}})
        assert len(errs) >= 3, f"应报多个问题，实得 {errs}"


# ================================================================ 可解释性
class TestExplain:
    def test_explain_contains_all_preds(self):
        """★ 第十二条第 4 条：系统必须能回答「为什么」。"""
        ctx = mk_ctx(rps=10.0)
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": ">=", "value": 80}},
            {"float_mv": {"op": ">=", "value": 1e10}}]}), ctx)
        out = r.explain()
        assert "rps" in out and "float_mv" in out
        assert "未命中" in out

    def test_explain_shows_missing_warning(self):
        ctx = mk_ctx(rps=None)
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": ">=", "value": 80}}]}), ctx)
        assert "缺失" in r.explain()

    def test_to_json_roundtrip(self):
        import json
        ctx = mk_ctx()
        r = evaluate(rule_with({"all_of": [
            {"rps": {"op": ">=", "value": 80}}]}), ctx)
        d = json.loads(json.dumps(r.to_json()))
        assert d["rule_id"] == "t"
        assert isinstance(d["preds"], list) and len(d["preds"]) == 1
        assert "value" in d["preds"][0]

    def test_unknown_predicate_does_not_crash(self):
        r = evaluate(rule_with({"all_of": [{"bogus_pred": {}}]}), mk_ctx())
        assert r.matched is False
        assert any("未知谓词" in w for w in r.warnings)


# ================================================================ 策略接口
class TestStrategyRegistry:
    def test_base_registered(self):
        assert "chanlun_base" in all_strategies()

    def test_get_strategy(self):
        s = get_strategy("chanlun_base")
        assert isinstance(s, ChanlunBase)
        assert s.strategy_id == "chanlun_base"

    def test_unknown_strategy_raises(self):
        with pytest.raises(KeyError):
            get_strategy("no_such_strategy")

    def test_register_new_strategy_without_touching_core(self):
        """★ M9 验收核心：不改核心代码即可接入新策略。"""
        @register
        class MyStrat(TraderStrategy):
            strategy_id = "test_my_001"
            name = "测试策略"

            def default_params(self):
                return {"threshold": 50.0}

            def decide(self, ctx):
                vr = ctx.vol_ratio(5) or 1.0
                return StrategyDecision(
                    strategy_id=self.strategy_id, code=ctx.code,
                    as_of=ctx.as_of.isoformat(), action="BUY",
                    score=vr * 10, position=0.1, reasons=["测试"])

        try:
            assert "test_my_001" in all_strategies(), "注册后应立即可见"
            s = get_strategy("test_my_001")
            d = s.decide(mk_ctx())
            assert d.action == "BUY"
            assert d.reasons == ["测试"]
            assert s.params["threshold"] == 50.0
        finally:
            unregister("test_my_001")
        assert "test_my_001" not in all_strategies()

    def test_register_requires_strategy_id(self):
        with pytest.raises(ValueError):
            @register
            class NoId(TraderStrategy):
                name = "无 id"

                def decide(self, ctx):
                    return None

    def test_duplicate_id_rejected(self):
        with pytest.raises(ValueError):
            @register
            class Dup(TraderStrategy):
                strategy_id = "chanlun_base"

                def decide(self, ctx):
                    return None

    def test_param_hash_changes_with_params(self):
        a = get_strategy("chanlun_base")
        b = ChanlunBase(base_position=0.9)
        assert a.param_hash() != b.param_hash(), "参数变了指纹必须变"

    def test_param_hash_stable(self):
        assert get_strategy("chanlun_base").param_hash() == \
            get_strategy("chanlun_base").param_hash()


# ================================================================ 基线策略
class TestChanlunBase:
    def test_buy_on_strong_3buy(self):
        d = get_strategy("chanlun_base").decide(mk_ctx())
        assert d.action == "BUY"
        assert d.ref_rules == ["chanlun_base"]
        assert d.reasons, "必须给出理由（第十二章第 4 条）"

    def test_hold_without_signal(self):
        d = get_strategy("chanlun_base").decide(mk_ctx(chan_signals=[]))
        assert d.action == "HOLD"
        assert d.position == 0.0

    def test_weak_3buy_does_not_buy(self):
        """★ 与 M8 结论一致：weak 是观察态，不能当买点。"""
        d = get_strategy("chanlun_base").decide(mk_ctx(
            chan_signals=[{"kind": "3buy", "level": "weak",
                           "date": "2026-10-08"}]))
        assert d.action == "HOLD"

    def test_sell_takes_priority_over_buy(self):
        """★ 同时有买点和卖点时必须先卖。

        否则会出现「一边发买入信号一边发卖出信号」的矛盾输出，
        实盘无法执行。
        """
        d = get_strategy("chanlun_base").decide(mk_ctx(chan_signals=[
            {"kind": "3buy", "level": "strong", "date": "2026-10-08"},
            {"kind": "3sell", "level": "strong", "date": "2026-10-08"}]))
        assert d.action == "SELL"

    def test_st_rejected(self):
        d = get_strategy("chanlun_base").decide(mk_ctx(is_st=True))
        assert d.action == "HOLD"
        assert "ST" in d.risk_note

    def test_suspend_rejected(self):
        d = get_strategy("chanlun_base").decide(mk_ctx(is_suspend=True))
        assert d.action == "HOLD"
        assert "停牌" in d.risk_note

    def test_bad_market_state_caps_position(self):
        ok = get_strategy("chanlun_base").decide(mk_ctx(market_state="修复"))
        bad = get_strategy("chanlun_base").decide(mk_ctx(market_state="高潮"))
        assert bad.position <= 0.2, "高潮/退潮必须压低仓位"
        assert ok.position > bad.position

    def test_block_state_lowers_score(self):
        ok = get_strategy("chanlun_base").decide(mk_ctx(market_state="修复"))
        bad = get_strategy("chanlun_base").decide(mk_ctx(market_state="退潮"))
        assert bad.score < ok.score

    def test_abnormal_vol_ratio_penalized(self):
        ok = get_strategy("chanlun_base").decide(mk_ctx())
        bad = get_strategy("chanlun_base").decide(
            mk_ctx(volumes=[1e6] * 59 + [50e6]))
        assert bad.score < ok.score
        assert "量比" in bad.risk_note

    def test_below_zhongshu_penalized(self):
        above = get_strategy("chanlun_base").decide(
            mk_ctx(chan_zhongshu_pos="above"))
        below = get_strategy("chanlun_base").decide(
            mk_ctx(chan_zhongshu_pos="below"))
        assert below.score < above.score
        assert below.risk_note

    def test_risk_note_always_present(self):
        """★ risk_note 强制存在 —— 无风险提示的推荐是不可用的。"""
        for kw in ({}, {"chan_signals": []}, {"market_state": "退潮"},
                   {"chan_zhongshu_pos": "below"}):
            d = get_strategy("chanlun_base").decide(mk_ctx(**kw))
            assert d.risk_note, f"缺 risk_note: {kw}"

    def test_decide_is_pure(self):
        """★ 同输入两次输出必须一致（铁律二：纯函数）。"""
        ctx = mk_ctx()
        s = get_strategy("chanlun_base")
        a, b = s.decide(ctx), s.decide(ctx)
        assert a.to_json() == b.to_json()

    def test_decide_does_not_mutate_context(self):
        ctx = mk_ctx()
        before = (list(ctx.closes), list(ctx.volumes))
        get_strategy("chanlun_base").decide(ctx)
        assert ctx.closes == before[0] and ctx.volumes == before[1]

# ======================================================================
# 亏钱效应面积谓词（loss_area_ratio）
#
# ★ 这条谓词守着两个已经真实踩过的坑：
#   1. **单位**：存的是比例（0~1），阈值必须写 0.30。
#      写成 30 ⇒ 0.30 > 30 永远 False ⇒ 规则永不触发且**不报错**（哑弹）。
#   2. **缺失**：当日无涨跌停时值是 None，必须判False + warning，
#      绝不能当0（0 = "亏钱效应为零"，与"算不出来"是两件事）。
# ======================================================================


class TestLossAreaPredicate:
    RULE = {"rule_id": "T", "if": {"all_of": [
        {"loss_area_ratio": {"op": ">", "value": 0.30}}]}}

    def test_registered(self):
        assert "loss_area_ratio" in PREDICATES
        assert validate_rule(self.RULE) == []

    def test_ratio_above_threshold_hits(self):
        """0.45 > 0.30 ⇒ 命中。"""
        r = evaluate(self.RULE, mk_ctx(loss_area_ratio=0.45))
        assert r.matched and r.preds[0].actual == pytest.approx(0.45)

    def test_ratio_below_threshold_misses(self):
        assert not evaluate(self.RULE, mk_ctx(loss_area_ratio=0.05)).matched

    # ★★ 这是本组最关键的一条：把阈值写成 30（百分数）必须**永远不命中**，
    #    证明谓词确实存比例。若哪天有人把ctx 字段改成百分数，这条会失败。
    def test_percent_threshold_is_silent_miss(self):
        r = evaluate({"rule_id": "T", "if": {"all_of": [
            {"loss_area_ratio": {"op": ">", "value": 30}}]}},
            mk_ctx(loss_area_ratio=0.45))
        assert not r.matched, "比例 0.45 不可能 > 30；若命中说明单位搞反了"

    def test_missing_is_false_with_warning(self):
        """缺失 ⇒ False + warning，且 actual 必须是 None（不是 0）。"""
        r = evaluate(self.RULE, mk_ctx(loss_area_ratio=None))
        assert not r.matched
        p = r.preds[0]
        assert p.actual is None, "缺失必须记 None，当0 会系统性偏低"
        assert p.missing or r.warnings, "缺失必须有 warning 或 missing 标记"

    def test_explain_shows_ratio_not_percent(self):
        r = evaluate(self.RULE, mk_ctx(loss_area_ratio=0.45))
        assert "0.45" in r.explain()
