# -*- coding: utf-8 -*-
"""★ 板块八因子 + 回填 + 采集脚本 的守护测试（#43）

★★★ 这组测试防的是「数字看着正常、口径是假的」
--------------------------------------------------
1. **非交易日被写成交易日** —— 2026-10-10（周六）跑 `sync_board_strength.py`，
   实时接口返回的是**上一交易日收盘**的静态数据，代码却照样
   `now.replace(hour=15)` 写成 10-10 15:00⇒ 库里凭空多一个交易日，
   而 `kline_daily` 那天是 **0 只**（周六）。实测已发生并已修数据。
2. **`r.pop(k, 0), r.pop(k, 0)`** —— 同一个 key pop 两次，第二个恒为默认 0。
   落库文案变成「每板块只能取到首页 0 只」，自相矛盾却不报错。
3. **反爬 401 被当成「页面结构变化」** —— 实测同花顺 IP 限流返回
   `401滑动验证码demo`，脚本却继续抓下一个板块，50 个里 33 个「失败」，
   且每个白等 12~25 秒，把临时限流坐实成封禁。
4. **`DELETE WHERE snap_date=?` 后再插** —— 限流中断时会把上次完整采到的
   板块一起抹掉，**快照随重试越来越空**。

运行： pytest tests/test_board_factors.py -v
"""
from __future__ import annotations

import ast
import json
import sqlite3
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domain import board_factors as BF      # noqa: E402

CST = timezone(timedelta(hours=8))
D = datetime(2026, 10, 9, 15, 0, tzinfo=CST)


def _q(code, prev=None, close=None, pct=None, amt=None, ret5=None, st=0):
    return {"code": code, "pre_close": prev, "close": close,
            "pct_chg": pct, "amount": amt, "ret5": ret5, "is_st": st}


