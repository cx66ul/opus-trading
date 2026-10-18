"""定时任务调度器（附录 6.1 / 6.2）。

设计要点（每条都来自提示词的硬性要求，不是我的偏好）：
1. **统一入口 + 手工触发**：每个任务都是 `Job` 对象，CLI 可 `--run <name>` 手工跑。
2. **超时熔断**：单任务 > timeout_sec 强杀并告警（6.2 明文要求 5 分钟）。
3. **失败重试**：指数退避，最多 retry 次。
4. **交易日守卫**：非交易日任务直接跳过（不报错 —— 休市日跳过是正常行为）。
5. **执行日志**：每次执行写event_log，含耗时/状态/错误摘要。
6. **补齐缺失任务**：合盖唤醒后按「上次成功时间」判断该跑哪些，而不是
   「下次触发时间 > 现在就跳过」—— 那样会永久漏跑（6.2 明文点名）。

★ 与 M1 铁律的衔接：所有 Job 的 as_of 都由调度器注入，任务内部不取系统时间。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent  # backend/scheduler.py ->项目根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger  # noqa: E402

from backend.data.sqlite import get_conn, log_event  # noqa: E402
from backend.domain.trade_cal import is_trade_day, prev_trade_day  # noqa: E402

CST = timezone(timedelta(hours=8))

# 附录 6.1 任务表
TASK_TABLE = [
    # (名称, cron, 是否仅交易日, 超时秒, 说明)
    ("news_247", "* * * * *", False, 55, "消息采集与LLM评价（7×24）"),
    ("premarket", "30 8 * * 1-5", True, 300, "拉隔夜外围 + 盘前报告 → 09:00 发信"),
    # ★ 原为 "*/1 9 9 * 1-5"：第 3 段是 day-of-month，写成 9 会把语义变成
    #   「每月 9 号 09:xx 每分钟」而非「每个交易日 09:15–09:25」。
    #   竞价监控必须每个交易日都跑，故第 3 段应为 *；小时段限定 9 点，
    #   具体 09:15–09:25 的窗口由任务实现内部自行判断。
    ("auction", "*/1 9 * * 1-5", True, 60, "集合竞价监控（09:15-09:25）"),
    ("realtime", "* 9-11,13-14 * * 1-5", True, 55, "跟踪池一分钟快照"),
    ("midday", "30 11 * * 1-5", True, 300, "上午复盘 → 12:00 发信"),
    ("close", "5 15 * * 1-5", True, 300, "收盘快照 + 当日信号命中判定"),
    ("daily_sync", "30 15 * * 1-5", True, 900, "日线增量落库 + 快照"),
    ("ref_sync", "0 20 * * *", False, 600, "股票列表/板块成分/财务同步"),
    ("night_archive", "0 23 * * *", False, 300, "事件归档 + VACUUM + 夜间聚合"),
    ("board_scan", "*/30 9-15 * * 1-5", True, 100, "全市场扫描 → 板块强度重算"),
    ("weekly", "0 10 * * 6", False, 1800, "周六策略批量回测 + 周报"),
]


@dataclass
class JobResult:
    name: str
    ok: bool
    skipped: bool = False
    elapsed_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class TimeoutGuard:
    """超时熔断：用独立线程执行，超时后主线程放弃等待。

    ★ 为什么不用 signal.SIGALRM：Windows 上不可用（无 os.kill 支持），
      而本项目只在 Windows 本机运行。
    ★ 为什么不用「任务内部主动 check()」：任务内部若卡在一个
      没有 check点的网络请求里（如 socket recv），check 永远不会被执行到。
      实测踩过：4秒任务限 1 秒，check 在任务末尾才执行，
      实际耗时 4 秒 —— 检测到了超时，但没拦住。
    ★ 线程无法真正强杀（Python 没有安全的线程终止），但**主线程不再等待**
      即达到调度层的「熔断」效果：调度器继续跑下一个任务，
      卡死的线程留在后台（daemon=True，进程退出即回收）。
      这是 Windows/Python 下的务实折中，比「永不超时」好得多——
      一个卡死的网络请求会堵死整条调度链。
    """

    def __init__(self, name: str, seconds: int):
        self.name = name
        self.seconds = seconds
        self.start = 0.0
        self.result: dict | None = None
        self.error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def run(self, fn: Callable, *args) -> dict:
        """把 fn 丢进守护线程执行，主线程最多等 seconds 秒。"""
        self.start = time.monotonic()

        def _wrap():
            try:
                self.result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 —— 需捕获后传给主线程
                self.error = exc

        self._thread = threading.Thread(
            target=_wrap, daemon=True, name=f"job-{self.name}"
        )
        self._thread.start()
        self._thread.join(timeout=self.seconds)
        if self._thread.is_alive():
            raise TimeoutError(
                f"任务 {self.name} 超时 {self.seconds}s，已熔断"
                f"（后台线程仍存活但调度器不再等待）"
            )
        if self.error is not None:
            raise self.error
        return self.result or {}

    def check(self) -> None:
        """任务内部可主动调用（配合上面的线程机制不影响效果）。"""
        el = time.monotonic() - self.start
        if el > self.seconds:
            raise TimeoutError(
                f"任务 {self.name} 超时 {el:.0f}s > 限 {self.seconds}s，已熔断"
            )

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.start) * 1000)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@dataclass
class Job:
    name: str
    cron: str
    trade_day_only: bool
    timeout_sec: int
    desc: str
    fn: Callable[[datetime, TimeoutGuard], dict] | None = None
    last_ok: str | None = None
    last_status: str = "never"


REGISTRY: dict[str, Job] = {}


def job(name: str) -> Callable:
    """注册任务的装饰器。"""

    def deco(fn: Callable) -> Callable:
        meta = next((t for t in TASK_TABLE if t[0] == name), None)
        if meta is None:
            raise KeyError(f"未在附录 6.1 任务表中定义的任务: {name}")
        REGISTRY[name] = Job(
            name=name,
            cron=meta[1],
            trade_day_only=meta[2],
            timeout_sec=meta[3],
            desc=meta[4],
            fn=fn,
        )
        return fn

    return deco


# ---------------------------------------------------------------- 执行
def run_job(j: Job, as_of: datetime, force: bool = False) -> JobResult:
    """执行单个任务，带交易日守卫 + 超时熔断 + 重试 + 日志。"""
    d = as_of.date().isoformat()

    # ① 交易日守卫
    if j.trade_day_only and not force:
        if not is_trade_day(d):
            logger.info(f"[skip] {j.name}：{d} 非交易日，跳过（正常行为）")
            return JobResult(
                name=j.name, ok=True, skipped=True, detail={"reason": "non_trading_day"}
            )
        # 强制场景（补跑/手工触发）也要记录用的是哪一天
        pd = prev_trade_day(d)
        if pd:
            logger.info(f"[run ] {j.name}：{d} 交易日（前一日 {pd}）")

    if j.fn is None:
        return JobResult(name=j.name, ok=False, error="任务未注册实现")

    # ② 执行 + 重试
    last_err = ""
    for attempt in range(1, 4):
        try:
            tg = TimeoutGuard(j.name, j.timeout_sec)
            detail = tg.run(j.fn, as_of, tg) or {}
            res = JobResult(
                name=j.name, ok=True, elapsed_ms=tg.elapsed_ms, detail=detail
            )
            j.last_ok = as_of.isoformat(timespec="seconds")
            j.last_status = "ok"
            _log_run(res, as_of, attempt)
            logger.info(
                f"[ok  ] {j.name} 耗时 {res.elapsed_ms}ms"
                + (f" {json.dumps(detail, ensure_ascii=False)[:120]}" if detail else "")
            )
            return res
        except TimeoutError as exc:
            last_err = str(exc)
            logger.error(f"[熔断] {j.name}: {exc}")
            break  # 超时不重试 —— 再来一次还是超
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[重试 {attempt}/3] {j.name}: {last_err}")
            if attempt >= 3:
                logger.error(
                    f"[fail] {j.name} 最终失败:\n" + traceback.format_exc()[-800:]
                )
            else:
                time.sleep(2**attempt * 0.5)

    j.last_status = "failed"
    _log_run(JobResult(name=j.name, ok=False, error=last_err), as_of, 3)
    return JobResult(name=j.name, ok=False, error=last_err)


def _log_run(res: JobResult, as_of: datetime, attempt: int) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO event_log (ts, kind, payload) VALUES (?,?,?)""",
        (
            as_of.isoformat(timespec="seconds"),
            "job_run",
            json.dumps(
                {
                    "job": res.name,
                    "ok": res.ok,
                    "skipped": res.skipped,
                    "elapsed_ms": res.elapsed_ms,
                    "attempt": attempt,
                    "error": res.error[:200],
                    "detail": res.detail,
                },
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------- 补齐缺失任务
def catch_up(as_of: datetime, window_hours: int = 24) -> list[JobResult]:
    """补齐因休眠/关机而错过的任务（附录 6.2 明文要求）。

    ★ 不能用「下次触发时间 > 现在就跳过」：合盖睡 8 小时后唤醒，
      08:30 的盘前任务已经错过，若按下次触发时间判断会被永久跳过。

    做法：以每个任务的**理论触发点**为准，判断它在
    [as_of - window_hours, as_of] 窗口内是否本该跑且尚未成功跑过。
    """
    out: list[JobResult] = []
    conn = get_conn()
    window_start = as_of - timedelta(hours=window_hours)

    # 读历史执行记录
    seen: set[str] = set()
    for r in conn.execute(
        "SELECT payload FROM event_log WHERE kind='job_run' AND ts >= ?",
        (window_start.isoformat(timespec="seconds"),),
    ).fetchall():
        try:
            p = json.loads(r[0])
            if p.get("ok") and not p.get("skipped"):
                seen.add(p["job"])
        except Exception:
            continue

    for meta in TASK_TABLE:
        name, _cron, td_only, _to, desc = meta
        if name in seen:
            continue
        # ★★ 未注册实现的任务必须跳过。
        #   原代码直接用 REGISTRY[name] 取函数，而 TASK_TABLE 里
        #   auction/close/midday/ref_sync/weekly 五个任务并无 @job 实现，
        #   唤醒补跑时会抛 KeyError 并中断整轮补齐（catch_up 无 try/except）。
        #   这里显式跳过并记日志，保证补齐流程不因缺实现而整体失败。
        if name not in REGISTRY:
            logger.warning(
                f"[补跑跳过] {name} （{desc}）—— 该任务尚无实现，不影响其余任务补齐")
            continue
        # 落在窗口内的、且是「每日一次」型任务才补跑
        # （高频任务如 news_247/board_scan 不补 —— 补也没意义）
        if name in ("news_247", "board_scan", "realtime", "auction"):
            continue
        # ★★ 时间窗判定修复。原实现比较的是**小时数**：
        #     `window_start.hour <= hh <= as_of.hour`。
        #   由于 window_start = as_of - 24h 与 as_of 同一钟点，
        #   该条件退化为 `hh == as_of.hour` —— 只有正好落在当前小时的任务
        #   才会被补跑，跨夜（如 08:30 盘前任务）永远补不到，
        #   与 docstring 承诺的行为相反。
        #   现改为按**真实时间戳**判断理论触发点是否落在窗口内。
        #   同时补上星期字段判定（原实现完全忽略），
        #   否则 weekly(0 10 * * 6) 会在周五被误判为「本该跑」。
        if not _in_cron_window(_cron, window_start, as_of):
            continue
        if td_only and not is_trade_day(as_of.date()):
            continue
        logger.info(f"[补跑] {name} （{desc}）—— 休眠期间错过")
        out.append(run_job(REGISTRY[name], as_of, force=True))
    return out


def _in_cron_window(cron: str, window_start: datetime,
                    as_of: datetime) -> bool:
    """判断 cron 描述的理论触发点是否落在 (window_start, as_of] 内。

    只解析本项目 TASK_TABLE 实际用到的字段：分钟、小时、星期。
    支持 ``*``、``a-b``、``a,b``、``*/n`` 四种写法。
    日/月字段本项目未使用（均为 *），不解析。
    """
    parts = cron.split()
    if len(parts) != 5:
        return False
    minute_f, hour, _dom, _mon, dow = parts

    def expand(field: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        for chunk in field.split(","):
            step = 1
            if "/" in chunk:
                chunk, _, s = chunk.partition("/")
                step = int(s) if s.isdigit() and int(s) > 0 else 1
            if chunk == "*":
                start, end = lo, hi
            elif "-" in chunk:
                a, _, b = chunk.partition("-")
                start, end = int(a), int(b)
            else:
                if not chunk.isdigit():
                    continue
                start = end = int(chunk)
            out.update(range(start, end + 1, step))
        return out

    hours = expand(hour, 0, 23)
    #★★★ 分钟字段**必须参与判定**。
    #   原实现 `_minute, hour, ... = parts` 把分钟直接丢掉，
    #   理论触发点一律取 `dtime(h, 0)`（整点零分）
    #   ⇒ `30 8 * * 1-5`（08:30）只在 08:00 那一分钟为真，
    #     窗口给到 08:01~08:59 都判 False
    #   ⇒ 后果：`catch_up` 在 08:31 唤醒时**补不到 premarket**，
    #     而长跑器按分钟轮询也永远等不到那一刻
    #     ⇒ 该任务静默永不执行，且不报错。
    #   实测（2026-10-11）：探针 08:30 / 09:20 / 15:30 全部命中为空。
    minutes = expand(minute_f, 0, 59)
    # cron 的 dow：0 与 7 都表示周日；Python weekday() 周一=0
    dows_raw = expand(dow, 0, 7) if dow != "*" else set(range(0, 8))
    # 只要窗口覆盖到的**任一自然日**的对应钟点是该任务的触发点即可
    day = window_start.date()
    end_date = as_of.date()
    while day <= end_date:
        py_dow = day.weekday()                     # 周一=0 … 周日=6
        cron_dow = (py_dow + 1) % 7                # 周日=0
        if cron_dow in dows_raw or (cron_dow == 0 and 7 in dows_raw):
            for h in hours:
                for mi in minutes:
                    ts = datetime.combine(day, dtime(h, mi),
                                          tzinfo=as_of.tzinfo)
                    if window_start < ts <= as_of:
                        return True
        day += timedelta(days=1)
    return False


# ---------------------------------------------------------------- 内置任务实现
@job("realtime")
def _realtime(as_of: datetime, tg: TimeoutGuard) -> dict:
    if abs((datetime.now(CST) - as_of).total_seconds()) > 300:
        raise ValueError("实时接口不能补造历史行情快照")
    from datetime import time as daytime

    from backend.services.tracking import capture_quotes

    clock = as_of.astimezone(CST).time().replace(tzinfo=None)
    if not (
        daytime(9, 30) <= clock <= daytime(11, 30)
        or daytime(13) <= clock <= daytime(15)
    ):
        return {"skipped": True, "reason": "outside_trading_session"}
    result = capture_quotes(as_of)
    result.pop("rows")
    tg.check()
    if result["missing"]:
        raise RuntimeError(f"行情采集不完整: {result['missing']}，{result['errors']}")
    return result


@job("board_scan")
def _board_scan(as_of: datetime, tg: TimeoutGuard) -> dict:
    if abs((datetime.now(CST) - as_of).total_seconds()) > 300:
        raise ValueError("实时接口不能补造历史板块快照")
    from backend.data import duck
    from backend.data.adapter import bootstrap, get_adapter
    from scripts.sync_board_strength import (
        board_history,
        build_rows,
        last_ranks,
        upsert_board_strength,
    )

    bootstrap()
    adapter = get_adapter("ths_board")
    raw = []
    for kind in ("industry", "concept"):
        rows = adapter.fetch_board_list(kind, as_of)
        if not rows:
            raise RuntimeError(f"{kind} 板块返回空数据")
        raw.extend(rows)
        tg.check()
    duck.init_db()
    rows, _ = build_rows(raw, as_of, last_ranks(as_of), board_history(as_of))
    return {
        "written": upsert_board_strength(rows),
        "partial": sum(r["factor_coverage"] < 1 for r in rows),
    }


@job("premarket")
def _premarket(as_of: datetime, tg: TimeoutGuard) -> dict:
    """盘前准备：拉隔夜外围 + 生成盘前报告（09:00 由邮件任务发出）。"""
    from backend.domain.trade_cal import last_n_trade_days

    d = as_of.date().isoformat()
    recent = last_n_trade_days(d, 5)
    logger.info(f"盘前准备：近5个交易日 {recent}")
    tg.check()

    # 生成推荐（真实调用 M6）
    import subprocess

    py = sys.executable
    r = subprocess.run(
        [py, str(_ROOT / "scripts" / "daily_recommend.py"), "--date", d],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=str(_ROOT),
    )
    tg.check()
    ok = r.returncode == 0
    tail = (r.stdout or "").strip().splitlines()[-3:]
    return {
        "date": d,
        "recommend_ok": ok,
        "recent": recent,
        "log_tail": tail,
        "stderr": (r.stderr or "")[-200:],
    }


@job("daily_sync")
def _daily_sync(as_of: datetime, tg: TimeoutGuard) -> dict:
    """日线增量落库 + Parquet 快照（15:30）。"""
    import subprocess

    d = as_of.date().isoformat()
    r = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "sync.py"),
            "daily",
            "--codes",
            str(_ROOT / "data" / "codes_all.txt"),
            "--count",
            "250",
        ],
        capture_output=True,
        text=True,
        timeout=780,
        cwd=str(_ROOT),
    )
    tg.check()
    r2 = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "sync.py"), "snapshot"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_ROOT),
    )
    tg.check()
    return {
        "date": d,
        "daily_rc": r.returncode,
        "snapshot_rc": r2.returncode,
        "stderr": (r.stderr or "")[-200:],
    }


