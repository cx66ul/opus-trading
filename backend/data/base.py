# -*- coding: utf-8 -*-
"""
Hawk · 数据源基础设施：令牌桶限流 + 指数退避 + 健康度 + 字段合理性校验

对应附录 3.3「免费源的工程约束」，把7 条兜底手段固化为可复用组件：
  1. 令牌桶限流（超限排队而非拒绝）
  2. 请求合并/ 批量（由各适配器实现）
  3. 请求伪装（UA / Referer / 会话保持 / 间隔抖动）
  4. 指数退避（1→2→4→8→…→60s 封顶）
  5. 健康度评分（成功率 / 延迟滑窗）
  6. 字段变动告警（解析失败率 > 5%）
  7. 合规（控制频率，不做批量抓取）

设计要点：这些能力对所有数据源通用，因此沉到基类，
子类只关心"怎么发请求"和"怎么解析响应"（铁律一：接口 + 注册表）。
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))

# 错误码分段（附录 L.2）
ERR_SOURCE_TIMEOUT = 1001
ERR_SOURCE_RATELIMIT = 1002
ERR_SOURCE_PARSE = 1003
ERR_SOURCE_ALL_DOWN = 1004


class SourceError(Exception):
    """数据源异常。code 落在 1xxx 段。"""

    def __init__(self, code: int, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.retryable = retryable


# ------------------------------------------------------------------ 令牌桶
class TokenBucket:
    """按域名单独配额的令牌桶。

    超限时**排队等待**而非抛异常（附录 3.3 第1 条）：
    免费源没有 429 语义，硬拒绝会导致盘中扫描中断。
    """

    def __init__(self, qps: float, capacity: float | None = None) -> None:
        self.qps = float(qps)
        self.capacity = float(capacity if capacity is not None else max(qps, 1))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """取令牌，返回实际等待秒数。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.qps
                )
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                need = (tokens - self._tokens) / self.qps
            time.sleep(min(need, 5.0))
            waited += min(need, 5.0)


# ------------------------------------------------------------------ 健康度
@dataclass
class Health:
    """滑动窗口健康度。UI 顶栏红黄绿灯直接读 success_rate / p95。

    计数设计：所有计数器共用同一个 maxlen 的 deque 窗口，
    统计时以「窗口内总样本数」为分母，避免各计数器独立伸缩导致比率失真
    （早期版本分子用 parse_fails、分母用 successes，会算出 > 100% 的比率）。
    """

    name: str
    window: int = 50
    successes: deque = field(default_factory=lambda: deque(maxlen=50))
    failures: deque = field(default_factory=lambda: deque(maxlen=50))
    parse_fails: deque = field(default_factory=lambda: deque(maxlen=50))
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))
    consecutive_fail: int = 0
    last_error: str = ""
    last_ok_at: str = ""

    def ok(self, latency_ms: int) -> None:
        self.successes.append(1)
        self.latencies.append(latency_ms)
        self.consecutive_fail = 0
        self.last_ok_at = datetime.now(CST).isoformat(timespec="seconds")

    def fail(self, err: str) -> None:
        self.failures.append(1)
        self.consecutive_fail += 1
        self.last_error = err

    def parse_fail(self) -> None:
        """本次请求解析存在异常字段（口径：一次请求 = 一个样本）。"""
        self.parse_fails.append(1)

    def parse_ok(self) -> None:
        """本次请求解析全部通过。与 parse_fail 配对，保证窗口长度同步。"""
        self.parse_fails.append(0)

    @property
    def total(self) -> int:
        return len(self.successes) + len(self.failures)

    @property
    def success_rate(self) -> float:
        return 1.0 if self.total == 0 else len(self.successes) / self.total

    @property
    def parse_fail_rate(self) -> float:
        """解析失败率 = 窗口内异常请求数 / 已解析请求数，落在 [0,1]。"""
        n = len(self.parse_fails)
        return 0.0 if n == 0 else len([x for x in self.parse_fails if x]) / n

    @property
    def p95_ms(self) -> int:
        if not self.latencies:
            return 0
        s = sorted(self.latencies)
        return s[min(len(s) - 1, int(len(s) * 0.95))]

    @property
    def status(self) -> str:
        """green / yellow / red —— 对应 UI 顶栏三色灯。"""
        if self.consecutive_fail >= 5:
            return "red"
        if self.success_rate < 0.8 or self.parse_fail_rate > 0.05:
            return "yellow"
        return "green"

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "success_rate": round(self.success_rate, 4),
            "parse_fail_rate": round(self.parse_fail_rate, 4),
            "p95_ms": self.p95_ms,
            "consecutive_fail": self.consecutive_fail,
            "total_calls": self.total,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
        }