# ═══════════════════════════════════════════════ 纯函数口径
class TestFactorPurity:
    """铁律二：领域层禁读墙钟。"""

    def test_no_wallclock(self):
        """★ 必须用 AST 看代码节点，不能用字符串匹配
        —— 源码注释里提到 datetime.now() 会被自己的守护拦下。"""
        tree = ast.parse(BF.__file__ and
                         Path(BF.__file__).read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr in ("now", "today"):
                    src = ast.unparse(f)
                    if "datetime" in src or "time" in src:
                        bad.append(src)
                # f-string 形式 datetime.now() 会被 JoinedStr 包住
                if isinstance(f, ast.Call) and isinstance(f.func, ast.Name):
                    if f.func.id in ("time",):
                        bad.append(f.func.id)
        assert not bad, f"领域层出现墙钟调用: {bad}"


class TestLimitRatio:
    """d3：涨停判定必须走 breadth 严格口径，且 None 要排除而非当 False。"""

    def test_exact_limit_up_counted(self):
        # 600000 沪主板：涨停价 = round(10.00*1.1, 2) = 11.00
        qs = [_q("600000", 10.00, 11.00, 10.0),
              _q("600001", 10.00, 10.50, 5.0),
              _q("600002", 10.00, 9.50, -5.0)]
        r, hit, base = BF.limit_ratio(qs)
        assert base == 3 and hit == 1
        assert r == pytest.approx(1 / 3)

    def test_near_but_not_limit_excluded(self):
        """实测过的坑：收 46.59 vs 涨停价 46.65，差 0.06 并未封板。
        宽松口径 pct>=9.8 会多判。"""
        prev = 42.35
        limit = round(prev * 1.1 + 1e-9, 2)     # 46.59/ 46.65 量级
        close = round(limit - 0.06, 2)
        _, hit, _ = BF.limit_ratio([_q("603823", prev, close, 10.0)])
        assert hit == 0, "未封板却被判涨停（宽松口径回归）"

    def test_missing_pre_close_excluded_from_denominator(self):
        """★ 缺失一律**不进分母**（否则 d3 被系统性压低，且不报任何错）。

        这条测的是**真会发生**的路径：成分股快照里 `pre_close` 为空是常态。
        （早先另有一条测`is_limit_up` 返回 None 的用例，但那条分支
        在本函数里**不可达** —— None 的唯一成因是 pre_close<=0，
        已被上游 `prev <= 0` 拦掉 ⇒ 变异掉它测试仍全绿 = 假阳性，已删除。）
        """
        qs = [_q("600000", None, 11.00, None),        # 前收缺失
              _q("600002", 10.00, 10.50, 5.0),# 未涨停，有效样本
              _q("600003", 10.00, 11.00, 10.0)]       # 涨停，有效样本
        r, hit, base = BF.limit_ratio(qs)
        assert base == 2, "缺 pre_close 的票被计入了分母"
        assert hit == 1 and r == pytest.approx(0.5)

    def test_missing_close_excluded(self):
        qs = [_q("600000", 10.00, None, None), _q("600002", 10.00, 11.00, 10.0)]
        r, hit, base = BF.limit_ratio(qs)
        assert base == 1 and hit == 1 and r == pytest.approx(1.0)

    def test_zero_pre_close_excluded(self):
        """pre_close == 0 也必须排除（除零会产生荒谬涨停价）。"""
        qs = [_q("600000", 0.0, 11.00, 10.0), _q("600002", 10.00, 10.5, 5.0)]
        _, hit, base = BF.limit_ratio(qs)
        assert base == 1 and hit == 0

    def test_all_missing_returns_none(self):
        r, hit, base = BF.limit_ratio([_q("600000", None, None, None)])
        assert r is None and hit == 0 and base == 0

    def test_empty_returns_none(self):
        assert BF.limit_ratio([]) == (None, 0, 0)

    def test_st_limit_up_included(self):
        """★ ST 涨停**包含**在本项目口径内（`is_limit_up` 实测 is_st=True 判 True）。

        早先我写成「ST 涨停必须排除」并断言 hit == 0 ⇒ 测试红了。
        核对 `breadth.is_limit_up` 的真实行为后确认：**是断言写反了**，
        代码是对的。（口径本身：ST 涨停**排除**、ST 跌停**包含**，
        这里测的是涨停被排除的场景之外 ⇒ ST 按 5% 封板应计入。）
        """
        prev = 10.00
        st_limit = round(prev * 1.05, 2)          # 10.50
        _, hit, base = BF.limit_ratio(
            [_q("600001", prev, st_limit, 5.0, st=1)])
        assert base == 1
        assert hit == 1, "ST 股封住 5% 涨停价却未计入（口径：ST 涨停包含）"

    def test_non_st_using_st_limit_price_excluded(self):
        """对照：非 ST 股涨到 5%（远不到 10% 涨停）不算涨停。"""
        prev = 10.00
        _, hit, base = BF.limit_ratio(
            [_q("600001", prev, round(prev * 1.05, 2), 5.0, st=0)])
        assert base == 1 and hit == 0

    def test_st_down_limit_not_counted_as_up(self):
        """ST 跌停**包含**在跌停口径，但绝不能被算成涨停。"""
        prev = 10.00
        st_down = round(prev * 0.95, 2)           # 9.50
        _, hit, base = BF.limit_ratio(
            [_q("600001", prev, st_down, -5.0, st=1)])
        assert base == 1 and hit == 0


class TestUpRatio:
    def test_missing_excluded(self):
        qs = [_q("a", None, 11.0), _q("b", 10.0, 10.5), _q("c", 10.0, 9.0)]
        r, base = BF.up_ratio(qs)
        assert base == 2 and r == pytest.approx(0.5)

    def test_all_none(self):
        assert BF.up_ratio([_q("a", None, None)]) == (None, 0)


class TestVolExpansion:
    """d6：名字必须与算法一致 = 均值 / 中位数。"""

    def test_equal_amounts_is_one(self):
        qs = [_q(str(i), amt=100.0) for i in range(5)]
        assert BF.vol_expansion(qs) == pytest.approx(1.0)

    def test_known_ratio(self):
        # [1,1,1,1,10] → mean=2.8, median=1 ⇒ 2.8
        qs = [_q(str(i), amt=v) for i, v in enumerate([1, 1, 1, 1, 10])]
        assert BF.vol_expansion(qs) == pytest.approx(2.8)

    def test_zero_median_returns_none(self):
        assert BF.vol_expansion([_q("a", amt=0.0), _q("b", amt=0.0)]) is None

    def test_missing_all_returns_none(self):
        assert BF.vol_expansion([_q("a"), _q("b")]) is None

    def test_partial_missing_uses_present_only(self):
        """缺失不当 0 —— 否则分母被稀释、倍数被系统性拉高。"""
        qs = [_q("a", amt=10.0), _q("b", amt=10.0), _q("c", amt=None)]
        assert BF.vol_expansion(qs) == pytest.approx(1.0)


class TestRs5Excess:
    """d2：成分股 5 日 RS 在全市场中的**分位**（0~1），取成分中位数。

    ★★★ 口径变更（2026-10-10）：原来是 `q.ret5 - market_rs[q.code]`，
      而 `market_ret5()` 返回的正是**同一只股票同一窗口**的收益
      ⇒ 差值恒为 0.0，**17 个板块 d2 全部等于 0.0**。
      0.0 是合法数值不是缺失值 ⇒ `data_gate` 拦不住，
      甚至让 coverage 显得更高（缺因子会拉低 coverage）。
    """

    def _mkt(self, n=100):
        """全市场收益：0.00 ~ 0.99，均匀分布。"""
        return {f"s{i}": i / 100.0 for i in range(n)}

    def test_not_dead_formula(self):
        """★★ 防死公式：d2 不得恒为 0.0。

        ★ 这条判据自己曾经是**假阳性**：原来只写 `assert got != 0.0`，
          而死公式的输出恰好就是 0.0 ⇒ 该断**应该**能抓到，
          但实测变异后真正变红的是另外 5 条（分位类断言），
          这条因为市场样本太小（n=3）导致分位也可能算出非 0 而侥幸通过。
          ⇒ 现在把市场样本放大，并直接断言**具体分位值**，
          让「死公式」在任何样本量下都过不去。
        """
        mkt = {"s{i}".format(i=i): i / 100.0 for i in range(100)}
        # 成分股收益**故意等于 market 里同名 code 的值**——
        # 死公式 `ret5 - market_rs[code]` 此刻必然为 0
        mkt["a"] = 0.80
        mkt["b"] = 0.95
        qs = [_q("a", ret5=0.80), _q("b", ret5=0.95)]
        got = BF.rs5_excess(qs, mkt)
        assert got is not None
        #分位中位应落在 [0.80, 0.95] 区间，绝不是 0
        assert got > 0.75, f"疑似退回死公式（输出 {got}）"

    def test_all_same_as_market_median_is_midrank(self):
        """成分股都在市场中位附近 ⇒ 分位约0.5。"""
        mkt = self._mkt()
        qs = [_q("a", ret5=0.50), _q("b", ret5=0.51)]
        got = BF.rs5_excess(qs, mkt)
        assert 0.45 <= got <= 0.56, got

    def test_strong_members_high_percentile(self):
        """强势成分 ⇒ 分位明显 > 0.5。"""
        mkt = self._mkt()
        qs = [_q("a", ret5=0.95), _q("b", ret5=0.97)]
        assert BF.rs5_excess(qs, mkt) > 0.9

    def test_weak_members_low_percentile(self):
        mkt = self._mkt()
        qs = [_q("a", ret5=0.01), _q("b", ret5=0.03)]
        assert BF.rs5_excess(qs, mkt) < 0.1

    def test_median_not_mean(self):
        """★ 取中位数：一个极端值不改变结论。"""
        mkt = self._mkt()
        qs = [_q(str(i), ret5=0.50 + i * 0.001) for i in range(5)]
        qs.append(_q("x", ret5=0.999))          # 极端值
        got = BF.rs5_excess(qs, mkt)
        assert 0.45 <= got <= 0.56, f"被极端值拉走: {got}"

    def test_empty_returns_none(self):
        assert BF.rs5_excess([], {"a": 0.1}) is None

    def test_empty_market_returns_none(self):
        """全市场为空 ⇒ 无法定分位，必须 None 而不是 0。"""
        assert BF.rs5_excess([_q("a", ret5=0.2)], {}) is None

    def test_missing_ret5_excluded(self):
        mkt = self._mkt()
        qs = [_q("a", ret5=None), _q("b", ret5=0.95)]
        assert BF.rs5_excess(qs, mkt) > 0.9


class TestRankSlope:
    """d8：少于 3 点必须 None。"""

    def test_two_points_returns_none(self):
        h = [(datetime(2026, 10, 7, tzinfo=CST), 10),
             (datetime(2026, 10, 8, tzinfo=CST), 3)]
        assert BF.rank_slope(h) is None

    def test_single_point_returns_none(self):
        assert BF.rank_slope([(D, 5)]) is None

    def test_improving_rank_negative_slope(self):
        """rank 变小 = 变强 ⇒ 负斜率。"""
        h = [(datetime(2026, 10, 7, tzinfo=CST), 30),
             (datetime(2026, 10, 8, tzinfo=CST), 20),
             (D, 10)]
        s = BF.rank_slope(h)
        assert s is not None and s < 0

    def test_flat_rank_zero(self):
        h = [(datetime(2026, 10, 7, tzinfo=CST), 10),
             (datetime(2026, 10, 8, tzinfo=CST), 10), (D, 10)]
        assert BF.rank_slope(h) == pytest.approx(0.0)

    def test_none_rank_filtered(self):
        h = [(datetime(2026, 10, 7, tzinfo=CST), None),
             (datetime(2026, 10, 8, tzinfo=CST), 20),
             (D, 10)]
        assert BF.rank_slope(h) is None


# ═══════════════════════════════════════════════ 组装与标注
class TestAnnotate:
    def test_structure_matches_score_boards(self):
        """★ 因子必须在**顶层**，`_meta` 是子对象。
        早先返回 {"factors": {...}} ⇒ 两套结构并存，读取方读空。"""
        out = BF.annotate({"d1_pct": 1.0}, 20, 20)
        assert out["d1_pct"] == 1.0
        assert "factors" not in out
        assert out["_meta"]["sample_n"] == 20

    def test_subset_note_present(self):
        """20 分子集口径必须写进落库内容，否则会被后人当全量。"""
        out = BF.annotate({}, 20, 20)
        note = out["_meta"]["sample_note"]
        assert "20" in note and "不是全量" in note

    def test_missing_reason_lists_all_unavailable(self):
        """★★ 落库的「缺失原因」必须**跟着 `FACTOR_UNAVAILABLE` 走**。

        ★ 这条断言原先写死 `{"d4_max_streak", "d5_inflow"}`，
          d4 实现后（2026-10-10）立刻红。
          —— 看着像「测试在守护代码」，其实是**测试在守护旧状态**：
          若照着红去把 d4 改回留空，就把已经算出来的因子又扔了。
          ⇒ 正确写法是「清单里的每一项都必须出现在 missing_reason 里」，
            以后再增删因子测试自动跟随，且能抓住
            「加了因子却忘了写理由」这种半吊子改动。
        """
        out = BF.annotate({}, 20, 20)
        mr = out["_meta"]["missing_reason"]
        assert BF.FACTOR_UNAVAILABLE, "FACTOR_UNAVAILABLE 不应为空"
        for k in BF.FACTOR_UNAVAILABLE:
            assert k in mr, f"{k} 缺落库缺失原因"

    def test_unavailable_factors_never_faked(self):
        """★ 不可算因子必须在常量清单里，且理由要具体。

        ★★ 这里**不能**断言清单的具体内容 —— 那等于把「当前还差什么」
          焊死在测试里：改进一个因子就得改测试，而人会选择改回去。
          只守护不变式：**每一项都有具体理由、命名规范**。
        """
        for k, v in BF.FACTOR_UNAVAILABLE.items():
            assert v and len(v) > 10, f"{k} 缺具体理由"
            assert k.startswith("d") and k[1].isdigit(), f"因子名不规范: {k}"


# ═══════════════════════════════════════════════ 脚本守护
def _in_str(line: str, ch: str) -> bool:
    """该行里`ch` 出现在字符串字面量内部（简易引号配平）。"""
    return line.count('"') % 2 == 1 or line.count("'") % 2 == 1


def _src(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _find_calls(tree: ast.AST, func_name: str) -> list[ast.Call]:
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == func_name):
            out.append(n)
    return out


class TestSyncBoardStrengthTradeDay:
    """★ 非交易日不得被写成交易日（实测已造成 50 行脏数据）。"""

    def _ast(self):
        return ast.parse(_src("sync_board_strength.py"))

    def test_guards_non_trade_day(self):
        tree = self._ast()
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "is_trade_day" in names, "缺少 is_trade_day 守卫"
        assert "prev_trade_day" in names, "缺少非交易日的归一处理"

    def test_hor_normalization_after_guard(self):
        """★ 归一必须发生在守卫**之后**。
        顺序反了 ⇒ 又会造出假交易日。"""
        tree = self._ast()
        src = _src("sync_board_strength.py")
        i_guard = src.index("is_trade_day(")
        i_norm = src.index("ts.replace(hour=15")
        assert i_guard < i_norm, "hour=15 归一出现在交易日守卫之前"

    def test_refuses_when_no_prev_trade_day(self):
        """无法确定最近交易日时必须拒写，而不是兜个日期。"""
        src = _src("sync_board_strength.py")
        assert "拒绝写入" in src


class TestFetchBoardMembersAntiScrape:
    def test_detects_blocked_page(self):
        src = _src("fetch_board_members.py")
        assert "_looks_blocked" in src
        assert "验证码" in src, "未识别 401 滑块验证页"

    def test_blocks_whole_batch_on_401(self):
        """★ 命中限流必须中止整批。
        继续抓只会把限流坐实成封禁（实测 50 个里 33 个失败）。"""
        src = _src("fetch_board_members.py")
        tree = ast.parse(src)
        breaks = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Break)]
        assert breaks, "遇到 blocked 没有 break"
        assert "中止剩余" in src

    def test_no_full_day_delete(self):
        """★★ 不得 `DELETE WHERE snap_date=?` 后再插 ——
        限流中断会把上次采到的板块抹掉，快照只会越来越空。"""
        src = _src("fetch_board_members.py")
        assert "DELETE FROM board_member_snapshot" not in src

    def test_reports_deduped_board_count(self):
        """日志必须报实际达成板块数，不是计划数。"""
        src = _src("fetch_board_members.py")
        assert "board_member_snapshot：全库" in src

    def test_subset_warning_present(self):
        src = _src("fetch_board_members.py")
        assert "不是全量" in src or "子集口径" in src