@job("news_247")
def _news(as_of: datetime, tg: TimeoutGuard) -> dict:
    """消息采集（7×24）。"""
    import subprocess

    r = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "fetch_news.py"),
            "--all",
            "--limit",
            "40",
        ],
        capture_output=True,
        text=True,
        timeout=50,
        cwd=str(_ROOT),
    )
    tg.check()
    if r.returncode != 0:
        raise RuntimeError("新闻采集失败：" + (r.stderr or "")[-500:])
    return {"rc": r.returncode, "stderr": (r.stderr or "")[-160:]}


@job("night_archive")
def _night(as_of: datetime, tg: TimeoutGuard) -> dict:
    """夜间归档 + VACUUM（23:00，D12 免打扰时段内不发信）。"""
    conn = get_conn()
    stats = {}
    for t in ("raw_news", "news", "signal", "event_log"):
        stats[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    conn.execute("VACUUM")
    log_event("night_archive", stats, as_of.isoformat(timespec="seconds"))
    return stats


@job("midday")
def _midday(as_of: datetime, tg: TimeoutGuard) -> dict:
    """上午复盘 → 午间发信（11:30）。

    ★ 此前本任务**只有 cron 条目没有实现**（TASK_TABLE 有、
      REGISTRY 无）⇒ `--run midday` 会 KeyError（已由 catch_up 的
      跳过逻辑兜住，但调度链路缺一环）。

    发信用 `retry=1`：规格 §5.3 的重试延迟是 30s/120s/300s，
    默认 retry=3 会让失败拖 450 秒，而本任务 timeout 只有 300s
    ⇒ **必然超时**（`mail_templates.send` 现在已支持透传 retry）。
    """
    from backend.domain import digest
    from backend.domain import mail_templates as mt

    # ★ 发信用 as_of 往前挪到「上午收盘后」——
    #   调度器注入的 as_of 是任务触发时刻，而 cron "30 11" 会在
    #   11:30:00 触发，此时上午已收盘。若直接用它当邮件时间戳，
    #   收件箱里显示的是 11:30，正好卡在收盘瞬间。
    ctx_as_of = as_of.replace(hour=11, minute=35, second=0, microsecond=0)
    ctx = digest.build_midday_context(ctx_as_of)
    tg.check()

    log_event("midday", {"positions": len(ctx.positions)},
              ctx_as_of.isoformat(timespec="seconds"))
    # ★ mail.to 未配置时不发（凭据缺失是合法状态），只记录
    if not getattr(mt.MailSender().cfg, "to", None):
        return {"sent": False, "reason": "mail.to 未配置，跳过发送"}
    res = mt.send(ctx, retry=1)
    tg.check()
    return {"sent": bool(res.get("ok")), "kind": ctx.kind,
            "skipped": bool(res.get("skipped")), "err": res.get("err", "")}


@job("close")
def _close(as_of: datetime, tg: TimeoutGuard) -> dict:
    """收盘快照 + 当日信号命中判定 → 发信（15:05）。

    ★ 与 `midday` 的差别见 `digest.build_close_context` 的 docstring：
      收盘用**当日完整数据**，午间只能用上午的。
      若两者共用装配函数，午间邮件会显示全天的板块涨幅。

    ★ 发信前先落一次板块强度（收盘后当天数据才完整）——
      否则邮件里的板块排名是上午的快照。
    """
    from backend.domain import digest
    from backend.domain import mail_templates as mt

    ctx_as_of = as_of.replace(hour=15, minute=5, second=0, microsecond=0)
    ctx = digest.build_close_context(ctx_as_of)
    tg.check()

    # ★ 收盘快照：采集当日板块强度（失败不阻断发信）
    board_ok, board_msg = False, ""
    try:
        import subprocess

        r = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "sync_board_strength.py")],
            capture_output=True, text=True, timeout=90, cwd=str(_ROOT),
        )
        board_ok = r.returncode == 0
        board_msg = (r.stdout or "")[-160:]
    except Exception as exc:
        board_msg = f"{type(exc).__name__}: {exc}"
    tg.check()

    log_event("close", {"positions": len(ctx.positions),
                        "board_ok": board_ok},
              ctx_as_of.isoformat(timespec="seconds"))
    if not getattr(mt.MailSender().cfg, "to", None):
        return {"sent": False, "reason": "mail.to 未配置，跳过发送",
                "board_ok": board_ok}
    res = mt.send(ctx, retry=1)
    tg.check()
    return {"sent": bool(res.get("ok")), "kind": ctx.kind,
            "board_ok": board_ok, "board_msg": board_msg,
            "skipped": bool(res.get("skipped")), "err": res.get("err", "")}


