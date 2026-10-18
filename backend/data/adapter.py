# -*- coding: utf-8 -*-
"""
Hawk · 数据适配器基类与注册表

铁律一「接口 + 注册表」：换数据源只是「注册一个新实现」，不改核心代码。
新增数据源只需：
    1.继承 BaseAdapter
    2. 实现 fetch_realtime / fetch_kline 等所需方法
    3. @register("your_source") 装饰器注册

基类统一承担：令牌桶限流、指数退避、重试、健康度统计、字段校验、
请求伪装（UA/Referer）、失败切源信号。
子类只负责「怎么发请求」「怎么解析响应」。

★ 所有方法禁止取系统时间：时间一律由调用方通过 as_of 传入（铁律二）。
  唯一的例外是 HTTP 层必须携带的真实抓取时间（记入 meta，不参与领域计算）。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Protocol

import httpx
from loguru import logger

from backend.data.base import (
    ERR_SOURCE_ALL_DOWN,
    ERR_SOURCE_PARSE,
    ERR_SOURCE_TIMEOUT,
    Backoff,
    Health,
    SourceError,
    TokenBucket,
    jittered_sleep,
    validate_fields,
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class Quote(Protocol):
    """统一行情结构。所有适配器输出此结构，上层不感知来源差异。"""

    code: str
    name: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    volume: int
    amount: float
    pct_chg: float
    ts: str


class BaseAdapter(ABC):
    """数据源适配器基类。"""

    name: str = "base"
    qps: float = 5.0
    referer: str = ""
    encoding: str = "utf-8"

    def __init__(self, qps: float | None = None) -> None:
        self.bucket = TokenBucket(qps or self.qps)
        self.backoff = Backoff()
        self.health = Health(self.name)
        self._client: httpx.Client | None = None

    # ---------------- HTTP 基础设施（子类通常不需覆盖） ----------------
    @property
    def client(self) -> httpx.Client:
        """复用连接。trust_env=False 必须保留：沙箱代理会劫持请求导致误判。"""
        if self._client is None:
            headers = {"User-Agent": DEFAULT_UA}
            if self.referer:
                headers["Referer"] = self.referer
            self._client = httpx.Client(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                trust_env=False,          # 绕过系统/沙箱代理
                headers=headers,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get(self, url: str, params: dict | None = None, timeout: float = 20.0) -> str:
        """带限流/退避/健康度的 GET。返回解码后的文本。

        失败时抛 SourceError，由 fetch_with_failover 决定是否切源。
        """
        if self.backoff.should_switch_source():
            raise SourceError(
                ERR_SOURCE_ALL_DOWN,
                f"{self.name} 连续失败 {self.backoff.attempts} 次，建议切源",
            )

        self.bucket.acquire()
        t0 = datetime.now()
        try:
            r = self.client.get(url, params=params or {},
                                timeout=timeout)
        except httpx.TimeoutException as exc:
            self.health.fail(f"timeout: {exc}")
            raise SourceError(ERR_SOURCE_TIMEOUT, f"{self.name} 超时: {exc}") from exc
        except httpx.HTTPError as exc:
            self.health.fail(f"http: {exc}")
            raise SourceError(ERR_SOURCE_ALL_DOWN, f"{self.name} 网络错误: {exc}") from exc

        elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)

        if r.status_code != 200:
            self.health.fail(f"http {r.status_code}")
            raise SourceError(
                ERR_SOURCE_ALL_DOWN, f"{self.name} HTTP {r.status_code}"
            )

        text = r.content.decode(self.encoding, errors="replace")
        if not text.strip():
            self.health.fail("empty body")
            raise SourceError(ERR_SOURCE_PARSE, f"{self.name} 返回空响应")

        self.health.ok(elapsed_ms)
        self.backoff.reset()
        return text

    # ---------------- 解析工具 ----------------
    def _guard(self, rows: list[dict], kind: str) -> list[dict]:
        """字段合理性校验 + 解析失败率统计（附录 3.3 第 6 条）。

        校验不通过的记录直接丢弃并计数；解析失败率超阈值时告警，
        绝不静默通过 —— 静默通过意味着脏数据流入信号层。

        市场由 symbol 前缀推导，用于按市场收紧涨跌幅上限
        （北交所 ±30%，若按主板 ±10% 校验会误伤合法涨停）。

        ★ strict_pct 由 kind 决定：
          · 实时快照（realtime/ztpool）→ 启用按市场上界校验
          · 历史 K 线（kline_*/minute_*）→ **不启用**。
            实测教训：除权除息会让前复权价产生巨大跳空，
            301686 在 2026-09-23 的 -34.53% 是真实单日跌幅（用前复权价重算吻合），
            按 ±32% 校验会把它当脏数据丢弃。全量同步误丢 12 条此类记录。

        计数口径：一次请求 = 一个样本。请求内部分记录校验失败时，
        上报一次 parse_fail 并附带失败条数，保证 Health 的比率落在 [0,1]。
        """
        good: list[dict] = []
        keep = ("code", "name", "price", "pct_chg", "trade_date")
        strict_pct = not (kind.startswith("kline") or kind.startswith("minute"))
        n_bad = 0
        first_bad: list[str] = []
        for r in rows:
            # 停牌股单独放行：现价为 0、高低价为 0 是合法的「今日无成交」状态，
            # 由is_suspend 字段承载。误丢停牌股会让「停牌」这个信息彻底消失，
            # 而停牌恰恰是缠论引擎必须跳过该 bar 的依据（附录 C.3）。
            if r.get("is_suspend"):
                good.append(r)
                continue
            sym = str(r.get("symbol") or "")
            market = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym[:2].lower())
            bad = validate_fields(r, market=market, strict_pct=strict_pct)
            if bad:
                n_bad += 1
                if len(first_bad) < 3:
                    sample = {k: r.get(k) for k in keep}
                    first_bad.append(f"违规字段={bad} market={market} 样本={sample}")
                continue
            good.append(r)
        if n_bad:
            # 整批全部被拦 -> 说明是系统性字段变动，告警级别最高
            self.health.parse_fail()
            logger.warning(
                f"[{self.name}] 字段校验失败 kind={kind} {n_bad}/{len(rows)} 条被丢弃。"
                f"疑似接口字段变动或单位口径变化。示例: {'; '.join(first_bad)}"
            )
        elif rows:
            self.health.parse_ok()
        return good

    @staticmethod
    def _f(v: Any) -> float | None:
        """宽松浮点解析：免费源常用 '-' / '' / 'null' 表示缺失。"""
        if v is None or v == "" or v == "-":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if f != f else f      # 过滤 NaN

    @staticmethod
    def _i(v: Any) -> int | None:
        f = BaseAdapter._f(v)
        return None if f is None else int(f)

    # ---------------- 子类实现 ----------------
    @abstractmethod
    def fetch_realtime(self, codes: list[str], as_of: datetime) -> list[dict]:
        """批量实时行情。必须返回统一结构，code 为 6 位。"""

    @abstractmethod
    def fetch_daily(self, code: str, count: int = 250, as_of: datetime | None = None) -> list[dict]:
        """日线历史，返回按时间升序。"""

    def health_snapshot(self) -> dict[str, Any]:
        return self.health.snapshot()

    def __enter__(self) -> "BaseAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ------------------------------------------------------------------ 注册表
_REGISTRY: dict[str, BaseAdapter] = {}
_REGISTRY_LOCK = threading.Lock()


def register(name: str, factory: Callable[[], BaseAdapter]) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[name] = factory()          # type: ignore[assignment]


def get_adapter(name: str) -> BaseAdapter:
    if name not in _REGISTRY:
        raise KeyError(
            f"数据源 {name} 未注册。已注册: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def registered() -> list[str]:
    return sorted(_REGISTRY)


def all_health() -> list[dict]:
    return [a.health_snapshot() for a in _REGISTRY.values()]


def fetch_with_failover(
    fetchers: list[tuple[str, Callable[[], list[dict]]]],
    retry: int = 2,
    kind: str = "data",
) -> list[dict]:
    """按顺序尝试多个数据源，全部失败才抛异常（多源冗余 + 自动降级）。

    fetchers: [(源名, 调用函数), ...]，按配置里的降级顺序排列。
    """
    errors: list[str] = []
    for src_name, fn in fetchers:
        for attempt in range(retry + 1):
            try:
                return fn()
            except SourceError as exc:
                errors.append(f"{src_name}#{attempt + 1}: {exc.msg}")
                logger.warning(f"[failover] {src_name} 第 {attempt + 1} 次失败: {exc.msg}")
                if not exc.retryable:
                    break
                if attempt < retry:
                    ad = get_adapter(src_name).backoff.next_delay()
                    logger.info(f"[failover] {src_name} 退避 {ad:.1f}s 后重试")
                    jittered_sleep(ad, jitter=0.10)
    raise SourceError(ERR_SOURCE_ALL_DOWN, "所有数据源均不可用: " + " | ".join(errors))


def bootstrap() -> None:
    """注册全部内置数据源。启动时调用一次。"""
    from backend.data.adapters.sina import SinaAdapter
    from backend.data.adapters.tencent import TencentAdapter

    register("tencent", TencentAdapter)
    register("sina", SinaAdapter)
    try:
        from backend.data.adapters.eastmoney import EastmoneyAdapter
        register("eastmoney", EastmoneyAdapter)
    except ImportError as exc:
        logger.warning(f"东财适配器未加载: {exc}")
    try:
        # 同花顺板块源：东财 push2 不可达时的板块能力替代（决策 D1）
        from backend.data.adapters.ths_board import ThsBoardAdapter
        register("ths_board", ThsBoardAdapter)
    except ImportError as exc:
        logger.warning(f"同花顺板块适配器未加载: {exc}")