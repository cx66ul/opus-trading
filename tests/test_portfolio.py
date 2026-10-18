# -*- coding: utf-8 -*-
"""★ 持仓：股数未知 ≠ 没持仓

★★★ 这条防的是「缺失被当成事实为否」
--------------------------------------------------
实测（2026-10-11）：用户报了三只持仓股（锡华科技/维科技术/格尔软件）
但没给股数与成本。原 `list_positions(holding_only=True)` 的过滤是

    WHERE status='holding' AND shares > 0

⇒ `shares` 为 NULL ⇒ `shares > 0` 为假 ⇒ **三条持仓全部消失**，
   而 API 正常返回 200、`rows` 为空 ⇒ 看起来像「你没持仓」。

★ 为什么这属于「缺失当0」的同一类错误：
  「股数未知」缺失的是**规模**，不是「是否持有」这个**事实**；
  用规模去推断事实，等于拿缺失当「否」用了。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CODES = ["603248", "600152", "603232"]      # 锡华科技/维科技术/格尔软件


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """给 `portfolio` 一个独立的临时 SQLite，避免污染真实持仓。"""
    from backend.data import sqlite as S
    p = tmp_path / "t.db"
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript(S.DDL)
    con.executemany(
        "INSERT INTO position (code, name, shares, cost, status) "
        "VALUES (?,?,?,?,?)",
        [(c, f"股票{i}", None, None, "holding") for i, c in enumerate(CODES)])
    con.commit()

    def fake_get_conn():
        return con
    monkeypatch.setattr(S, "get_conn", fake_get_conn)
    import backend.domain.portfolio as pf
    monkeypatch.setattr(pf, "get_conn", fake_get_conn)
    return con


class TestSharesUnknownIsStillHolding:
    def test_null_shares_still_listed(self, tmp_db):
        from backend.domain import portfolio
        rows = portfolio.list_positions(holding_only=True)
        assert {r.code for r in rows} == set(CODES), (
            f"股数为 NULL 的持仓被当成没持仓：{[r.code for r in rows]}")

    def test_shares_kept_as_none_not_zero(self, tmp_db):
        """★ 股数缺失必须仍是 None，**不能被填成 0** ——
        填 0 会让「持仓市值」「盈亏」都算出 0 元的漂亮数字。"""
        from backend.domain import portfolio
        rows = {r.code: r for r in portfolio.list_positions(holding_only=True)}
        for c in CODES:
            assert rows[c].shares is None, f"{c} 股数被填成了 {rows[c].shares}"
            assert rows[c].cost is None

    def test_closed_position_still_excluded(self, tmp_db):
        """★ 反向自证：`status='closed'` 仍必须被排除。

        若判据写得太宽（把 status 也放行），这条会红。
        """
        tmp_db.execute("UPDATE position SET status='closed' WHERE code=?",
                       (CODES[0],))
        tmp_db.commit()
        from backend.domain import portfolio
        rows = {r.code for r in portfolio.list_positions(holding_only=True)}
        assert CODES[0] not in rows, "已平仓仍被列为持仓"
        assert len(rows) == len(CODES) - 1


class TestGuardSource:
    """★ 静态守护：源码里不得再出现 `shares>0` 这类「以规模推断事实」。"""

    def test_no_shares_gt_zero_filter(self):
        """★ 只看 **SQL 字符串常量**，不看注释。

        解释这个 bug 的注释里必然出现 `shares > 0` 字样，
        按源码子串判会永远红（本次就先踩了这个坑）。
        """
        import ast
        tree = ast.parse((ROOT / "backend" / "domain" / "portfolio.py")
                         .read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "list_positions")
        # ★ 要收**所有** SQL 片段：初始化的常量 + 后续 `sql += ...` 的右值。
        #   过滤条件只出现在 += 那一段，只看 BinOp 会整个漏掉（判据恒失效）。
        frags = [n.value for n in ast.walk(fn)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and any(k in n.value.upper() for k in
                         ("SELECT", "WHERE"))]
        assert frags, "未找到 SQL 片段，判据本身失效"
        flat = " ".join(" ".join(f.split())
                        for f in frags).lower()
        # ★ 只看 WHERE 子句里的比较：``shares >= 0`` 是允许的
        #   （允许 shares 为 NULL），禁的是 ``shares > 0``
        where = flat.split("where", 1)[-1] if "where" in flat else flat
        assert "shares > 0" not in flat.replace("shares>=0", ""), (
            "仍用 shares>0 判定持仓 ⇒ 股数未知时整条消失")
        assert "shares is not null" not in where, (
            "WHERE 里排除了 shares IS NULL ⇒ 正是本 bug 的成因")

    def test_position_has_no_fake_defaults(self):
        """★ 成本/股数不得有 DEFAULT 值。

        有默认值 = 缺数据会被自动填成 0 或某个「看起来合理」的数，
        而 0 成本会让收益率计算全部失真。
        """
        from backend.data.sqlite import DDL
        i = DDL.index("CREATE TABLE IF NOT EXISTS position")
        block = DDL[i:DDL.index(");", i)]
        assert "shares INTEGER NOT NULL" not in block, "shares 不得 NOT NULL"
        assert "cost REAL NOT NULL" not in block, "cost 不得 NOT NULL"
        assert "DEFAULT 0" not in block, "成本/股数不得有 DEFAULT 0"