@job("auction")
def _auction(as_of: datetime, tg: TimeoutGuard) -> dict:
    """集合竞价监控（09:15–09:25，每分钟一次）。

    ★★ 本任务**拒绝补造历史**——这是它与其它任务最不一样的地方。
      竞价盘口只在 09:15–09:25 真实存在，过期后接口返回的是
      正常盘口，拿它当竞价数据是**最危险的静默错误**：
      看起来有数、实际是错的 ⇒ 用户会据此判断开盘强弱。
      故 `as_of` 不在窗口内时直接返回 skipped（ok=True），
      **不报错**（否则 catch_up 会每分钟反复补跑）。
    """
    from datetime import time as daytime

    from backend.domain import auction as auc
    from backend.services.tracking import tracked_codes

    # ---- ① 窗口守卫（不抛异常，见上方docstring） ----
    why = auc.reject_reason(as_of)
    if why:
        return {"skipped": True, "reason": why, "window": "outside"}

    clock = as_of.astimezone(CST).time().replace(tzinfo=None)
    if clock >= auc.OPEN_AUCTION_END or clock < auc.OPEN_AUCTION_START:
        return {"skipped": True, "reason": "outside_window", "window": "outside"}

    # ---- ② 取跟踪池（不指定全市场：竞价瞬时波动，只有池内才有意义）----
    # ★ `get_conn` 是**模块级已导入**的 sqlite 连接（见文件头）。
    #   不要另造名字——调度器已在别处用它写日志，复用同一连接更安全。
    con = get_conn()
    codes = tracked_codes(con, as_of, limit=60)
    if not codes:
        return {"skipped": True, "reason": "跟踪池为空", "window": "early"}
    tg.check()

    # ---- ③ 拉竞价盘口 ----
    from backend.data.adapter import bootstrap, get_adapter
    bootstrap()
    books = get_adapter("tencent").fetch_auction_book(codes, as_of)
    tg.check()
    if not books:
        raise RuntimeError("竞价盘口返回空（源异常或该时段无数据）")

    # ---- ④ 历史均量（算量比用）----
    #★ 用**前 5 个交易日**的日成交量 ÷ 240 分钟，
    #   得「每分钟均量」，再与「竞价累计量」比。
    #   注意单位：日线 volume 是**股**，盘口挂单量是**手** ⇒ 需 ×100 对齐。
    avgs = _avg_minute_volume(codes)
    tg.check()

    verdicts = []
    for b in books:
        code = b["code"]
        # 竞价「成交量」用买一/卖一档挂单量之和的较大者近似
        #（竞价阶段未成交单的意愿集中在一档）
        bk = b["book"]
        auction_vol = max(bk.bid_amount, bk.ask_amount) * 100  # 手 → 股
        verdicts.append(auc.judge(
            code=code, name=b["name"], as_of=as_of,
            auction_price=b["price"], pre_close=b["pre_close"],
            auction_vol=auction_vol, avg_5d_vol=avgs.get(code),
            book=bk))

    strong = [v for v in verdicts if v.strength in ("强买", "强卖")]
    window = verdicts[0].window if verdicts else "early"

    log_event("auction",
              {"codes": len(verdicts), "strong": len(strong),
               "window": window, "noisy": verdicts[0].noisy if verdicts else True},
              as_of.isoformat(timespec="seconds"))

    # ---- ⑤ 只在「强买/强卖」时发信（D8：不做整点播报，仅异常推送）----
    if not strong:
        return {"codes": len(verdicts), "strong": 0, "window": window,
                "notified": False, "reason": "无强信号，按 D8 仅异常推送"}

    from backend.common.mailer import MailSender

    sender = MailSender()
    if not getattr(sender.cfg, "to", None):
        return {"codes": len(verdicts), "strong": len(strong),
                "window": window, "notified": False,
                "reason": "mail.to 未配置"}

    stamp = as_of.strftime("%H:%M")
    res = sender.send(
        subject=f"Hawk 集合竞价 {stamp} — {len(strong)} 只强信号",
        html_body=auc.render_html(verdicts),
        text_body=auc.render_text(verdicts),
        as_of=as_of, kind="auction", retry=1, urgent=True)
    tg.check()
    return {"codes": len(verdicts), "strong": len(strong), "window": window,
            "noisy": verdicts[0].noisy, "notified": bool(res.get("ok")),
            "err": res.get("err", "")}