# ------------------------------------------------------------------ 字段校验
# 上下界用于捕捉「字段错位」与「单位放大」两类静默故障。
#
#★ 涨跌幅上界必须按市场区分，不能一刀切（实测踩坑）：
#    主板 ±10%、创业板/科创板 ±20%、**北交所 ±30%**
#  若统一按 20%设限，会把北交所的合法涨停（如力王股份 +29.96%）误判为脏数据，
#  导致北交所样本被静默丢弃 —— 这类误伤比漏检更隐蔽。
#  此处取北交所上限 30% 作为全局上界，并在 pct_chg_max_for_market 里保留按市场收紧的能力。
SANITY_LIMITS = {
    "price_max": 3000.0,      # A股最高价量级（茅台约 2600），非复权口径可更高
    "pct_chg_abs_max": 35.0,   # 实时快照用：北交所 30% + 5% 余量
    # K 线用：放宽到 100%。除权除息会让前复权价产生巨大跳空，
    # 历史上实测出现过 -34.53% 的真实单日跌幅（301686），
    # 用 35% 上界会误丢这类正常数据。仍设 100% 上界以拦截单位错位（×100）。
    "pct_chg_kline_abs_max": 100.0,
    "turnover_max": 100.0,
    "mv_max": 1e14,
    "vol_max": 1e13,
}

# 按市场 / 板块细化的涨跌幅上界（实测踩坑两次，必须区分清楚）
#   主板（沪 60xxxx / 深 00xxxx）      ±10%
#   创业板（30xxxx）                    ±20%
#   科创板（688xxx）                    ±20%
#   北交所（920xxx / 43xxxx / 83-87xxxx）±30%
# 早期版本统一按 ±12% 校验，把科创板 688196「+20% 涨停」误判为脏数据；
# 又曾统一按 ±25%，把北交所 920627「+29.96%」误判。两次误伤都是漏掉分板块。
PCT_CHG_MAX_BY_MARKET = {
    "SH": 12.0,   # 沪市主板
    "SZ": 22.0,   # 深市主板 10% / 创业板 20%
    "BJ": 32.0,   # 北交所 30%
}
# 科创板与沪市主板共用 SH 标识，需按代码前缀细分
PCT_CHG_MAX_BY_CODE_PREFIX = {
    "688": 22.0,  # 科创板
    "689": 22.0,  # 科创板 CDR
}