class TestBackfillNoPhantomFields:
    """★ 禁 double-pop、禁在 pop 后取值。"""

    def test_no_double_pop_same_key(self):
        """★★ `r.pop(k, 0)` 在**同一循环里出现两次**时，
        第二次必然返回默认值 ⇒ 静默归零。

        ★★ 判据曾经写成「同一个 call 里有两个相同实参」——
          那在 Python 里根本写不出来（`a.pop(k,0)` 只吃一个 key），
          所以这条判据**永远不会触发**，是典型的假阳性。
          实测：按它去变异真实代码，测试依然全绿 ⇒ 才发现了这一点。
          正确判据：**按 for 循环统计 pop 的 key 出现次数**。
        """
        from collections import Counter

        tree = ast.parse(_src("backfill_board_factors.py"))
        checked = 0
        for loop in [n for n in ast.walk(tree)
                     if isinstance(n, (ast.For, ast.AsyncFor))]:
            keys = [
                n.args[0].value
                for n in ast.walk(loop)
                if (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "pop" and n.args
                    and isinstance(n.args[0], ast.Constant))
            ]
            dup = {k: v for k, v in Counter(keys).items() if v > 1}
            assert not dup, f"同循环内对同一 key pop 多次: {dup}"
            checked += 1
        assert checked > 0, "未找到任何 for 循环，判据本身失效"

    def test_no_pop_then_get(self):
        """★ pop 之后再 get 同一 key ⇒ 恒为默认值（静默归零）。

        ★★ 判据必须**同时看行号顺序**：
          只统计「同函数内 pop 与 get 同 key」会把
          「先 get 统计、后 pop 消耗」的**正确写法**也判成违规
          （实测源码 211 行 get、225 行 pop 是对的，却红了）。
        ★★ 判据范围必须是**整个函数**，不能只看 for 循环：
          实测「pop 在循环里、get 在后面的 stats 块」这种最常见的写法
          在循环级判据下**完全测不出来**（测试全绿但代码是错的）。
        """
        tree = ast.parse(_src("backfill_board_factors.py"))
        checked = 0
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            pops: dict[str, int] = {}
            gets: dict[str, int] = {}
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.args and isinstance(n.args[0], ast.Constant)):
                    continue
                k = n.args[0].value
                if not isinstance(k, str):
                    continue
                if n.func.attr == "pop":
                    pops.setdefault(k, n.lineno)
                elif n.func.attr == "get":
                    gets.setdefault(k, n.lineno)
            # ★ 只有「pop 在 get 之前」才是 bug；反序（先 get 后 pop）是正确写法
            clash = {k: (pops[k], gets[k]) for k in pops
                     if k in gets and pops[k] < gets[k]}
            assert not clash, (
                f"{fn.name}: 先 pop 再 get 同一 key（后者恒为默认 0）: {clash}")
            checked += 1
        assert checked > 0, "未找到任何函数，判据本身失效"

    def test_sample_n_read_from_meta(self):
        """★ 样本量在 `_meta` 下，不能读顶层（恒None）。"""
        src = _src("backfill_board_factors.py")
        assert '_meta' in src

    def test_uses_data_gate(self):
        """残缺日必须被拦，否则会产出 coverage≈0.33 却报「成功」。"""
        src = _src("backfill_board_factors.py")
        assert "data_gate" in src and "is_usable" in src

    def test_raw_columns_roundtrip(self):
        """★★★ 回填脚本不能把自己的**原料列**写成NULL。

        实测事故（2026-10-10）：脚本 `DELETE` 当日行→重算→插回，
        而写回时用 `r.get("net_inflow")` / `r.get("up_ratio")`，
        `rows_out` 里的键却叫 `d5_inflow` / `d7_up_ratio`
        ⇒ `.get()` 恒None ⇒ `net_inflow` 383 → 0、`coverage` 0.82 → 0.67。
        而下一轮读的原料正是上一轮写坏的行 ⇒ **每重跑一次丢一截**，
        全程零报错、零告警。

        判据：**INSERT 语句里出现的每个 `r.get("x")`，其 x 必须是
        `rows_out.append({...})` 字典里的真实键**；缺一个就红。
        ★ 不能只判「`get` 的键都存在」—— 那样写死 `None` 也算过；
          要同时保证这些键确实来自读出的原料列。
        """
        tree = ast.parse(_src("backfill_board_factors.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "backfill")

        # 1) 本轮从库里读出的原料列（SELECT 列表）
        raw_cols = set()
        for n in ast.walk(fn):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value.startswith("SELECT ")
                    and "board_strength" in n.value):
                for piece in n.value[n.value.index("SELECT") + 6:
                                     n.value.index("FROM")].split(","):
                    raw_cols.add(piece.strip())

        # 2) rows_out.append({...}) 的键
        built = set()
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "append" and n.args
                    and isinstance(n.args[0], ast.Dict)):
                built |= {k.value for k in n.args[0].keys
                          if isinstance(k, ast.Constant)}

        # 3) INSERT 语句里 r.get("x") 的键
        #    ★ 必须**限定在执行 INSERT 的那条语句**上，不能扫整个函数：
        #      函数里还有 json.loads(...).get("percentile")、
        #      BF.annotate 之类无关的 .get，扫全函数会一律误伤
        #      （实测判据报 sample_n/_meta/percentile 全是假阳性）。
        insert_stmt = None
        for n in ast.walk(fn):
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                call = n.value
                if (isinstance(call.func, ast.Attribute)
                        and call.func.attr == "execute"):
                    sql = call.args[0] if call.args else None
                    if (isinstance(sql, ast.Constant)
                            and isinstance(sql.value, str)
                            and sql.value.lstrip().upper().startswith(
                                "INSERT INTO BOARD_STRENGTH")):
                        insert_stmt = n
                        break
        assert insert_stmt is not None, (
            "未找到写入 board_strength 的 INSERT 语句，判据本身失效")
        gets = {n.args[0].value for n in ast.walk(insert_stmt)
                if (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get" and n.args
                    and isinstance(n.args[0], ast.Constant))}

        assert raw_cols, "未解析到 SELECT 原料列，判据本身失效"
        assert built, "未找到 rows_out 字典，判据本身失效"

        orphan = {k for k in gets if k not in built}
        assert not orphan, (
            f"INSERT 写了 rows_out 里不存在的键（恒为 None → 落库 NULL）: {orphan}")

        # ★ 反向：原料列若没被读进来，写回时也无处可取。
        #   注意 `pct_chg` 在 rows_out 里换名成了 `d1_pct`（因子名），
        #   所以「同名透传」只对 net_inflow / up_ratio 这类成立。
        must_keep = {"pct_chg", "net_inflow", "up_ratio",
                     "limit_up_cnt", "limit_down_cnt", "max_streak",
                     "rank_change", "vol_ratio", "leader_code", "amount"}
        assert must_keep <= raw_cols, (
            f"原料列漏读，重跑会丢数据: {must_keep - raw_cols}")
        passthrough = must_keep - {"pct_chg"}
        assert passthrough <= built, (
            f"原料列未透传到 rows_out: {passthrough - built}")

    def test_write_path_uses_readwrite_conn(self):
        """★ 「先读后写」的脚本必须**全程 `get_conn()`**。

        实测：读用 `get_ro_conn()`、写用 `get_conn()`
        ⇒ 切到写连接时抛
        `Can't open a connection to same database file with a
        different configuration than existing connections`。
        """
        # ★ 只看**代码**，不看注释 —— 注释里为了说明原因
        #   必然会出现 "get_ro_conn" 这个词，按子串判会永远红。
        code = chr(10).join(
            ln for ln in _src("backfill_board_factors.py").splitlines()
            if not ln.lstrip().startswith("#"))
        body = chr(10).join(
            ln.split("#")[0] if "#" in ln and not _in_str(ln, "#") else ln
            for ln in code.splitlines())
        assert "get_ro_conn" not in body, (
            "写脚本不得使用只读连接（与读写连接不能共存）")
        assert "duck.get_conn()" in body


