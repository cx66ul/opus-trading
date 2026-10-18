# -*- coding: utf-8 -*-
"""★ 大盘口径 = 真实上证指数（语料口径，2026-10-11 用户确认）

★★★ 这组测试防的是「口径是代理、但数字看着完全正常」
--------------------------------------------------
`backfill_regime.py` 原来用 `breadth.avg_chg`
（全市场**个股等权均值**）当 `index_pct` 的代理。

当时的标注是诚实的（`index_pct_source="equal_weight_avg"`，
注释也写着「不冒充上证指数」），所以没人发现它是个问题。
2026-10-11 实测 30 个交易日：

    日期          等权均值%    上证%     差(pp)
    2026-09-09     −0.469     +0.278    +0.747
    2026-09-16     +1.418     +0.707    −0.711
    2026-09-21     +1.919     +0.972    −0.946
    2026-10-09     +0.608     +0.050    −0.558

⇒ **符号经常相反**（等权 vs 市值加权，小盘股主导时放大）
⇒ 市场状态会判反，而**不报任何错**：数值合法、范围正常、
   甚至「比代理更极端」看起来像是好现象。

运行：pytest tests/test_regime_index.py -v
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "backfill_regime.py"
FETCH = ROOT / "scripts" / "fetch_index_daily.py"
DUCK = ROOT / "backend" / "data" / "duck.py"
INDEX_MAIN = "sh000001"


def _src(path: Path = SCRIPT) -> str:
    return path.read_text(encoding="utf-8")


class TestIndexTableExists:
    def test_index_daily_ddl_declared(self):
        """★ `index_daily` 表必须有 DDL。

        ★ 坑：`CREATE TABLE IF NOT EXISTS` 只在 `init_db()` 里执行，
          直接跑抓取脚本会
          `Catalog Error: Table with name index_daily does not exist!`
          —— DDL 写了但没人调，等于没建。
        """
        assert "CREATE TABLE IF NOT EXISTS index_daily" in _src(DUCK)

    def test_fetch_script_initialises_db(self):
        """★ 抓取脚本必须先 `init_db()`，不能假设表已存在。"""
        src = _src(FETCH)
        assert "duck.init_db()" in src, (
            "抓取脚本未调用 init_db() —— 首次运行必然 Catalog Error")

    def test_ddl_has_primary_key(self):
        """主键是幂等写入（`INSERT OR REPLACE` 去重）的前提。"""
        src = _src(DUCK)
        i = src.index("CREATE TABLE IF NOT EXISTS index_daily")
        block = src[i:src.index(";", i)]
        assert "PRIMARY KEY (index_code, trade_date)" in block, (
            "index_daily 缺主键 ⇒ 重跑会插出重复行")

    def test_amount_is_never_computed(self):
        """★★ 指数 `amount` 源不给就留 NULL，**绝不自算**。

        个股那边 `amount` 由 `volume × close` 自算是因为库里全空；
        指数这边照抄会引入**第二种**误差（源口径不同），
        而且看起来「有值了」比 NULL 更可信 —— 这正是最坏情况。
        """
        src = _src(FETCH)
        assert "float(d[\"amount\"]) if d.get(\"amount\") else None" in src, (
            "指数 amount 必须源不给就None")
        assert "volume * close" not in src.replace(" ", ""), (
            "指数 amount 不得由 volume×close 自算")


class TestRegimeUsesRealIndex:
    def test_no_avg_chg_as_index_pct(self):
        """★★ `index_pct` 不得直接取等权均值。

        ★ 判据不能是「源码里没有 avg_chg」——
          `avg_chg` 还要留在 detail_json 里做对照/审计，
          完全不用它是可疑的（可能反而把证据也删了）。
          要抓的是「**它被当成 index_pct 用了**」。
        """
        tree = ast.parse(_src())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "build_rows")
        env_calls = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)
                     and n.func.id == "MarketEnv"]
        assert env_calls, "未找到 MarketEnv(...) 调用，判据本身失效"
        for call in env_calls:
            idx = next((k for k in call.keywords if k.arg == "index_pct"), None)
            assert idx is not None, "MarketEnv 未传 index_pct"
            # ★ 允许 `_fallback_idx(b)`（已记账的退路），
            #   禁止裸 `b.avg_chg`
            code = ast.dump(idx.value)
            assert "attr='avg_chg'" not in code, (
                "index_pct 仍在用等权均值 attr='avg_chg'")

    def test_index_main_is_shanghai(self):
        """语料口径：大盘 = 上证指数。"""
        assert f'INDEX_MAIN = "{INDEX_MAIN}"' in _src()

    def test_fallback_is_accounted(self):
        """★★ 退回等权代理时必须**记账 + 留痕**。

        退而不记 ⇒ 事后看到某天状态判反，
        根本不知道那天用的是哪一种口径。
        """
        src = _src()
        assert "_FALLBACK_DAYS" in src, "无回退记账"
        assert "equal_weight_avg_fallback" in src, (
            "回退时未在detail_json 标注实际口径")
        # 报告里必须打印回退天数
        assert "缺" in src and "退回" in src, "报告未披露回退天数"

    def test_attach_cannot_be_parameterised(self):
        """★★ `ATTACH` 是 DDL，**不支持占位符**（实测 Parser Error）。

        本脚本用 `duckdb.connect()`（内存连接）读 parquet，
        读不到主库的 `index_daily` ⇒ 必须 attach 主库，
        而路径只能内插 ⇒ **必须自己挡引号/分号**。
        """
        src = _src()
        i = src.index("ATTACH")
        seg = src[i - 200: i + 200]
        assert "ATTACH ?" not in src, "ATTACH 不支持占位符，路径不能参数化"
        assert "replace(\"'\", \"''\")" in seg or "replace(\"'\", \"''\")" in src, (
            "内插路径未转义单引号 —— 一个引号即可执行任意 SQL")
        assert "拒绝内插" in src, "路径含分号时未拒绝"


class TestLibraryState:
    """★ 库内口径必须已落地（不是只改代码）。"""

    # ★★★ 必须用 `get_ro_conn()`，不能用 `get_conn()`
    #   同进程内 `read_only=True` 与读写连接**不能共存**
    #   （全量跑时前者已由conftest 建好，后者会抛
    #     "Can't open a connection to same database file with a
    #     different configuration"）
    #   ★ 这些断言**只读**，绝不该用写连接。
    @staticmethod
    def _con():
        from backend.data import duck
        return duck.get_ro_conn()

    def test_all_regime_rows_use_real_index(self):
        """★ `market_regime` 每一行的 `index_pct_source` 都必须是真指数。

        混用两种口径而不留痕 ⇒ 无法回答「某天判反是不是因为口径」。
        """
        rows = self._con().execute(
            "SELECT json_extract_string(detail_json, "
            "'$.index_pct_source') AS src, count(*) "
            "FROM market_regime GROUP BY 1").fetchall()
        assert rows, "market_regime 为空"
        bad = {src: n for src, n in rows if src != INDEX_MAIN}
        assert not bad, (
            f"存在非 {INDEX_MAIN} 口径的行：{bad}"
            f"（回退需重跑 fetch_index_daily.py --all）")

    def test_index_daily_has_shanghai(self):
        n = self._con().execute(
            "SELECT count(*) FROM index_daily WHERE index_code = ?",
            [INDEX_MAIN]).fetchone()[0]
        assert n > 1000, f"{INDEX_MAIN} 仅 {n} 根，不足以支撑 regime 回填"

    def test_no_weekend_rows(self):
        """★ 指数表不得含非交易日。

        与个股同一铁律：非交易日的数据来自「上一交易日收盘」的静态快照，
        归一后会**凭空造出库里不存在的交易日**。
        """
        bad = self._con().execute(
            "SELECT count(*) FROM index_daily WHERE index_code = ? "
            "AND dayofweek(trade_date) IN (0, 6)", [INDEX_MAIN]).fetchone()[0]
        assert bad == 0, f"{INDEX_MAIN} 含 {bad} 个周末行"


class TestLossAreaNotDudBullet:
    """★★★ 「亏钱效应面积」曾是一个**哑弹谓词**。

    实测（2026-10-11）：三层都齐 ——
      · `breadth.loss_area_ratio()`计算函数存在
      · `dsl.PREDICATES["loss_area_ratio"]` 谓词存在
      · `StrategyContext.loss_area_ratio` 字段存在
    但**生产链路里没有任何地方给它赋值**，
    只有 `tests/test_dsl.py` 在传值
    ⇒ 实盘恒为 None ⇒「亏钱效应面积 > 30%」**永不触发**，
      而且**系统不报任何错**（None 被当作「数据不足」正常跳过）。

    「定义了」≠「接上了」——这是最贵的形态：
    单元测试全绿，因为测试自己传了值。
    """

    SCRIPT = ROOT / "scripts" / "run_m10_pipeline.py"

    def test_real_backtest_fills_loss_area(self):
        """★ 真实回测必须把市场级横截面填进上下文。"""
        src = self.SCRIPT.read_text(encoding="utf-8")
        assert "loss_area_ratio=mrow.get" in src, (
            "真实回测未填 loss_area_ratio ⇒ 该谓词在实盘链路是哑弹")
        assert "limit_up_today=mrow.get" in src, (
            "未填当日涨跌停家数 ⇒ 无法复算面积，也无法在 UI 展示")

    def test_denom_is_imported_constant(self):
        """★ 分母必须**从 breadth 导入常量**，不写字面量字符串。

        `loss_area_ratio` 只认 `DENOM_LIMIT_POOL` / `DENOM_ALL` 的值；
        手写 `"limit_pool"` 一旦拼错就静默走错口径。
        """
        src = self.SCRIPT.read_text(encoding="utf-8")
        assert "DENOM_LIMIT_POOL" in src
        assert 'LOSSDENOM = "limit_pool"' not in src, "分母不得写字符串字面量"

    def test_loss_area_reuses_one_implementation(self):
        """★ 必须复用 `backtest_tide_rule.build_days`，不重写涨停判定。

        ★★ 判据必须看 **AST 里的真实 import**，
          不能对源码做子串匹配 ——
          注释里写着「复用 breadth.is_limit_up/down」是**说明**，
          按子串判会把它当成「自己实现了一套」而误报
          （实测这个判据第一次就是这么失败的）。
        """
        tree = ast.parse(self.SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    imported.add(a.name)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    imported.add(a.name.split(".")[-1])
        assert "build_days" in imported, (
            "未导入 build_days ⇒ 本文件自行判定涨停，口径会与"
            f"backtest_tide_rule 分叉（当前 import：{sorted(imported)}）")
        assert not ({"is_limit_up", "is_limit_down"} & imported), (
            "本文件直接 import 涨停判定函数 ⇒ 又一套实现")

    def test_pre_close_is_derived_not_stored(self):
        """★★ `pre_close` 必须用 lag(close) 自取。

        实测：库里的 `pre_close` 列只有 **93.7%** 与 `lag(close)` 一致
        ⇒ 剩下 6.3% 涨停价算错 ⇒ 涨跌停家数偏 ⇒ 面积偏
        ⇒ 而这条规则正好拿面积当阈值，**且不会报错**。
        实测修正后：阈值 30% 的触发日数 19 → 23。
        """
        src = (ROOT / "scripts" / "backtest_tide_rule.py").read_text(
            encoding="utf-8")
        i = src.index("def build_days")
        body = src[i:src.index("\ndef ", i + 10)]
        assert "lag(close) over" in body.lower(), (
            "build_days 未用 lag(close) 自取前收")
        assert "and pre_close is not null" not in body, (
            "仍在依赖存储的 pre_close 列（仅 93.7% 可信）")

    def test_predicate_fires_with_real_data(self):
        """★ 反向自证：真实数据下该谓词**必须能命中**。

        若一条都命中不了，说明还在哑弹状态
        —— 而「恒不命中」正是哑弹的定义。
        """
        import importlib
        from datetime import datetime, timedelta, timezone
        sys.path.insert(0, str(ROOT / "scripts"))
        m10 = importlib.import_module("run_m10_pipeline")
        from backend.data import duck
        from backend.domain import dsl
        from backend.domain.strategy import StrategyContext
        CST = timezone(timedelta(hours=8))
        market = m10.loss_area_by_day(duck.get_ro_conn())
        rule = {"rule_id": "T", "if": {"all_of": [
            {"loss_area_ratio": {"op": ">", "value": 0.30}}]}}
        hits = 0
        for d, row in market.items():
            r = row.get("ratio")
            if r is None:
                continue
            ctx = StrategyContext(
                code="000001",
                as_of=datetime.strptime(d, "%Y-%m-%d").replace(
                    tzinfo=CST, hour=15),
                closes=[10.0, 10.1, 10.2],
                loss_area_ratio=r, loss_area_denom=m10.LOSSDENOM)
            if dsl.evaluate(rule, ctx).matched:
                hits += 1
        assert hits > 0, (
            f"真实数据下 0 天命中（{len(market)} 天）⇒ 谓词仍是哑弹")

    def test_missing_does_not_fire(self):
        """★ 缺失必须判 False —— 宁可漏触发，不能乱触发。"""
        from datetime import datetime, timedelta, timezone
        from backend.domain import dsl
        from backend.domain.strategy import StrategyContext
        CST = timezone(timedelta(hours=8))
        rule = {"rule_id": "T", "if": {"all_of": [
            {"loss_area_ratio": {"op": ">", "value": 0.30}}]}}
        ctx = StrategyContext(
            code="000001",
            as_of=datetime(2026, 10, 9, tzinfo=CST, hour=15),
            closes=[10.0])
        assert not dsl.evaluate(rule, ctx).matched