def _avg_minute_volume(codes: list[str], days: int = 5) -> dict[str, float]:
    """每只票的「历史每分钟均量」（股/分钟），供竞价量比用。

    ★ 必须按**交易日**取最近 N 天，不能用自然日 ——
      用自然日会把周末算成「零成交日」从而拉低均值、虚高量比。
    """
    from backend.data import duck

    out: dict[str, float] = {}
    try:
        con = duck.get_conn()
        for c in codes:
            rows = con.execute(
                "SELECT volume FROM kline_daily WHERE code = ?"
                " ORDER BY trade_date DESC LIMIT ?", [c, days]).fetchall()
            vols = [float(r[0]) for r in rows
                    if r[0] is not None and float(r[0]) > 0]
            if not vols:
                #★ 缺失就**不给键** ⇒ auction_ratio 返回 None
                #   而不是用 0或 1.0 假装有历史均量。
                continue
            out[c] = sum(vols) / len(vols) / 240.0
    except Exception:
        # ★ 拿不到就整体降级为「无均量」，调用方得到 None 量比。
        return {}
    return out


@job("ref_sync")
def _ref_sync(as_of: datetime, tg: TimeoutGuard) -> dict:
    """20:00 参考数据同步（规格 6.1）：股票列表 / 板块 / 财务。

    ★★ 本任务的设计核心：**分项独立降级，绝不整段失败**。
      三项数据源的健康度差别很大（股票列表可、板块成分被 Nginx 403 拦死），
      若「一项失败就整段failed」：
        · 任务失败 ⇒ `catch_up` 会把它当漏跑任务**反复补跑**（每次 600s）
        · 且失败的往往是**本来就拿不到**的那项 ⇒ 永远失败、永远重试
      ⇒ 必须每项单独 try，成功/失败都记进detail。

    ★ 各子步骤复用已有脚本/适配器，**不在任务里另写一套采集逻辑** ——
      两套口径必然对不上，而参考数据是全系统的地基。
    """
    from backend.data import duck
    from backend.data.adapter import bootstrap, get_adapter
    from backend.data.sqlite import get_conn as get_sqlite, upsert_stocks

    bootstrap()
    duck.init_db()
    detail: dict[str, Any] = {}

    # ---------- ① 股票列表（新浪，实测可用） ----------
    try:
        sn = get_adapter("sina")
        rows = sn.fetch_stock_list(as_of, max_pages=70)
        valid = [r for r in rows if r.get("id") and len(str(r["id"])) == 6
                 and str(r["id"]).isdigit() and r.get("market")]
        if not valid:
            raise RuntimeError("股票列表返回空")
        detail["stock_list"] = upsert_stocks(valid)
        duck.set_watermark("ref_stock_list",
                           as_of.isoformat(timespec="seconds"),
                           note=f"count={detail['stock_list']}")
    except Exception as exc:
        detail["stock_list_error"] = f"{type(exc).__name__}: {exc}"
    tg.check()

    # ---------- ② 板块列表（同花顺，实测 industry 可用 / gn 常403） ----------
    # ★★ 必须**逐项记录成功数**，不能「任一项失败就整项作废」：
    #   实测同花顺 `thshy`(行业) 可用而 `gn`(概念) 被 403，
    #   若整项抛错，已取到的 50 个行业板块会被**全部丢弃**。
    #   部分可用 > 整项丢失，且必须把「不完整」显式写进 detail。
    got: dict[str, int] = {}
    board_errs: list[str] = []
    try:
        ths = get_adapter("ths_board")
        for kind in ("industry", "concept"):
            try:
                bs = ths.fetch_board_list(kind, as_of)
                if not bs:
                    raise RuntimeError(f"{kind} 返回空")
                got[kind] = len(bs)
            except Exception as exc:
                board_errs.append(f"{kind}: {type(exc).__name__}: {exc}")
            tg.check()
    except Exception as exc:
        # 适配器本身取不到（如注册失败）
        board_errs.append(f"adapter: {type(exc).__name__}: {exc}")

    if got:
        detail["board_list"] = sum(got.values())
        detail["board_list_by_kind"] = got
        duck.set_watermark("ref_board_list",
                           as_of.isoformat(timespec="seconds"),
                           note=f"count={sum(got.values())}")
        if board_errs:
            # ★ 有成功也有失败 ⇒ 记为 partial而非 error，
            #   且必须带上失败原因，否则「50 个板块」会被当成全量。
            detail["board_list_partial"] = True
            detail["board_list_error"] = " | ".join(board_errs)[:300]
    else:
        detail["board_list_error"] = " | ".join(board_errs)[:300] or "无数据"
    tg.check()

    # ---------- ③ 板块成分股（★ 实测被 Nginx IP 限制 403） ----------
    #   这里**不静默吞掉**：既标 `unsupported` 又把原因写进detail，
    #   因为「拿不到」与「不需要」对下游是完全不同的信号。
    detail["board_members"] = "unsupported"
    detail["board_members_reason"] = (
        "同花顺成分股 AJAX 被 Nginx 层 IP 限制（403），"
        "换网络环境前不可得；板块强度已降级为不含成分覆盖的 7 因子")

    # ---------- ④ 财务数据 ---------- #
    #★ 显式标记「尚未接入」而不是不做：空着不说，
    #   下游会以为「查了没有」而不是「没查」。
    detail["financials"] = "not_implemented"
    detail["financials_reason"] = (
        "规格附录 A 未指定财务数据源；当前无适配器。"
        "依赖财务数据的因子应按缺失处理，不得默认 0")

    # ---------- 存量核对（有诊断价值） ----------
    try:
        con = get_sqlite()
        detail["stock_total"] = con.execute(
            "SELECT count(*) FROM stock").fetchone()[0]
        detail["st_count"] = con.execute(
            "SELECT count(*) FROM stock WHERE is_st=1").fetchone()[0]
        detail["suspended"] = con.execute(
            "SELECT count(*) FROM stock WHERE is_suspend=1").fetchone()[0]
    except Exception as exc:
        detail["verify_error"] = f"{type(exc).__name__}: {exc}"

    # ★ 只有「全部可获取的子项都失败」才算任务失败。
    #   `stock_list` 与 `board_list` 两项**都**失败 ⇒ 基本可断定是网络/源故障。
    #   只坏一项时任务仍返回 ok=True，因为另一项的数据仍然有效——
    #   这正是「分项降级」的意义。
    core = ("stock_list", "board_list")
    core_failed = [k for k in core if detail.get(f"{k}_error")]
    log_event("ref_sync", detail, as_of.isoformat(timespec="seconds"))
    if len(core_failed) == len(core):
        raise RuntimeError(
            f"参考数据同步全部失败（{'/'.join(core)}）："
            + "; ".join(str(detail[f'{k}_error'])[:120] for k in core))
    return detail