class TestMaxStreak:
    """★★ d4 最高连板：本地 K 线**足以**推出，不该再留空。

    ★ 原先 `FACTOR_UNAVAILABLE["d4_max_streak"]` 写的是「无逐股连板状态机」，
      理由其实站不住 —— 逐日涨停标记折叠一下就是状态机。
      这类「把『没写』当成『拿不到』」的留空最贵：
      因子权重 0.10 被白白浪费，且 coverage 少 10 个点看起来仍「正常」。
    """

    TD = ["2026-10-07", "2026-10-08", "2026-10-09"]

    def _q(self, code, hits):
        """hits: {day: True/False/None}"""
        return {"code": code,
                "streaks": BF.limit_up_streaks(list(hits.items()), self.TD)}

    def test_three_in_a_row(self):
        r = BF.limit_up_streaks([("2026-10-07", True), ("2026-10-08", True),
                                 ("2026-10-09", True)], self.TD)
        assert r == {"2026-10-07": 1, "2026-10-08": 2, "2026-10-09": 3}

    def test_suspension_breaks_chain(self):
        """★★ 停牌（缺数据）必须**断链**，不能跨过去累加。

        这是本函数存在的全部理由：按 `bars` 行序累加的话，
        10-07 与 10-09 都封板会被算成 2 连板，而 10-08 根本没交易。
        """
        r = BF.limit_up_streaks([("2026-10-07", True), ("2026-10-09", True)],
                                self.TD)
        assert r["2026-10-09"] == 1, f"停牌后应从 1 重新起算，实得 {r['2026-10-09']}"
        assert r["2026-10-08"] == 0

    def test_explicit_not_limit_up_resets(self):
        """明确「没涨停」与「缺数据」都要归零，但语义不同。"""
        r = BF.limit_up_streaks([("2026-10-07", True), ("2026-10-08", True),
                                 ("2026-10-09", False)], self.TD)
        assert r["2026-10-09"] == 0

    def test_zero_is_valid_not_missing(self):
        """★ 高度 0 是**合法结果**，不能当缺失 —— 否则弱市全部被剔除，
        而 `factor_coverage` 恰好不受影响 ⇒ 又是一个漂亮的假象。"""
        qs = [self._q("A", {"2026-10-07": True, "2026-10-08": False,
                            "2026-10-09": False})]
        h, n = BF.max_streak(qs, "2026-10-09", self.TD)
        assert h == 0 and n == 1

    def test_missing_data_returns_none(self):
        qs = [{"code": "A", "streaks": {}}]
        h, n = BF.max_streak(qs, "2026-10-09", self.TD)
        assert h is None and n == 0

    def test_non_trade_day_refused(self):
        """★ 非交易日不算连板：库里那天的行情其实是上一交易日收盘。"""
        qs = [self._q("A", {d: True for d in self.TD})]
        assert BF.max_streak(qs, "2026-10-10", self.TD) == (None, 0)

    def test_max_across_members(self):
        qs = [self._q("A", {"2026-10-07": True, "2026-10-08": True,
                            "2026-10-09": True}),
              self._q("B", {"2026-10-07": True, "2026-10-08": True,
                            "2026-10-09": True}),
              self._q("C", {"2026-10-07": True, "2026-10-08": False,
                            "2026-10-09": True})]
        h, n = BF.max_streak(qs, "2026-10-09", self.TD)
        assert (h, n) == (3, 3)

    def test_partial_member_excluded_not_zeroed(self):
        """★ 某只票窗口内缺数据 ⇒ 排除该票，而不是当 0 参与取max。

        当 0 参与不会影响 max（除非它本来是最高的），
        但它会虚增 `counted` ⇒ 样本量注水。
        """
        qs = [self._q("A", {"2026-10-07": True, "2026-10-08": True,
                            "2026-10-09": True}),
              {"code": "B", "streaks": {"2026-10-09": 2}}]   # 缺前两天
        h, n = BF.max_streak(qs, "2026-10-09", self.TD)
        assert h == 3 and n == 1, f"缺数据的票应被排除，实际 n={n}"

    def test_d4_no_longer_declared_unavailable(self):
        """★ d4 已可算 ⇒ 不得再出现在 `FACTOR_UNAVAILABLE` 里。

        这条守护的是「文档与实现同步」：留空说明写在表头上，
        早晚会有人照着它以为 d4 拿不到而不再实现。
        """
        assert "d4_max_streak" not in BF.FACTOR_UNAVAILABLE
        assert "d5_inflow" in BF.FACTOR_UNAVAILABLE

    def test_streak_series_keys_are_str(self):
        """★★★ `date` 与 `str` 的等值比较恒为 False，会**静默清空结果**。

        实测事故（2026-10-10）：`if d not in all_days` 里 `d` 是
        `datetime.date` 而 `all_days` 是 `list[str]` ⇒ 恒 True
        ⇒ 329 只票全被 continue ⇒ 日志打「0 只成分票有 K 线」
        而**不报任何错**。
        ⇒ 判据：`limit_up_streaks` 产出的键必须是 `str`。
        """
        r = BF.limit_up_streaks([(__import__("datetime").date(2026, 10, 9),
                                  True)], self.TD)
        assert all(isinstance(k, str) for k in r), r
        assert r["2026-10-09"] == 1

    def test_entity_column_prefers_computed_factor(self):
        """★ 因子 `d4_max_streak` 必须落到实体列 `max_streak`。

        实测：只透传原料时 `max_streak` 383 行**全 NULL**，
        而页面读的是实体列 ⇒ 因子算出来了却看不见。
        """
        src = _src("backfill_board_factors.py")
        assert '"max_streak": d4 if d4 is not None else mx_streak' in src, (
            "本地算出的 d4 必须优先写入 max_streak 实体列")
        assert '"vol_ratio": d6 if d6 is not None else vol_ratio' in src

    def test_no_date_str_membership_comparison(self):
        """★★★ 守护「参与日期集合筛选的变量必须先 str() 归一化」。

        实测事故（2026-10-10）：`if d not in all_days` 里 `d` 是
        `datetime.date`、`all_days` 是 `list[str]` ⇒ 恒 True ⇒ 329 只票
        全部continue⇒ 因子算出「0 只成分票」而**全程零报错**。
        这类 bug 的特征是「结果为空而不是异常」，
        所以只能**静态**守护：看用来做 `not in` 判断的变量
        是否经过 `str()` 归一。

        ★ 判据只针对**循环体内、直接参与 `not in` 的变量**：
          变异方式是把 `dkey` 换回 `d`（去掉 str 归一），
          此时该变量来自游标赋值、无 `str()` ⇒ 判据必须红。
        """
        tree = ast.parse(_src("backfill_board_factors.py"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "load_streak_series"), None)
        assert fn is not None, "未找到 load_streak_series，判据本身失效"

        def normalized_names(scope: ast.AST) -> set[str]:
            out = set()
            for n in ast.walk(scope):
                # x = str(y) / x = f"{y}"
                if isinstance(n, ast.Assign) and len(n.targets) == 1:
                    tgt = n.targets[0]
                    if isinstance(tgt, ast.Name) and isinstance(n.value, ast.Call):
                        fnc = n.value.func
                        if isinstance(fnc, ast.Name) and fnc.id == "str":
                            out.add(tgt.id)
            return out

        checked = 0
        for loop in [n for n in ast.walk(fn)
                     if isinstance(n, (ast.For, ast.While))]:
            norm = normalized_names(loop)
            for cmp_ in [c for c in ast.walk(loop)
                         if isinstance(c, ast.Compare)
                         and isinstance(c.ops[0], ast.NotIn)
                         and isinstance(c.left, ast.Name)]:
                var = cmp_.left.id
                assert var in norm, (
                    f"`{var}` 直接参与日期集合的 `not in` 判断，"
                    f"却没有 str() 归一化 —— date/str 比较恒为 False，"
                    f"会静默清空结果且不报错")
                checked += 1
        assert checked > 0, "未找到任何日期 not in 判断，判据本身失效"

    def test_select_cols_match_unpack_count(self):
        """★★ SELECT 的原料列个数必须等于解包变量个数（静默错位的高发点）。

        列数与解包数不一致时，DuckDB 只会在多出来的位置报错，
        少写的那一列则**整列拿到None** —— 数据看着「有值」实则错位。
        ★ 用 AST 而不是正则：正则要处理 Python 相邻字符串隐式拼接，
          极易写出「永远匹配不上」的判据（本次就先踩了一次）。
        """
        src = _src("backfill_board_factors.py")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "backfill")
        sel_cols = None
        for n in ast.walk(fn):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value.startswith("SELECT board_code, board_name")
                    and "board_strength" in n.value):
                sel_cols = [x.strip() for x in
                            n.value[n.value.index("SELECT") + 6:
                                    n.value.index("FROM")].split(",")]
        assert sel_cols, "未找到原料 SELECT（判据本身失效）"
        unpack = None
        for n in ast.walk(fn):
            if isinstance(n, ast.For) and isinstance(n.target, ast.Tuple):
                unpack = [e.id for e in n.target.elts
                          if isinstance(e, ast.Name)]
        assert unpack, "未找到 snap_rows 解包（判据本身失效）"
        assert len(sel_cols) == len(unpack), (
            f"SELECT {len(sel_cols)} 列 vs 解包 {len(unpack)} 个变量："
            f"{sel_cols} / {unpack}")


