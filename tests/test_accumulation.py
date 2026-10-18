"""主力吸筹/控盘 S1→S7 状态机的回归测试。

该模型此前完全未实现（backend/ 下 grep「吸筹/洗盘/护盘/上影/下影」零命中），
本文件守住它按交易逻辑原文逐阶段推进、并能在破位时正确重置。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from backend.domain.accumulation import (STAGE_TAG, AccParams, Stage,
                                         evaluate, scan, summary_table)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 10, 9, 15, 0, tzinfo=CST)


def mk(spec: list[tuple]) -> list[dict]:
    """(o, h, low, c, v, turnover) 序列 -> 日线行。"""
    return [
        {"trade_date": f"D{i + 1:03d}", "open": o, "high": h, "low": low,
         "close": c, "volume": v, "turnover": tv}
        for i, (o, h, low, c, v, tv) in enumerate(spec)
    ]


CALM = [(10, 10.1, 9.9, 10, 1000, 1.0)] * 20
S1 = (10, 11.2, 10.0, 11.0, 3000, 4.0)          # 放量爆拉
S2 = (11, 11.3, 10.8, 11.0, 1800, 3.0)          # 上影线
S3A = (11, 11.05, 10.9, 10.95, 1200, 2.0)       # 缩量
S3B = (10.9, 11.0, 10.85, 10.9, 1100, 1.9)      # 连续缩量
S3C = (10.9, 10.98, 10.8, 10.85, 1000, 1.85)    # 连续缩量
S4 = (10.85, 10.9, 10.75, 10.78, 950, 1.8)      # 换手趋稳 + 阴跌
S5 = (10.78, 11.1, 10.3, 10.9, 1300, 2.0)       # 长下影护盘，底线 10.30
S6 = (10.9, 11.4, 10.9, 11.3, 2200, 4.2)        # 量价齐升
S7 = (11.3, 11.8, 11.2, 11.7, 2400, 4.5)        # 确认进场


def stages_of(res) -> list[str]:
    return [t.to.value for t in res.transitions]


def test_full_sequence_reaches_s7():
    """完整形态必须按 S1→S7 顺序推进，并给出买点、护盘底线与止损。"""
    r = evaluate("T", mk([*CALM, S1, S2, S3A, S3B, S3C, S4, S5, S6, S7]), AS_OF)
    assert r.stage is Stage.S7_CONFIRMED
    assert stages_of(r) == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert r.is_buy
    assert r.support == pytest.approx(10.30)      # S5 日最低价
    assert r.entry_low is not None and r.entry_high is not None
    assert r.entry_low <= r.entry_high
    assert r.stop is not None and r.stop < r.support
    assert r.confirm_date == "D029"


def test_stops_at_s1_without_followthrough():
    """只有放量爆拉、没有后续，应停在 S1，不得凭空跳到 S7。"""
    r = evaluate("T", mk([*CALM, S1]), AS_OF)
    assert r.stage is Stage.S1_SURGE
    assert not r.is_buy
    assert r.support is None


def test_s1_requires_volume_and_gain():
    """S1 的硬条件：量 ≥ 前5日均量×2 且 涨幅 ≥ 5%。

    量增但涨幅不足、或涨幅够但没放量，都不该触发。
    """
    # 放量但只涨 1%
    r = evaluate("T", mk([*CALM, (10, 10.2, 9.9, 10.1, 3000, 1.5)]), AS_OF)
    assert r.stage is Stage.IDLE
    # 涨幅够但量没放出来
    r = evaluate("T", mk([*CALM, (10, 11.2, 9.9, 11.0, 1100, 1.5)]), AS_OF)
    assert r.stage is Stage.IDLE


def test_relaxed_s1_branch_needs_close_near_high():
    """放宽分支：涨幅 ≥3% 且收盘接近最高价。收盘远离高点则不算。"""
    # 收盘贴着最高价 -> 触发
    r = evaluate("T", mk([*CALM, (10, 11.0, 9.95, 10.95, 3000, 3.0)]), AS_OF)
    assert r.stage is Stage.S1_SURGE
    # 冲高回落收在低位 -> 不触发
    r = evaluate("T", mk([*CALM, (10, 11.0, 9.95, 10.10, 3000, 3.0)]), AS_OF)
    assert r.stage is Stage.IDLE


def test_s3_requires_consecutive_shrink():
    """缩量洗盘要求「连续 ≥2 日」缩量且小实体短上影，单日缩量不算。"""
    r = evaluate("T", mk([*CALM, S1, S2, S3A]), AS_OF)
    assert "S3" not in stages_of(r)


def test_reset_on_breaking_support():
    """跌破护盘底线 -> 形态破坏，重置回 IDLE。"""
    broken = (10.5, 10.6, 9.5, 9.6, 1400, 2.2)   # 收盘 9.6 < 10.30*0.97
    r = evaluate("T", mk([*CALM, S1, S2, S3A, S3B, S3C, S4, S5, broken]), AS_OF)
    assert r.stage is Stage.IDLE
    assert stages_of(r)[-1] == "IDLE"
    assert "破坏" in r.transitions[-1].reason


def test_reset_when_s2_window_expires():
    """S1 后超过观察窗口仍未出上影线 -> 失效重置（不硬扛）。

    构造的是**真正没有上影线**的 bar：最高价 == max(开, 收)，
    即收盘就在最高价上。这种形态下 upper=0，不满足「上影 ≥ 实体」。
    """
    no_upper = [(10.5, 11.0, 10.4, 11.0, 900, 2.0)] * 5
    r = evaluate("T", mk([*CALM, S1, *no_upper]), AS_OF)
    assert r.stage is Stage.IDLE
    assert "失效" in r.transitions[-1].reason


def test_s6_requires_turnover_expansion():
    """量价齐升要求换手率呈倍数扩大，只有量增价涨而换手不动不算。"""
    weak = (10.9, 11.4, 10.9, 11.3, 2200, 2.1)      # 换手未翻倍
    r = evaluate("T", mk([*CALM, S1, S2, S3A, S3B, S3C, S4, S5, weak]), AS_OF)
    assert r.stage is Stage.S5_SUPPORT


def test_tags_for_watchlist():
    """S3/S4/S5 必须能在自选股列表上打标签。"""
    assert STAGE_TAG[Stage.S3_SHRINK_WASH] == "洗盘中"
    assert STAGE_TAG[Stage.S4_STABLE_TURNOVER] == "换手趋稳"
    assert STAGE_TAG[Stage.S5_SUPPORT] == "护盘底线"
    r = evaluate("T", mk([*CALM, S1, S2, S3A, S3B, S3C]), AS_OF)
    assert r.tag == "洗盘中"


def test_insufficient_bars_is_reported_not_guessed():
    """数据不足要明说，不能猜。"""
    r = evaluate("T", mk(CALM[:3]), AS_OF)
    assert r.stage is Stage.IDLE
    assert any("不足" in n for n in r.notes)


def test_suspended_bars_are_skipped():
    """停牌行（high/low/close=0）不得参与判定。"""
    rows = mk([*CALM, S1])
    rows.insert(5, {"trade_date": "SUS", "open": 0, "high": 0, "low": 0,
                    "close": 0, "volume": 0, "turnover": None})
    r = evaluate("T", rows, AS_OF)
    assert r.stage is Stage.S1_SURGE


def test_thresholds_are_configurable():
    """阈值必须可由配置覆盖（规格要求全可配）。"""
    strict = AccParams(s1_vol_mult=5.0)          # 要求 5 倍量
    r = evaluate("T", mk([*CALM, S1]), AS_OF, strict)
    assert r.stage is Stage.IDLE, "3 倍量达不到 5 倍门槛，不应触发"
    loose = AccParams(s1_vol_mult=1.5)
    r = evaluate("T", mk([*CALM, S1]), AS_OF, loose)
    assert r.stage is Stage.S1_SURGE


def test_scan_and_summary_shape():
    """批量扫描与汇总表结构。"""
    res = scan({"A": mk([*CALM, S1, S2, S3A, S3B, S3C, S4, S5, S6, S7]),
                "B": mk(CALM)}, AS_OF)
    assert set(res) == {"A", "B"}
    table = summary_table(res)
    assert len(table) == 1                       # IDLE 的 B 不进表
    assert table[0]["code"] == "A"
    assert table[0]["is_buy"] is True


def test_result_is_json_serializable():
    """结果要能直接进邮件/UI（无 set/Enum 泄漏）。"""
    import json
    r = evaluate("T", mk([*CALM, S1, S2, S3A, S3B, S3C, S4, S5, S6, S7]), AS_OF)
    s = json.dumps(r.to_dict(), ensure_ascii=False)
    assert "确认进场" in s