@job("weekly")
def _weekly(as_of: datetime, tg: TimeoutGuard) -> dict:
    """周六批量回测 + 周报（规格 6.1 周六 10:00）。

    ★★ 本任务的核心立场：**必须报「不稳定」，不能只报好看的数字**。
      已踩过的坑：30/60 只时样本外 +0.51%「通过」，200 只 -0.15%
      「不通过」，400 只 +0.44% 又「通过」——
      收益在 0 附近来回穿越，只报一档等于在 7 个数字里挑好看的。

    ★ 回测**复用 `run_oos.stability_check`**（多档样本量）而不是单次跑：
      口径与人工验证脚本完全一致，避免周报与命令行结论打架。
    """
    from backend.domain import weekly as wk

    ctx = wk.build_weekly_ctx(as_of)
    tg.check()

    # ---- 回测（失败不阻断周报，但要显式记录）----
    stability: dict = {"verdict": "no_data", "rows": [], "error": None}
    try:
        import subprocess

        # ★ 用与人工验证完全相同的脚本与参数口径。
        #   绝不在这里另写一套回测逻辑 —— 两套口径必然对不上，
        #   而周报是用户做实盘决策的依据，不能有「另一个数字」。
        cmd = [sys.executable, str(_ROOT / "scripts" / "run_oos.py"),
               "--stability", "--limit", "400"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=max(tg.seconds - 60, 300), cwd=str(_ROOT))
        out_path = _ROOT / "data" / "oos_stability.json"
        if r.returncode == 0 and out_path.exists():
            import json as _j
            stability = _j.loads(out_path.read_text("utf-8"))
        else:
            stability["error"] = ((r.stdout or "") + (r.stderr or ""))[-300:]
    except Exception as exc:
        stability["error"] = f"{type(exc).__name__}: {exc}"
    tg.check()

    #★ 由多档结果重新判定 verdict —— run_oos 的 stability_check 已判过，
    #   这里**再判一次**是为了保证周报措辞与 weekly.verdict_lines 同源。
    if stability.get("rows"):
        v = wk.stability_verdict(stability["rows"])
        stability["verdict"] = v["verdict"]
        stability["note"] = v["note"]
        stability["n_pass"] = v["n_pass"]
        stability["n_total"] = v["n_total"]

    report_path = wk.save_weekly_report(ctx, stability)
    tg.check()

    title, note = wk.verdict_lines(stability.get("verdict", "no_data"))
    log_event("weekly", {"verdict": stability.get("verdict"),
                         "signals": len(ctx["signals"]),
                         "report": report_path},
              as_of.isoformat(timespec="seconds"))

    # ---- 发信（缺收件人属合法状态，不抛异常）----
    from backend.common.mailer import MailSender

    sender = MailSender()
    if not getattr(sender.cfg, "to", None):
        return {"sent": False, "reason": "mail.to 未配置",
                "verdict": stability.get("verdict"), "report": report_path}

    subj = (f"Hawk 周报 {ctx['week_start']}~{ctx['week_end']} — {title}")
    # ★ 用 MailSender.send 而非自造函数：它负责写 mail_log 与免打扰判定。
    # ★ retry=1：默认 retry=3× 延迟 30/120/300 = 450s > 本任务 timeout 1800s
    #   不至于超时，但周报是**非实时**任务，多等 7 分钟毫无意义。
    res = sender.send(
        subject=subj,
        html_body=wk.render_weekly_html(ctx, stability),
        text_body=wk.render_weekly_text(ctx, stability),
        as_of=as_of, kind="weekly", retry=1)
    tg.check()
    return {"sent": bool(res.get("ok")), "verdict": stability.get("verdict"),
            "n_pass": stability.get("n_pass"), "n_total": stability.get("n_total"),
            "report": report_path, "bt_error": stability.get("error"),
            "skipped": bool(res.get("skipped")), "err": res.get("err", "")}