# ═══════════════════════════════════════════════ 变异验证（判据是否真阳性）
MUTATIONS = [
    ("TestLimitRatio",
     "test_missing_preclose_excluded_not_false",
     'base += 1',
     'base += 1  # mutated'),
    ("TestVolExpansion", "test_known_ratio",
     "return mean / med", "return 1.0  # mutated"),
    ("TestRankSlope", "test_two_points_returns_none",
     "if len(pts) < 3:", "if len(pts) < 2:  # mutated"),
]


class TestMutationSensitive:
    """★ 只有真做过变异，才知道这些测试不是「永远绿的摆设」。

    这里做的是**离线反例**：把判据表达式原地改成错的值，
    确认对应断言真的会失败。反例在内存里构造，**不改源码文件**。
    """

    def test_limit_ratio_double_count_would_fail(self):
        """把「缺 pre_close 也计入分母」实现出来，判据必须失败。"""
        good = BF.limit_ratio([_q("600000", None, 11.0, None),
                               _q("600001", 10.0, 10.5, 5.0)])
        assert good[2] == 1
        # 反例：分母含缺失项 ⇒ base=2
        assert 2 != good[2], "反例构造失效，判据不可区分"

    def test_vol_expansion_flat_would_fail(self):
        assert BF.vol_expansion([_q(str(i), amt=v) for i, v in
                                 enumerate([1, 1, 1, 1, 10])]) == pytest.approx(2.8)

    def test_rank_slope_two_pts_would_fail(self):
        """反例：放开 2 点限制会得出斜率（=两点连线），噪声下必假。"""
        h = [(datetime(2026, 10, 7, tzinfo=CST), 10),
             (datetime(2026, 10, 8, tzinfo=CST), 3)]
        assert BF.rank_slope(h) is None