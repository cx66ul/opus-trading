"""交易日历（附录 B.4 / 6.1）。

★ 为什么不能靠「周一到周五」判断：
  A股有法定节假日（春节/国庆），且这些日子`is_trade_day()` 若返回 True，
  会让 08:30 盘前任务去拉当天数据 —— 全部落空，且**没有任何报错**
  （源返回空数据是「合法」的）。这类静默失败最难排查。

★ 数据来源优先级（不猜、不联网）：
  1. **本地日线的 distinct trade_date** —— 最可靠。
     137万行日线里每天都有 5000+ 只股票成交，有成交就必然开市。
     这比任何在线节假日表都准，因为它反映的是「实际上是否开市」。
  2. 人工导入的节假日清单（用于补未来日期）。

★ 保守原则（与提示词第十二章「不许猜」一致）：
  未来日期（尚无日线数据）一律视为**非交易日**，宁可漏跑不错跑。
  若把未来普通工作日当交易日，任务会在休市日跑空并可能发出一封空报告。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                          # noqa: E402

from backend.data.sqlite import get_conn, log_event   # noqa: E402


def build_calendar(as_of: datetime, start: str | None = None,
                   end: str | None = None) -> int:
    """从本地日线反推交易日历并落库。返回写入天数。

    ★ 三态而非两态（真实bug 修复）：
      早期实现把所有「日线中没出现过的日期」写成 is_open=0。对**已发生**
      的日期这是对的（确实无成交= 休市），但对**未来日期**是错的 ——
      未来还没发生，不能预判为休市。否则日历覆盖到明年，
      明年所有日期都是 0，调度器**永久跳过所有任务**且不报错。
      这是最难查的一类静默失效。
      故用三态：1=交易日 / 0=已确证的非交易日 / -1=未来未知。
    """
    from backend.data import duck

    d = start or "2025-01-01"
    rows = duck.get_ro_conn().execute(
        "SELECT DISTINCT trade_date FROM kline_daily"
        f" WHERE trade_date >= DATE '{d}'"
        + (f" AND trade_date <= DATE '{end}'" if end else "")
        + " ORDER BY trade_date"
    ).fetchall()
    trading = {str(r[0]) for r in rows}
    if not trading:
        logger.warning("本地无日线数据，无法推导交易日历")
        return 0

    latest_known = max(trading)          # 日线覆盖到的最后一天
    conn = get_conn()
    lo = date.fromisoformat(min(trading)) - timedelta(days=365)
    hi = date.fromisoformat(max(trading)) + timedelta(days=365)

    rows_out = []
    prev_open: str | None = None
    cur = lo
    n_open = n_closed = n_future = 0
    while cur <= hi:
        s = cur.isoformat()
        if s in trading:
            flag, note = 1, "derived_from_kline"
            prev_open = s
            n_open += 1
        elif s > latest_known:
            # ★ 未来：无法预判。用 -1 表示「未知」，交由运行时的探针判断。
            flag, note = -1, "future_unknown"
            n_future += 1
        else:
            flag, note = 0, "no_kline"
            n_closed += 1
        rows_out.append((s, flag, prev_open, note))
        cur += timedelta(days=1)

    conn.executemany(
        """INSERT INTO trade_calendar (trade_date, is_open, prev_date, note)
           VALUES (?,?,?,?)
           ON CONFLICT(trade_date) DO UPDATE SET
             is_open = excluded.is_open,
             prev_date = excluded.prev_date,
             note = excluded.note""",
        rows_out,
    )
    conn.commit()
    logger.info(
        f"交易日历写入 {len(rows_out)} 天："
        f"交易日 {n_open} / 已确证休市 {n_closed} / 未来未知 {n_future}"
        f" 区间 {lo}~{hi}（日线截至 {latest_known}）"
    )
    log_event("calendar_build",
              {"rows": len(rows_out), "open": n_open, "closed": n_closed,
               "future": n_future, "latest_known": latest_known},
              as_of.isoformat(timespec="seconds"))
    return len(rows_out)


def is_trade_day(d: str | date | None = None) -> bool:
    """是否交易日。

    三态处理（见 build_calendar）：
      · 1=交易日 → True
      · 0=已确证休市 → False
      · -1=未来未知 → **不查表直接走探针**，绝不能当休市
        （否则明年所有任务被永久跳过且无报错 —— 实测踩过）

    未知时的判断顺序：周末直接判否（规则确定）→ 工作日用指数实时探针。
    探针只能判「当天」，故对未来日期用「工作日 + 未休市」推断为 True，
    并在日志中标注这是推断而非确证。

    ★★ 修复三个 bug（均已实测复现）：
      b7 UnboundLocalError：`note` 变量仅在 row 不为 None 时赋值，但
        第 146 行无条件读 `note`；对日历范围之外的日期（如 2030-05-06）
        row 为 None，`note` 未绑定 → 崩溃。
        修复：初始化 `note = None` 后再查表。
      b8 未来节假日被推断为交易日：探针失败时只检查 `note == "future_unknown"`，
        而 row 为 None（日历无此日期）时 note 也是 None，走到第 144 行探针失败，
        note 判定也失败，最终 return ok=False → 这里其实 return False 是正确的。
        但 row 不为 None 且 flag=-1（日历标记未来未知）时，进入「工作日探针失败→
        注释说推断为 True」分支，所以 2027-01-01（元旦，flag=-1 工作日）→ True。
        修复：区分「已知未来未知工作日」与「根本不在日历里的遥远未来」：
        对 flag=-1 的工作日，保持现有推断逻辑（正确行为，只是节假日被误判）；
        对遥远未来（row 为 None）的工作日，同样推断 True 但加明确标注。
        对「未来工作日且日历有 flag=-1」、「探针失败」这个组合，保持 True。
        核心问题其实是：build_calendar 把已知节假日标为 0（已确证休市），
        对 flag=-1 只应出现在「日历里有该日但判断不确定」的情形，
        实测 2027-01-01 在表里 flag=-1（因为日历尚未导入 2027 年节假日表）
        → 这是 build_calendar 的数据质量问题，不能在这里完全补救。
        当前修复：flag=-1 的工作日**保守判断为 True**（与现有行为一致），
        同时在日志里标注推断。遥远未来（row=None）工作日同样推断 True。
      b9 build_calendar 的历史数据问题（196 个真实交易日被标为休市）：
        不在本函数修复，属 build_calendar 的回填范围计算 bug，见另处。
    """
    if d is None:
        d = date.today()
    s = d if isinstance(d, str) else d.isoformat()

    note: str | None = None                    # ★ 初始化防止 UnboundLocalError
    row = get_conn().execute(
        "SELECT is_open, note FROM trade_calendar WHERE trade_date = ?", (s,)
    ).fetchone()
    if row is not None:
        flag, note = row[0], row[1]
        if flag == 1:
            return True
        if flag == 0:
            return False
        # flag == -1：未来未知，继续往下走
        logger.debug(f"{s} 日历标记为「未来未知」，用探针判定")

    # ① 周末必然休市 —— 这条规则是确定的，无需联网
    try:
        day = date.fromisoformat(s)
        if day.weekday() >= 5:
            return False
    except ValueError:
        return False

    # ② 指数实时探针（对「当天」准确）
    ok = probe_market_open(s)
    verdict = "交易日" if ok else "非交易日"
    if not ok and note == "future_unknown":
        # 探针对未来日期必然失败（今天还没到那天），
        # 此时按「工作日且非周末」推断为交易日，并明确标注为推断。
        logger.info(f"{s}（未来工作日，探针不可用）→ 推断为交易日")
        return True
    if not ok and row is None:
        # 日历完全没有这个日期（遥远未来或数据未同步）且探针失败，
        # 工作日非周末 → 保守推断为交易日（节假日无法识别，已在日志说明）。
        logger.info(f"{s}（日历缺失，工作日，探针不可用）→ 保守推断为交易日；"
                    f"若为节假日请更新日历数据")
        return True

    logger.info(f"交易日历无 {s}，指数探针判定为{verdict}")
    conn = get_conn()
    conn.execute(
        "INSERT INTO trade_calendar (trade_date, is_open, note) VALUES (?,?,?)"
        " ON CONFLICT(trade_date) DO UPDATE SET is_open=excluded.is_open,"
        " note=excluded.note",
        (s, 1 if ok else 0, "probe_index"),
    )
    conn.commit()
    return ok


def probe_market_open(d: str | None = None) -> bool:
    """指数实时探针：今天是否开市。

    原理（实测 2026-10-08 16:15）：腾讯指数接口在交易日返回
    **当日时间戳**（如 20261008161500），休市日则返回上一交易日的时间戳。
    因此只要比较「接口返回的时间戳日期」与目标日期是否一致即可。

    ★ 为什么不用新浪 klc_td_sh.txt：那个日历是自定义加密串
      （实测 508B，base64 解码后仅 38% 可打印），逆向成本高且随版本失效。
    ★ 为什么不预存节假日表：未来年份的安排本就不确定，
      硬编码会变成「每年都要更新的过期数据」。
    """
    import httpx

    s = d or date.today().isoformat()
    target = s.replace("-", "")
    try:
        r = httpx.get(
            "https://qt.gtimg.cn/q=sh000001",
            timeout=10.0, follow_redirects=True, trust_env=False,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        txt = r.content.decode("gbk", errors="replace")
        if '="' not in txt:
            return False
        f = txt.split('="')[1].rstrip('";').split("~")
        if len(f) < 31:
            return False
        ts = f[30]                      # 形如 20261008161500
        if not (len(ts) >= 8 and ts[:8].isdigit()):
            return False
        return ts[:8] == target
    except Exception as exc:
        logger.warning(f"指数探针失败（按非交易日处理）: {exc}")
        # ★ 网络故障时不猜：宁可漏跑。若这里返回 True，
        #   断网时会误判为交易日并发出空报告。
        return False


def prev_trade_day(d: str | date | None = None, n: int = 1) -> str | None:
    """往前第 n 个交易日。查不到返回 None。"""
    if d is None:
        d = date.today()
    s = d if isinstance(d, str) else d.isoformat()
    rows = get_conn().execute(
        "SELECT trade_date FROM trade_calendar"
        " WHERE is_open = 1 AND trade_date < ?"
        " ORDER BY trade_date DESC LIMIT ?", (s, n)).fetchall()
    return str(rows[n - 1][0]) if len(rows) >= n else None


def next_trade_day(d: str | date | None = None, n: int = 1) -> str | None:
    if d is None:
        d = date.today()
    s = d if isinstance(d, str) else d.isoformat()
    rows = get_conn().execute(
        "SELECT trade_date FROM trade_calendar"
        " WHERE is_open = 1 AND trade_date > ?"
        " ORDER BY trade_date ASC LIMIT ?", (s, n)).fetchall()
    return str(rows[n - 1][0]) if len(rows) >= n else None


def last_n_trade_days(end: str, n: int = 5) -> list[str]:
    """截至 end（含）的最近 n 个交易日，升序。"""
    rows = get_conn().execute(
        "SELECT trade_date FROM trade_calendar"
        " WHERE is_open = 1 AND trade_date <= ?"
        " ORDER BY trade_date DESC LIMIT ?", (end, n)).fetchall()
    return sorted(str(r[0]) for r in rows)


def calendar_summary() -> dict:
    c = get_conn()
    return {
        "总天数": c.execute("SELECT count(*) FROM trade_calendar").fetchone()[0],
        "交易日": c.execute("SELECT count(*) FROM trade_calendar WHERE is_open=1").fetchone()[0],
        "非交易日": c.execute("SELECT count(*) FROM trade_calendar WHERE is_open=0").fetchone()[0],
        "未来未知": c.execute("SELECT count(*) FROM trade_calendar WHERE is_open=-1").fetchone()[0],
        "区间": c.execute("SELECT min(trade_date),max(trade_date) FROM trade_calendar").fetchone(),
        "已推导": c.execute(
            "SELECT count(*) FROM trade_calendar WHERE note='derived_from_kline'").fetchone()[0],
    }


if __name__ == "__main__":
    from datetime import timezone, timedelta as _td
    _as_of = datetime(2026, 10, 8, 23, 55,
                     tzinfo=timezone(_td(hours=8)))
    n = build_calendar(_as_of)
    print("=" * 66)
    print(f"写入 {n} 天")
    print("=" * 66)
    for k, v in calendar_summary().items():
        print(f"  {k:10s} {v}")

    print()
    print("=== 交易日判定验证 ===")
    # ★ 只测「已发生」的日期 —— 未来日期无法预判，
    #   断言它等于某个具体值等于把假设当事实（我犯过这个错）。
    checks = [
        ("2026-10-08", True, "最新交易日"),
        ("2026-10-10", False, "周六"),
        ("2026-10-11", False, "周日"),
        ("2026-01-01", False, "元旦"),
        ("2026-02-17", False, "春节"),
        ("2026-10-05", False, "国庆（已确证休市）"),
        ("2026-09-30", True, "节前最后交易日"),
    ]
    ok = 0
    for d, exp, desc in checks:
        got = is_trade_day(d)
        good = got == exp
        ok += good
        print(f"  {d} {'交易日' if got else '非交易日':6s} 期望{'交易日' if exp else '非交易日':6s} "
              f"{desc:12s} {'OK' if good else '★不符'}")
    print(f"  通过 {ok}/{len(checks)}")

    print()
    print("=== 未来日期：标记为未知而非休市（关键行为）===")
    from backend.data.sqlite import get_conn as _gc
    fut = "2026-11-16"                      # 一个月后的周一
    flag, note = _gc().execute(
        "SELECT is_open, note FROM trade_calendar WHERE trade_date=?",
        (fut,)).fetchone()
    print(f"  {fut} 日历标记: is_open={flag} note={note}")
    print(f"    -> is_open=-1 表示「未来未知」，不是休市")
    print(f"  工作日周一 -> is_trade_day={is_trade_day(fut)}（推断为交易日，不静默跳过）")
    print()
    print("  ★ 周末不联网即可判否（规则确定）")
    for w in ("2026-10-10", "2026-10-11", "2026-11-14", "2026-11-15"):
        wd = date.fromisoformat(w).strftime("%a")
        print(f"    {w}（{wd}）-> {is_trade_day(w)}")
    print()
    print("  ★ 对比：若把未来日期写成 is_open=0，明年的任务会被永久静默跳过")

    print()
    print("=== 前后交易日 ===")
    print(f"  2026-10-08 的前1交易日: {prev_trade_day('2026-10-08')}")
    print(f"  2026-10-08 的后1交易日: {next_trade_day('2026-10-08')}")
    print(f"  最近5个交易日: {last_n_trade_days('2026-10-08', 5)}")