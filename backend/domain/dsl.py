# -*- coding: utf-8 -*-
"""
Hawk · 条件 DSL 解析器与求值器（附录 D · M9）

★★ 三条不可妥协的语义（附录 D.3 原文）：
   1. **缺失数据 → False + warning，绝不默认 True**
      「宁可漏触发，不能乱触发」—— 一个缺失因子若默认 True，
      会让规则在数据缺失时全部通过，回测结果虚高且无从察觉。
   2. **禁止 eval() 裸执行** —— custom 逃生舱走 AST 白名单。
   3. **必须返回中间过程** —— 每个谓词的真假都要给出来，
      否则 UI 无法回答「为什么没触发」（第十二章第 4 条：系统必须能解释）。

★ 为什么解析器不用 eval：
   规则来自语料库蒸馏（M10），属于「外部输入」。
   `eval("__import__('os').system('rm -rf /'))")` 一行就能删库。
   即便当前规则可信，也必须假设未来某条规则来自不可信来源。
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from backend.domain.strategy import StrategyContext

CST = timezone(timedelta(hours=8))


# ================================================================== 中间结果
@dataclass
class PredResult:
    """单个谓词的求值结果（含解释）。"""
    name: str
    value: bool
    actual: Any = None# 实际取到的值
    threshold: Any = None          # 阈值
    missing: bool = False          # 是否因数据缺失而判False
    error: str = ""

    def explain(self) -> str:
        if self.error:
            return f"{self.name}: 错误 {self.error}"
        if self.missing:
            return f"{self.name}: 数据缺失 → False"
        return f"{self.name}: {self.actual} {self.op_text} {self.threshold} → {self.value}"

    op_text: str = ""


@dataclass
class EvalResult:
    """整条规则的求值结果。"""
    rule_id: str
    matched: bool
    preds: list[PredResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def explain(self) -> str:
        """人类可读的求值过程（UI 直接展示）。"""
        lines = [f"规则 {self.rule_id} → {'命中' if self.matched else '未命中'}"]
        for p in self.preds:
            lines.append(f"  {'✔' if p.value else '✘'} {p.explain()}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        if self.error:
            lines.append(f"  ✘ 解析/求值错误: {self.error}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "rule_id": self.rule_id, "matched": self.matched,
            "preds": [{"name": p.name, "value": p.value, "actual": p.actual,
                       "threshold": p.threshold, "missing": p.missing,
                       "error": p.error} for p in self.preds],
            "warnings": self.warnings, "error": self.error,
        }


# ================================================================== 比较算子
def _num(x: Any) -> float | None:
    """转 float。bool 视为 0/1（Python 里 bool 是 int 子类）。"""
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _cmp(a: Any, op: str, b: Any) -> bool | None:
    """通用比较。返回 None 表示无法比较（→ 判 False并记 warning）。"""
    if op == "in":
        return a in (b or [])
    if op == "not_in":
        return a not in (b or [])

    # ★ 布尔必须走布尔比较，不能走数值路径。
    #   `is_st: {op: "==", value: true}` 若先过 _num，bool 会被转成 None
    #   （因为 Python 里 bool 是 int 的子类，直接 float(True)=1.0 虽可行，
    #    但 None 的处理让「False vs True」变成「无法比较」而不是 False），
    #   结果是「停牌/ST 过滤」这种硬性风控条件静默失效 —— 实测踩过。
    if isinstance(a, bool) or isinstance(b, bool):
        if op == "==":
            return bool(a) is bool(b) if isinstance(a, bool) and isinstance(b, bool) \
                else a == b
        if op == "!=":
            return not (bool(a) is bool(b)) if isinstance(a, bool) and isinstance(b, bool) \
                else a != b
        na, nb = _num(a), _num(b)
        if na is None or nb is None:
            return None
        return {">": na > nb, ">=": na >= nb, "<": na < nb,
                "<=": na <= nb}.get(op)

    na, nb = _num(a), _num(b)
    if na is None or nb is None:
        return None
    return {
        ">": na > nb, ">=": na >= nb, "<": na < nb, "<=": na <= nb,
        "==": na == nb, "!=": na != nb,
    }.get(op) if op in (">", ">=", "<", "<=", "==", "!=") else None


OP_TEXT = {
    ">": ">", ">=": "≥", "<": "<", "<=": "≤",
    "==": "=", "!=": "≠", "in": "∈", "not_in": "∉",
    "between": "介于", "cross_above": "上穿", "cross_below": "下穿",
    "_bool": "满足",
}


# ================================================================== 谓词实现
#每个谓词: (ctx, params) -> (actual, op, threshold)
#  缺失时返回 (None, op, threshold)，由框架统一判 False + warning。
#
# ★★ 布尔型谓词的处理方式（实测踩过的坑）：
#   有些谓词的语义本身就是「是/否」，不该再和阈值比较：
#     limit_up: 「近 N 日有没有涨停」 —— actual 是 (次数>=N) 这个布尔值，
#                若框架拿它去比 `>= days`，bool 会走数值路径，
#                而 _num 对 bool 返回 None ⇒ 判False + warning，
#                结果「排除近期涨停」这条硬性风控**静默失效**。
#   解决：这些谓词返回 op="_bool"，框架见到就跳过比较，直接取 actual。
BOOL_PREDICATES = {"limit_up"}


def _p_board_strength_rank(ctx, p):
    return ctx.board_rank, p.get("op", "<="), p.get("value")


def _p_board_strength_value(ctx, p):
    return ctx.board_strength, p.get("op", ">="), p.get("value")


def _p_board_rank_change(ctx, p):
    return ctx.board_rank_change, p.get("op", ">="), p.get("value")


def _p_price_vs_ma(ctx, p):
    return ctx.price_vs_ma(int(p.get("ma", 5))), p.get("op", ">="), p.get("value")


def _p_ma_relation(ctx, p):
    """均线关系：fast 上穿/下穿 slow。

    ★★ 修复记录（原实现有两个缺陷，均导致该谓词在实盘永不命中）：
      1. gap(i) 的守卫写成 ``i - slow < 0``，而调用处 i 恒为 -1 / -2
         （负下标），故 guard 恒真、gap 恒返回 None → seq_cross 恒 None
         → 谓词恒判「数据缺失」。正确写法是把负下标换算成绝对下标再判。
      2. op 参数被忽略：cross_below 与 cross_above 返回同一个布尔量。
         现在按 op 分别返回「上穿」或「下穿」的结论。
    返回 (bool, op, op) 交由下游 ma_relation 分支展示（op_text 显示上穿/下穿）。
    """
    fast, slow = int(p.get("fast", 5)), int(p.get("slow", 20))
    op = p.get("op", "cross_above")
    if fast >= slow or fast < 1:
        # 参数非法（如 fast>=slow）无法构成穿越关系 → 按数据缺失处理，不猜
        return None, op, None

    closes = ctx.closes
    n = len(closes)
    crossed: bool | None = None
    if n >= slow + 1:
        def gap(k: int) -> float | None:
            """k 为绝对下标。区间不足 slow 根时返回 None。"""
            if k - slow + 1 < 0:
                return None
            f = sum(closes[k - fast + 1: k + 1]) / fast
            s = sum(closes[k - slow + 1: k + 1]) / slow
            return f - s

        g, gp = gap(n - 1), gap(n - 2)
        if g is not None and gp is not None:
            if op == "cross_below":
                crossed = g < 0 and gp >= 0
            else:
                crossed = g > 0 and gp <= 0

    if crossed is None:
        return None, op, None
    return crossed, op, None            # 交由下方 ma_relation 分支按 op 展示


def _p_vol_ratio(ctx, p):
    return ctx.vol_ratio(int(p.get("window", 5))), p.get("op", "<="), p.get("value")


def _p_pct_change_n(ctx, p):
    v = ctx.pct_change_n(int(p.get("n", 5)))
    return v, p.get("op", ">="), p.get("value")


def _p_pct_change_today(ctx, p):
    return ctx.pct_chg_today, p.get("op", ">="), p.get("value")


def _p_turnover(ctx, p):
    return ctx.turnover, p.get("op", ">="), p.get("value")


def _p_market_state(ctx, p):
    return ctx.market_state, "in", p.get("in", [])


def _p_market_score(ctx, p):
    return ctx.market_score, p.get("op", ">="), p.get("value")


def _p_chan_signal(ctx, p):
    """是否存在指定类型/级别的缠论信号。

    ★ level 语义：附录 D 的level 既是级别（30m）也可能是强度（strong）。
      实测 M8 结论表明强弱档差异巨大（86% vs 20%），所以 level 缺省时
      **不按强度过滤**，只比 kind，避免悄悄把 weak 混进来。
    """
    types = p.get("type") or p.get("types") or []
    if isinstance(types, str):
        types = [types]
    level = p.get("level")
    hits = [s for s in ctx.chan_signals
            if (not types or s.get("kind") in types)
            and (not level or s.get("level") == level)]
    actual = [f"{s.get('kind')}/{s.get('level')}" for s in hits]
    return (len(hits) > 0), "==", actual


def _p_chan_type(ctx, p):
    return ctx.chan_stage, "in", p.get("type", [])


def _p_chan_zhongshu_pos(ctx, p):
    return ctx.chan_zhongshu_pos, "in", [p.get("pos", "above")]


def _p_beichi(ctx, p):
    want = p.get("type")
    wants = [want] if isinstance(want, str) else (want or [])
    hits = [b for b in ctx.beichi_types if not wants or b in wants]
    return (len(hits) > 0), "==", hits


def _p_news_direction(ctx, p):
    """近 N 日是否有指定方向的强消息。

    ★ strength 也可能带 op（附录 D.4示例是 `{op: ">=", value: 3}`），
      也可能直接给数字，都支持。
    """
    direction = p.get("direction", "bull")
    if ctx.news_recent_direction and ctx.news_direction != direction:
        return False, "==", ctx.news_recent_direction
    st = p.get("strength")
    if isinstance(st, dict):
        thr, op = st.get("value"), st.get("op", ">=")
    elif st is None:
        thr, op = None, ">="
    else:
        thr, op = st, ">="
    within = p.get("within_days")
    if within is not None and ctx.news_within_days is not None:
        if ctx.news_within_days > within:
            return False, "==", f"{ctx.news_within_days}天前"
    if ctx.news_direction == direction and ctx.news_strength is not None:
        if thr is not None:
            r = _cmp(ctx.news_strength, op, thr)
            if r is None:
                return None, op, thr
            return r, op, thr
        return True, op, None
    return None, op, thr      # 无消息数据 → 缺失 → False


def _p_limit_up(ctx, p):
    """近 N 日内是否有涨停。返回的已是布尔结论（见 BOOL_PREDICATES 说明）。"""
    days = int(p.get("days", 1))
    if ctx.limit_up_days is None:
        return None, "_bool", days
    return (ctx.limit_up_days >= days), "_bool", days


def _p_is_st(ctx, p):
    return ctx.is_st, "==", True


def _p_is_new(ctx, p):
    return ctx.is_new, "==", True


def _p_is_suspend(ctx, p):
    return ctx.is_suspend, "==", True


def _p_float_mv(ctx, p):
    return ctx.float_mv, p.get("op", ">="), p.get("value")


def _p_total_mv(ctx, p):
    return ctx.total_mv, p.get("op", ">="), p.get("value")


def _p_rps(ctx, p):
    return ctx.rps, p.get("op", ">="), p.get("value")


def _p_loss_area_ratio(ctx, p):
    """亏钱效应面积（市场级因子，来自 `breadth.loss_area_ratio()`）。

    ★ 存的是**比例**（0~1），不是百分数 —— 所以阈值写 `0.30`。
      写成 `30` 会永不触发（0.30 > 30 永远 False），这是哑弹的另一种形态。
    ★ 缺失（None）⇒ 返回 (None, op, value)，由框架判 False + warning，
      符合铁律一「宁可漏触发，不能乱触发」。
    """
    return ctx.loss_area_ratio, p.get("op", ">"), p.get("value")


PREDICATES: dict[str, Callable] = {
    "board_strength_rank": _p_board_strength_rank,
    "board_strength_value": _p_board_strength_value,
    "board_rank_change": _p_board_rank_change,
    "price_vs_ma": _p_price_vs_ma,
    "ma_relation": _p_ma_relation,
    "vol_ratio": _p_vol_ratio,
    "pct_change_n": _p_pct_change_n,
    "pct_change_today": _p_pct_change_today,
    "turnover": _p_turnover,
    "market_state": _p_market_state,
    "market_score": _p_market_score,
    "chan_signal": _p_chan_signal,
    "chan_type": _p_chan_type,
    "chan_zhongshu_pos": _p_chan_zhongshu_pos,
    "beichi": _p_beichi,
    "news_direction": _p_news_direction,
    "limit_up": _p_limit_up,
    "is_st": _p_is_st,
    "is_new": _p_is_new,
    "is_suspend": _p_is_suspend,
    "float_mv": _p_float_mv,
    "total_mv": _p_total_mv,
    "rps": _p_rps,
    "loss_area_ratio": _p_loss_area_ratio,
}


#================================================================== custom 逃生舱
SAFE_FUNCS: dict[str, Any] = {
    "abs": abs, "min": min, "max": max, "round": round,
    "len": len, "sum": sum, "float": float, "int": int, "bool": bool,
}
# ★ 这些名字即便写成字符串下标也危险（`x['__builtins__']` 等）
DANGEROUS_KEYS = {
    "__builtins__", "__class__", "__globals__", "__subclasses__",
    "__mro__", "__code__", "__closure__", "__globals__", "__dict__",
    "__reduce__", "__reduce_ex__", "func_globals", "gi_frame",
}
SAFE_NODES = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare,
    ast.Call, ast.Name, ast.Load, ast.Constant, ast.Attribute,
    ast.IfExp, ast.List, ast.Tuple, ast.Dict, ast.Subscript,
    ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn,
)


def safe_eval(expr: str, names: dict[str, Any]) -> Any:
    """AST 白名单求值（★ 不使用 eval）。

    ★★ 必须把**运行期**异常也归一成 ValueError：
      `x['__builtins__']` 在 AST 阶段是合法的 Subscript 节点，
      但求值时抛的是 `KeyError: '__builtins__'`。
      若不归一，调用方的 `except ValueError` 抓不到——
      危险表达式会**通过校验**（validator 报「合法」），
      真跑到规则引擎里才崩，这是典型的「校验通过但运行失败」。
    """
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, SAFE_NODES):
            raise ValueError(f"表达式含禁用节点: {type(node).__name__}")
        if isinstance(node, ast.Attribute):
            raise ValueError("表达式禁止属性访问（防 __class__ / __globals__ 逃逸）")
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise ValueError("只允许调用白名单函数")
        if isinstance(node, ast.Name) and node.id not in names \
                and node.id not in SAFE_FUNCS:
            raise ValueError(f"未知变量: {node.id}")
        # ★ 字符串下标是危险面：`x['__builtins__']` 运行时能摸到解释器内部
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in DANGEROUS_KEYS:
            raise ValueError(f"表达式禁止引用敏感名: {node.value}")
    try:
        return eval(compile(tree, "<dsl>", "eval"),                # noqa: S307
                    {"__builtins__": {}}, {**SAFE_FUNCS, **names})
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"表达式求值失败: {type(exc).__name__}: {exc}") from exc


# ================================================================== 求值核心
def eval_predicate(name: str, params: dict, ctx: StrategyContext,
                   warnings: list[str]) -> PredResult:
    """求值单个谓词。"""
    if name == "custom":
        try:
            env = {k: v for k, v in vars(ctx).items()
                   if not k.startswith("_") and isinstance(
                       v, (int, float, str, bool, list, tuple))}
            env["ma"] = ctx.ma
            env["vol_ratio"] = ctx.vol_ratio
            env["pct_change_n"] = ctx.pct_change_n
            env["price_vs_ma"] = ctx.price_vs_ma
            val = safe_eval(params.get("expr", "False"), env)
            return PredResult(name="custom", value=bool(val), actual=val,
                              threshold=params.get("expr", ""),
                              op_text="expr")
        except Exception as exc:
            warnings.append(f"custom 表达式错误: {type(exc).__name__}: {exc}")
            return PredResult(name="custom", value=False, missing=True,
                              error=str(exc)[:80], threshold=params.get("expr", ""))

    fn = PREDICATES.get(name)
    if fn is None:
        msg = f"未知谓词 '{name}'"
        warnings.append(msg)
        return PredResult(name=name, value=False, missing=True, error=msg)

    # ★★ YAML 简写兼容：`- is_st: true` 解析成 dict，但 `- is_st`（裸键）
    #   与 `- limit_up` （裸键或 null）会得到 None / True。
    #   布尔型谓词（is_st/is_new/is_suspend/limit_up）允许裸写法，
    #   其余谓词传非 dict 直接判错，否则会得到
    #   「not enough values to unpack」这种看不出意图的异常。
    if params is None or isinstance(params, (bool, int, float)):
        params = {} if params is None or params is True else {"value": params}
    if not isinstance(params, dict):
        msg = f"{name} 的参数必须是 dict，收到 {type(params).__name__}"
        warnings.append(msg)
        return PredResult(name=name, value=False, missing=True, error=msg)

    try:
        actual, op, thr = fn(ctx, params or {})
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        warnings.append(f"{name} 求值失败: {msg}")
        return PredResult(name=name, value=False, missing=True, error=msg[:80])

    # ★★ 缺失 → False + warning（附录 D.3 第 1 条）
    if actual is None:
        warnings.append(f"{name}: 数据缺失（actual=None）→ 判 False")
        return PredResult(name=name, value=False, missing=True,
                          threshold=thr, op_text=OP_TEXT.get(op, op))

    if op == "_bool":
        #谓词已自行给出是/否结论，直接采纳，不再比较
        return PredResult(name=name, value=bool(actual), actual=actual,
                          threshold=thr, op_text="满足")

    if name == "ma_relation":
        # 该谓词直接把穿越布尔量放在actual 上
        return PredResult(name=name, value=bool(actual), actual=actual,
                          threshold=thr or op, op_text=OP_TEXT.get(op, op))

    if name in ("chan_signal", "beichi"):
        return PredResult(name=name, value=bool(actual), actual=actual,
                          threshold=thr, op_text="存在")

    if op == "between":
        # ★ threshold 必须是长度为 2 的序列，否则解包会抛
        #   「not enough values to unpack」—— 这类异常看不出是配置写错。
        if not isinstance(thr, (list, tuple)) or len(thr) != 2:
            msg = f"{name}: between 的 threshold 需为 [lo, hi]，收到 {thr!r}"
            warnings.append(msg)
            return PredResult(name=name, value=False, missing=True,
                              threshold=thr, op_text=OP_TEXT[op],
                              error=msg[:80])
        lo, hi = thr
        n = _num(actual)
        if n is None:
            warnings.append(f"{name}: 数值不可比较 → 判 False")
            return PredResult(name=name, value=False, missing=True,
                              threshold=thr, op_text=OP_TEXT[op])
        return PredResult(name=name, value=lo <= n <= hi, actual=n,
                          threshold=f"[{lo}, {hi}]", op_text=OP_TEXT[op])

    r = _cmp(actual, op, thr)
    if r is None:
        msg = f"{name}: 无法比较 actual={actual!r} op={op} thr={thr!r}"
        warnings.append(msg + " → 判 False")
        return PredResult(name=name, value=False, missing=True,
                          threshold=thr, op_text=OP_TEXT.get(op, op))
    return PredResult(name=name, value=bool(r), actual=actual, threshold=thr,
                      op_text=OP_TEXT.get(op, op))


def _eval_group(clause: dict, ctx: StrategyContext,
                warnings: list[str], out: list[PredResult],
                mode: str) -> bool:
    """求值一个子句（all_of / any_of / none_of）。

    mode 语义（空列表时的返回值见 `evaluate` 的说明）：
      all_of  → 全部为真才True；空 → True（无约束）
      any_of  → 任一为真即 True；空 → False（无候选可供命中）
      none_of → 全部为假才 True；空 → True（无排除项）

    ★ 不做短路：三个子句的谓词都会被求值并记录，
      否则 UI 上会出现「为什么只看了一个条件」而无法排查。
    """
    items = clause.get(mode) or []
    # ★ 容错：YAML 里 `- is_st`（裸键）会解析成字符串 "is_st"，
    #   `- {limit_up: {days: 1}}` 才是 dict。两种都要能处理，
    #   否则报出的错是「not enough values to unpack」，
    #   完全看不出是写法问题（实测踩过）。
    results = []
    for item in items:
        if isinstance(item, str):
            results.append(eval_predicate(item, {}, ctx, warnings))
        elif isinstance(item, dict) and len(item) == 1:
            k, v = next(iter(item.items()))
            results.append(eval_predicate(k, v, ctx, warnings))
        else:
            warnings.append(f"{mode} 里的项格式非法: {item!r}")
            results.append(PredResult(name="?", value=False, missing=True,
                                      error=f"项格式非法: {str(item)[:40]}"))
    out.extend(results)
    vals = [r.value for r in results]
    if mode == "all_of":
        return all(vals) if vals else True
    if mode == "any_of":
        return any(vals) if vals else False
    if mode == "none_of":
        return not any(vals) if vals else True
    raise ValueError(f"未知子句类型: {mode}")


def evaluate(rule: dict, ctx: StrategyContext) -> EvalResult:
    """求值整条规则（附录 D.1 结构）。

    rule = {"rule_id":..., "name":..., "if": {all_of/any_of/none_of}, "then": {...}}

    ★★★ 关键的空子句语义（实测踩过，错了会让**所有规则永不命中**）：
      附录 D.1 明确写着 `any_of` / `none_of` 是**可省略**的。
      若把「省略的 any_of」当成「空的 any_of」并求值成 False，
      再与 all_of 取 AND ⇒ 任何没写 any_of 的规则**永远不命中**，
      而且不报错、 preds 全是 True，日志上看「条件都过了却不触发」，
      极难定位。

      正确语义：
        all_of  缺省 → True（无硬性约束）
        none_of 缺省 → True（无排除项）
        any_of  缺省 → True（★ 不是 False，见上）
        显式写空的 any_of: [] → False（「要求至少命中一个候选」但没给候选）
    """
    rid = rule.get("rule_id", "?")
    warnings: list[str] = []
    preds: list[PredResult] = []
    clause = rule.get("if") or {}

    try:
        a = _eval_group(clause, ctx, warnings, preds, "all_of")
        n = _eval_group(clause, ctx, warnings, preds, "none_of")
        # ★ any_of 只在**显式写了非空候选**时参与判定；
        #   缺省或写空列表都视为「无附加要求」。
        has_any = bool(clause.get("any_of"))
        b = _eval_group(clause, ctx, warnings, preds, "any_of") \
            if has_any else True
        matched = bool(a and b and n)
    except Exception as exc:
        return EvalResult(rule_id=rid, matched=False, preds=preds,
                          warnings=warnings,
                          error=f"{type(exc).__name__}: {exc}")

    return EvalResult(rule_id=rid, matched=matched, preds=preds,
                      warnings=warnings)


# ================================================================== 校验器
_KNOWN_KEYS = {"rule_id", "name", "timeframe", "if", "then", "note",
               "source", "evidence"}


def validate_rule(rule: dict) -> list[str]:
    """规则 schema 校验，返回问题列表（空 = 合法）。

    ★ 不抛异常而是返回列表：蒸馏流程要能批量校验一批规则并报告所有问题，
      一遇错就中断会丢掉其余诊断信息。
    """
    errs: list[str] = []
    if not isinstance(rule, dict):
        return [f"规则必须是 dict，收到 {type(rule).__name__}"]
    if not rule.get("rule_id"):
        errs.append("缺少 rule_id")
    for k in rule:
        if k not in _KNOWN_KEYS:
            errs.append(f"未知顶层字段 '{k}'")
    clause = rule.get("if")
    if not isinstance(clause, dict):
        errs.append("缺少 if 子句或类型错误")
        return errs
    if not any(m in clause for m in ("all_of", "any_of", "none_of")):
        errs.append("if 子句里没有 all_of / any_of / none_of")
    for mode in ("all_of", "any_of", "none_of"):
        for item in (clause.get(mode) or []):
            # 裸键简写（`- is_st`）也是合法写法
            if isinstance(item, str):
                if item != "custom" and item not in PREDICATES:
                    errs.append(f"未知谓词 '{item}'")
                continue
            if not isinstance(item, dict) or len(item) != 1:
                errs.append(f"{mode} 里的谓词必须是单键 dict，收到 {item!r}")
                continue
            name = next(iter(item))
            if name != "custom" and name not in PREDICATES:
                errs.append(f"未知谓词 '{name}'")
            elif name == "custom":
                expr = (item[name] or {}).get("expr", "")
                try:
                    ast.parse(expr, mode="eval")
                except SyntaxError as exc:
                    errs.append(f"custom 表达式语法错误: {exc.msg}")
                else:
                    try:
                        safe_eval(expr, {"x": 1.0})
                    except Exception as exc:
                        errs.append(f"custom 表达式不安全/不可求值: {exc}")
    then = rule.get("then")
    if then is not None:
        if not isinstance(then, dict):
            errs.append("then 必须是 dict")
        else:
            act = then.get("action")
            if act is not None and act not in ("BUY", "SELL", "HOLD"):
                errs.append(f"then.action 只能是 BUY/SELL/HOLD，收到 {act!r}")
            pos = then.get("position")
            if pos is not None and not (0 <= float(pos) <= 1):
                errs.append(f"then.position 应在 0~1，收到 {pos}")
    return errs


if __name__ == "__main__":                          # pragma: no cover
    import json

    ctx = StrategyContext(
        code="600519", as_of=datetime(2026, 10, 8, 15, 0, tzinfo=CST),
        closes=[1400 + (i % 7) * 10 for i in range(120)],
        volumes=[1e6 * (1 + i % 3) for i in range(120)],
        chan_signals=[{"kind": "3buy", "level": "strong", "date": "2026-10-08",
                       "reason": "回抽不破中枢上沿"}],
        chan_zhongshu_pos="above", market_state="修复",
        float_mv=1.8e11, rps=88.0, is_st=False, is_suspend=False,
        news_direction="bull", news_strength=4.0, news_within_days=1,
        limit_up_days=0)

    demo = {
        "rule_id": "demo-001",
        "name": "强势板块缩量回踩不破5日线",
        "timeframe": "1d",
        "if": {
            "all_of": [
                {"float_mv": {"op": ">=", "value": 2e10}},
                {"rps": {"op": ">=", "value": 80}},
                {"price_vs_ma": {"ma": 5, "op": ">=", "value": -0.01}},
                {"vol_ratio": {"window": 5, "op": "<=", "value": 3.0}},
                {"market_state": {"in": ["启动", "发酵", "修复"]}},
                {"chan_zhongshu_pos": {"pos": "above"}},
                {"chan_signal": {"type": ["3buy"], "level": "strong"}},
            ],
            "none_of": [
                {"is_st": {}},
                {"limit_up": {"days": 1}},
                # ★ 故意用缺失因子验证「缺失 → False」：
                #   该 Context 没有 bear 消息（news_direction=bull），
                #   判 False 并记 warning，而不是当成「没有利空」放行。
                {"news_direction": {"direction": "bear", "within_days": 1}},
            ],
        },
        "then": {"action": "BUY", "position": 0.3,
                 "stop_loss": "prev_bi_start", "take_profit": [0.08, 0.15],
                 "max_hold_days": 10},
    }

    print("=" * 74)
    print("附录 D · 条件 DSL 求值演示")
    print("=" * 74)
    errs = validate_rule(demo)
    print(f"schema 校验: {'通过' if not errs else errs}")
    res = evaluate(demo, ctx)
    print(res.explain())
    print()
    print(f"命中: {res.matched}")
    print()
    print("JSON 形态:")
    print(json.dumps(res.to_json(), ensure_ascii=False, indent=2)[:700])