# ---------------------------------------------------------------- CLI
def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Hawk 定时任务调度")
    ap.add_argument("--run", help="手工执行某个任务（不查交易日）")
    ap.add_argument("--list", action="store_true", help="列出全部任务")
    ap.add_argument("--catch-up", action="store_true", help="补齐错过的任务")
    ap.add_argument("--date", help="as_of 日期")
    args = ap.parse_args()

    as_of = (
        datetime.fromisoformat(args.date).replace(tzinfo=CST)
        if args.date
        else datetime.now(CST)
    )

    if args.list:
        print("=" * 84)
        print(f"{'任务':16s}{'cron':22s}{'交易日':>7s}{'超时':>6s}  说明")
        print("=" * 84)
        for n, cron, td, to, desc in TASK_TABLE:
            print(f"{n:16s}{cron:22s}{'是' if td else '否':>7s}{to:>5d}s  {desc}")
        print("=" * 84)
        print(f"共 {len(TASK_TABLE)} 个任务，已注册实现 {len(REGISTRY)} 个")
        return 0

    if args.catch_up:
        results = catch_up(as_of)
        print(f"补跑 {len(results)} 个任务:")
        for r in results:
            print(f"  {r.name:16s} ok={r.ok} {r.elapsed_ms}ms {r.error[:60]}")
        return 0

    if args.run:
        j = REGISTRY.get(args.run)
        if j is None:
            cand = [t[0] for t in TASK_TABLE]
            print(f"未注册实现的任务。可用（已实现）：{list(REGISTRY)}")
            print(f"全部任务：{cand}")
            return 1
        r = run_job(j, as_of, force=True)
        print(
            json.dumps(
                {
                    "name": r.name,
                    "ok": r.ok,
                    "skipped": r.skipped,
                    "elapsed_ms": r.elapsed_ms,
                    "detail": r.detail,
                    "error": r.error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if r.ok else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