def pct_chg_limit(code: str | None, symbol: str | None = None,
                  market: str | None = None) -> float:
    """按代码/市场推导涨跌幅合法上界。无法判定时返回全局宽松上界。"""
    c = str(code or "")
    for pref, lim in PCT_CHG_MAX_BY_CODE_PREFIX.items():
        if c.startswith(pref):
            return lim
    m = market or {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(
        str(symbol or "")[:2].lower(), "")
    return PCT_CHG_MAX_BY_MARKET.get(m, SANITY_LIMITS["pct_chg_abs_max"])

SANITY_RULES = {
    # price 必须 > 0：这是「单位放大」故障的核心检测点
    #（涨停池 p 字段未除 1000 时会变成 22220，必须拦下）
    "price":      lambda v: isinstance(v, (int, float)) and 0 < v <= SANITY_LIMITS["price_max"],
    # high/low 允许为 0：停牌股接口返回 high=low=open=0，属合法的「今日无成交」状态。
    # 这类记录由is_suspend 字段单独表达，缠论引擎据它跳过该bar（附录 C.3），
    # 不应在此处误判为字段错位（实测曾误丢 15 只停牌股）。
    "high":       lambda v: v is None or (isinstance(v, (int, float)) and 0 <= v <= SANITY_LIMITS["price_max"]),
    "low":        lambda v: v is None or (isinstance(v, (int, float)) and 0 <= v <= SANITY_LIMITS["price_max"]),
    "pre_close":  lambda v: isinstance(v, (int, float)) and 0 < v <= SANITY_LIMITS["price_max"],
    "close":      lambda v: isinstance(v, (int, float)) and 0 < v <= SANITY_LIMITS["price_max"],
    "high":       lambda v: v is None or (isinstance(v, (int, float)) and 0 <= v <= SANITY_LIMITS["price_max"]),
    "low":        lambda v: v is None or (isinstance(v, (int, float)) and 0 <= v <= SANITY_LIMITS["price_max"]),
    # ★★ pct_chg 在 K 线场景下**放宽到 100% 上界**（原为 35）
    #
    # 实测教训（2026-10-08 全量同步 137 万行）：
    #   按「日涨跌幅上限 22%~32%」校验 K 线，会误丢 12 条**完全真实**的记录，例如：
    #     301686 2026-09-23 -34.53%（用前复权价重算完全吻合）
    #     603248 2025-12-24 -29.47%（同）
    #   这些是真实极端行情：除权除息跳空、新股上市初期无涨跌幅限制、妖股波动。
    #   **用日涨跌幅上限去卡 K 线是错的** —— 那个上限只适用于实时快照
    #   （盘中瞬时涨跌幅），不适用于历史 K 线（前复权价遇除权会产生巨大跳空）。
    #   仍保留 100% 上界：超过 100% 只可能是单位错位（如 ×100）而非真实行情。
    "pct_chg":    lambda v: v is None or (
        isinstance(v, (int, float)) and abs(v) <= SANITY_LIMITS["pct_chg_kline_abs_max"]),
    "turnover":   lambda v: v is None or (
        isinstance(v, (int, float)) and 0 <= v <= SANITY_LIMITS["turnover_max"]),
    "code":       lambda v: isinstance(v, str) and len(v) == 6 and v.isdigit(),
    "trade_date": lambda v: isinstance(v, str) and len(v) == 10 and v[4] == "-",
    "ts":         lambda v: isinstance(v, str) and len(v) >= 12,
    "volume":     lambda v: v is None or (
        isinstance(v, (int, float)) and 0 <= v < SANITY_LIMITS["vol_max"]),
    "amount":     lambda v: v is None or (
        isinstance(v, (int, float)) and 0 <= v < SANITY_LIMITS["vol_max"]),
    "float_mv":   lambda v: v is None or (
        isinstance(v, (int, float)) and 0 <= v < SANITY_LIMITS["mv_max"]),
    "total_mv":   lambda v: v is None or (
        isinstance(v, (int, float)) and 0 <= v < SANITY_LIMITS["mv_max"]),
}


def validate_fields(row: dict[str, Any], market: str | None = None,
                    strict_pct: bool = False) -> list[str]:
    """返回违规字段名列表。空列表 = 通过。

    这是「字段变动告警」的检测器：免费接口字段会无声变动，
    典型表现是单位放大（涨停池 p 字段 ×1000）、字段错位（收盘价落到 high 位）。
    因此除了类型与格式，还必须有量程上下界。

    ★ strict_pct 控制涨跌幅是否按市场上界校验：
      · strict_pct=True  —— 用于**实时快照**（盘中瞬时涨跌幅，上界明确）
      · strict_pct=False（默认）—— 用于**历史 K 线**。
        除权除息会让前复权价产生巨大跳空，实测出现过 -34.53% 的真实单日跌幅
        （301686 2026-09-23，用前复权价重算完全吻合）。
        若对 K 线套用实时上界，会误丢这类正常数据（实测误丢 12 条）。
    """
    bad = []
    limit = pct_chg_limit(row.get("code"), row.get("symbol"), market) if strict_pct else None
    for k, v in row.items():
        if k == "pct_chg" and limit is not None and v is not None:
            try:
                if not (isinstance(v, (int, float)) and abs(v) <= limit):
                    bad.append(k)
                    continue
            except Exception:
                bad.append(k)
                continue
        rule = SANITY_RULES.get(k)
        if rule is None:
            continue
        try:
            if not rule(v):
                bad.append(k)
        except Exception:
            bad.append(k)
    return bad


# ------------------------------------------------------------------ 退避
class Backoff:
    """指数退避 + ±30% 抖动。连续失败超过阈值触发切源。"""

    def __init__(
        self,
        base: float = 1.0,
        cap: float = 60.0,
        jitter: float = 0.30,
        fail_switch_threshold: int = 5,
    ) -> None:
        self.base = base
        self.cap = cap
        self.jitter = jitter
        self.fail_switch_threshold = fail_switch_threshold
        self._n = 0

    def reset(self) -> None:
        self._n = 0

    @property
    def attempts(self) -> int:
        return self._n

    def should_switch_source(self) -> bool:
        """连续失败达阈值 -> 应切到备源。"""
        return self._n >= self.fail_switch_threshold

    def next_delay(self) -> float:
        """返回本次退避时长并递增重试计数。

        序列必须为 base, 2*base, 4*base ...（附录 3.3 第 4 条：1→2→4→8→…→60 封顶）。
        注意重试计数从 0 开始，因此用 self._n 本身作指数，
        早期版本误写成 max(0, n-1) 会让前两次延时相同，退避形同虚设。
        """
        d = min(self.cap, self.base * (2 ** self._n))
        self._n += 1
        return d * (1 + random.uniform(-self.jitter, self.jitter))


def jittered_sleep(base: float, jitter: float = 0.30) -> None:
    """批间随机 sleep，规避固定周期被识别。"""
    time.sleep(max(0.05, base * (1 + random.uniform(-jitter, jitter))))


if __name__ == "__main__":
    h = Health("demo")
    for i in range(9):
        h.ok(100 + i * 10)
    h.fail("boom")
    print("健康度示例:", h.snapshot())

    b = Backoff()
    print("退避序列:", [round(b.next_delay(), 2) for _ in range(7)])

    tb = TokenBucket(qps=5)
    t0 = time.monotonic()
    for _ in range(12):
        tb.acquire()
    print(f"令牌桶 12 次取令牌耗时 {time.monotonic() - t0:.2f}s（5 QPS 理论约 2.2s）")

    print("字段校验 price=1255.79 ->", validate_fields({"price": 1255.79}))
    print("字段校验 price=2222000（放大1000倍）->", validate_fields({"price": 2222000}))
    print("字段校验 pct_chg=3000 ->", validate_fields({"pct_chg": 3000.0}))
    print("字段校验 code='60051' ->", validate_fields({"code": "60051"}))