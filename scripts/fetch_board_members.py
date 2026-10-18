#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 同花顺板块成分股抓取（M5 硬前置）

问题（实测 2026-10-08）：
    M5 板块强度必须知道「哪些股票属于哪个板块」，
    但 http://q.10jqka.com.cn/thshy/detail/.../ajax/1/code/<code>
    返回的是 JS 跳转页而非 JSON（裸连 401/404）。

方案：Playwright 打开板块详情页，等表格渲染完成后读 DOM。
    ★ 不用抓 XHR —— 同花顺的成分表是**服务端渲染进HTML** 的，
      浏览器拿到页面时表格已经在 DOM 里，直接读 table 即可，
      比拦截接口更稳（接口参数会变，DOM 结构相对稳定）。

实测数据源：
    http://q.10jqka.com.cn/thshy/detail/code/881121/   半导体
    http://q.10jqka.com.cn/gn/detail/code/308614/      概念

用法：
    python scripts/fetch_board_members.py --probe881121 # 先探测单板块
    python scripts/fetch_board_members.py --sample 5      # 试抓 5 个
    python scripts/fetch_board_members.py --all           # 全量（低频，约需 10 分钟）

⚠️ 合规（附录 A.3 第 7 条）：每板块仅 1 次页面加载，板块间随机 sleep 1~2 秒。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

from backend.domain.trade_cal import is_trade_day, prev_trade_day

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

SAMPLES = ROOT / "samples"


def _looks_blocked(page) -> bool:
    """判断当前页是否被反爬拦截（401 滑动验证码 / 403）。

    ★ 实测坑（2026-10-10）：同花顺对同一 IP 有**频率阈值**。
      连续抓 50 个板块页后，后续请求返回
      `status=401` + title=`滑动验证码demo`，DOM 里没有 `table`。
      早先脚本把它记成「仅 0 只（可能是结构变化）」——
      **归因完全错误**：不是同花顺改了页面结构，而是我们自己被限流了。
      两者必须区分，否则会去「修」一个根本没坏的解析器。
    """
    try:
        title = (page.title() or "").strip()
    except Exception:
        return False
    return ("验证码" in title) or ("验证" in title) or ("Forbidden" in title)


def probe_one(page, code: str, name: str, kind: str,
              max_pages: int = 15) -> tuple[list[dict], str]:
    """打开板块页，**点击分页**读取全部成分股。

    返回 (members, status)，status ∈ {"ok", "blocked", "empty"}。
    ★ channel="chromium" 必须：headless_shell 在本机报 spawn EFTYPE。
    """
    members: list[dict] = []
    base = "http://q.10jqka.com.cn/thshy/" if kind == "industry" else "http://q.10jqka.com.cn/gn/"
    seen: set[str] = set()

    try:
        resp = page.goto(f"{base}detail/code/{code}/", wait_until="domcontentloaded",
                         timeout=25000)
        status = resp.status if resp is not None else None
        if status is not None and status >= 400:
            return [], "blocked"
        page.wait_for_selector("table tbody tr", timeout=12000)
    except Exception as exc:
        #★ 先判是不是被拦了：被拦时 goto 不一定抛错，但表格永远不会出现
        if _looks_blocked(page):
            logger.warning(f"[{name}({code})] 被反爬拦截（IP 限流），整批应停止")
            return [], "blocked"
        logger.warning(f"[{name}({code})] 首页加载失败: "
                       f"{type(exc).__name__}: {str(exc)[:50]}")
        return [], "empty"

    def harvest() -> int:
        """抓当前页表格，返回新增条数。"""
        n = 0
        for r in page.query_selector_all("table tbody tr"):
            tds = r.query_selector_all("td")
            if len(tds) < 3:
                continue
            c = (tds[1].inner_text() or "").strip()
            nm = (tds[2].inner_text() or "").strip()
            if not (len(c) == 6 and c.isdigit()) or c in seen:
                continue
            seen.add(c)
            pct = _parse_pct(tds[4].inner_text()) if len(tds) >= 5 else None
            members.append({
                "code": c, "name": nm, "pct_chg": pct,
                "board_code": code, "board_name": name, "board_type": kind,
            })
            n += 1
        return n

    harvest()
    total_pages = _total_pages(page)
    if total_pages > 1:
        logger.debug(f"[{name}] 共 {total_pages} 页")

    for pno in range(2, min(total_pages, max_pages) + 1):
        try:
            # 点击分页按钮（AJAX）
            btn = page.query_selector(f'.changePage[page="{pno}"]')
            if btn is None:
                # 页面可能没渲染出该页按钮，改点「下一页」
                btn = page.query_selector("a.next")
                if btn is None:
                    break
            btn.click()
            page.wait_for_timeout(900)          # 等 AJAX 渲染
            page.wait_for_selector("table tbody tr", timeout=8000)
            if harvest() == 0:
                break
        except Exception as exc:
            logger.warning(f"[{name}] 第 {pno} 页失败: {type(exc).__name__}")
            break
    if not members:
        return [], "empty"
    return members, "ok"


def _total_pages(page) -> int:
    """从 .page_info（形如 '1/10'）读总页数。"""
    try:
        el = page.query_selector(".page_info")
        if el:
            txt = (el.inner_text() or "").strip()
            if "/" in txt:
                return int(txt.split("/")[-1].strip())
    except Exception:
        pass
    return 1


def _parse_pct(txt: str) -> float | None:
    """解析涨跌幅文本，如 '3.45%' / '-1.2%'。"""
    if not txt:
        return None
    t = txt.replace("%", "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description="同花顺板块成分股抓取")
    p.add_argument("--probe", default="", help="只探测单个板块代码，如 881121")
    p.add_argument("--sample", type=int, default=0, help="试抓前 N 个板块")
    p.add_argument("--all", action="store_true", help="全量抓取")
    p.add_argument("--min-members", type=int, default=3,
                   help="成分股少于该数视为无效（可能是页面结构变化）")
    p.add_argument("--sleep", type=float, default=1.5,
                   help="板块间基础间隔秒数（实测 IP 限流，建议 ≥3 重试）")
    a = p.parse_args()

    from playwright.sync_api import sync_playwright

    from backend.data.adapter import bootstrap, get_adapter
    from backend.data.sqlite import get_conn

    bootstrap()
    ths = get_adapter("ths_board")
    as_of = datetime.now(CST)

    #★★★ 采集日必须先归一到交易日（与 sync_board_strength 同一个坑）
    #   `board_member_snapshot.snap_date` 是**行情归属日**，
    #   非交易日跑这个脚本时同花顺返回的是上一交易日收盘的成分/行情，
    #   直接按`now.date()` 存就会造出库里不存在的日期。
    #   实测 2026-10-10（周六）采了 329 条 ⇒ 而它们属于 10-09，
    #   于是回填脚本 `WHERE snap_date <= '2026-10-09'` 取不到任何成分，
    #   **383 个板块全部拿不到行情**。
    #   ⇒ 这里 snap 到最近交易日；确定不了就拒跑（宁可无数据）。
    day0 = as_of.strftime("%Y-%m-%d")
    if not is_trade_day(day0):
        snapped = prev_trade_day(day0)
        if snapped is None or snapped == day0:
            logger.error(f"★ {day0} 非交易日且无法确定最近交易日，"
                         f"拒绝采集（不造假日期）")
            return 3
        logger.warning(f"★ {day0} 非交易日（接口返回的是最近交易日收盘数据）"
                       f"⇒ 快照日归一到 {snapped}")
        as_of = datetime.strptime(snapped, "%Y-%m-%d").replace(tzinfo=CST)

    # 取板块列表
    if a.probe:
        targets = [{"code": a.probe, "name": "探测", "type": "industry"}]
    else:
        # ★ 分项独立降级，绝不整段失败
        #   —— 同花顺 `gn`（概念板块）实测 403 Forbidden，`thshy`（行业）可用。
        #   早先写成 `industry_list + concept_list` 直接相加：
        #   概念这一段抛 SourceError ⇒ **整个脚本崩掉，行业也一并不采**。
        #   记忆里的「ref_sync 必须分项独立降级」是调度层的教训，
        #   这里必须同样落实，否则每加一个源就多一个「一票否决」点。
        boards, partial = [], []
        for kind in ("industry", "concept"):
            try:
                got = ths.fetch_board_list(kind, as_of)
                boards.extend(got)
                logger.info(f"板块列表 {kind}: {len(got)} 个")
            except Exception as exc:                    # noqa: BLE001
                partial.append({"kind": kind, "err": str(exc)[:200]})
                logger.warning(f"板块列表 {kind} 失败（已跳过，不影响另一类）：{exc}")
        if not boards:
            logger.error("两类板块列表都取不到，无法继续")
            return 2
        if partial:
            logger.warning(f"★ 部分板块类型降级：{[p['kind'] for p in partial]}")

        if a.sample:
            # ★ 随机取样，不能按主键顺序
            #   —— 行业+概念拼接后前 N 个全是行业板块，样本报「概念抓不动」是假的。
            #⚠ `random` 必须在**模块顶层**导入：早先放在这个分支里 import，
            #   Python 仍会把它标记为函数级局部变量 ⇒ 后面 main() 里
            #   `random.uniform(1.0, 2.0)` 报 UnboundLocalError。
            #   （import 语句的位置 ≠ 名字的作用域，这是 Python 的经典陷阱）
            random.seed(20261010)
            boards = random.sample(boards, min(a.sample, len(boards)))
        elif not a.all:
            boards = boards[:3]
        targets = [{"code": b["code"], "name": b["name"], "type": b["type"]}
                   for b in boards]
    logger.info(f"待抓取 {len(targets)} 个板块")

    out: list[dict] = []
    ok = bad = 0
    with sync_playwright() as pw:
        # ★ channel="chromium" 必须：headless_shell 在本机报 spawn EFTYPE
        browser = pw.chromium.launch(headless=True, channel="chromium")
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        blocked_at: str = ""
        for i, t in enumerate(targets, 1):
            mem, status = probe_one(page, t["code"], t["name"], t["type"])
            if status == "blocked":
                # ★ 命中反爬 ⇒ **立刻停止整批**。
                #   早先的写法是把它记成「仅 0 只（可能是结构变化）」然后
                #   接着抓下一个 —— 结果 50 个板块里 33 个「失败」，
                #   且每个都白等 12~25 秒，纯属把限流坐实成封禁。
                blocked_at = t["name"]
                logger.error(
                    f"[{i}/{len(targets)}] {t['name']}({t['code']}) 被反爬拦截，"
                    f"中止剩余 {len(targets) - i} 个板块")
                break
            if len(mem) >= a.min_members:
                ok += 1
                out.extend(mem)
                logger.info(f"[{i}/{len(targets)}] {t['name']}({t['code']}) -> {len(mem)} 只")
            else:
                bad += 1
                logger.warning(f"[{i}/{len(targets)}] {t['name']}({t['code']}) -> "
                               f"仅 {len(mem)} 只（页面结构可能变化）")
            # 合规：板块间随机间隔
            # ★ 实测：间隔 1~2 秒连续抓 50 个 ⇒ 约 17 个后开始 401 滑块验证。
            #   所以间隔必须可调，重试批次用更大间隔慢速补齐。
            if not a.probe and i < len(targets):
                time.sleep(random.uniform(a.sleep, a.sleep * 2))
        if a.probe:
            page.screenshot(path=str(SAMPLES / "ths_board_probe.png"), full_page=False)
        browser.close()

    # ★ 报实际达成数，**不是计划数**。
    #   日志上一版打「成功 17 / 失败 33 / 成分记录 329 条」，
    #   但同一批里成功板块数 17、每板块 14~20 只⇒ 理论 240~340 条，
    #   而库内实际只有 329 条 / 17 个板块 —— 中间差的是重复板块代码。
    #   打印 len(out) 是不去重的口径，会让人误以为覆盖更高。
    n_unique_mem = len({m["board_code"] for m in out})
    logger.info(f"完成：成功 {ok} 个板块，失败/异常 {bad} 个，"
                f"成分记录 {len(out)} 条")
    if blocked_at:
        logger.error(f"★ 本批被同花顺 IP 限流中断（首个被拦板块：{blocked_at}）——"
                     f"这不是页面结构变化，稍后需降频重试剩余板块")

    if not out:
        logger.error("未抓到任何成分股，先看samples/ths_board_probe.png 确认页面结构"
                     + ("（若为验证码页，则是 IP 限流，等 30~60 分钟再试）"
                        if blocked_at else ""))
        return 3 if blocked_at else 1

    # 落盘 + 落库
    (SAMPLES / "ths_members.json").write_text(
        json.dumps({"fetched_at": as_of.isoformat(timespec="seconds"),
                    "board_count": ok, "records": len(out), "data": out},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    # ★ board_member_snapshot 在 **DuckDB**（行情库），不在 SQLite（业务库）。
    #   写错库会报 "no such table" —— 实测踩过一次。
    from datetime import date as _date

    from backend.data import duck

    snap = _date(as_of.year, as_of.month, as_of.day).isoformat()
    con = duck.get_conn()
    # ★ 增量写入，**不做 "DELETE 当日全量再插"**。
    #   本脚本可能因IP 限流中断，只抓到部分板块；
    #   早先的 `DELETE WHERE snap_date=?` 会把上一次完整采到的板块**一起抹掉**，
    #   让快照随每次重试而**越来越空** —— 覆盖度只会单调下降。
    #   INSERT OR REPLACE 本身按主键去重，重复跑同一天是幂等的。
    before = con.execute(
        "SELECT count(DISTINCT board_code) FROM board_member_snapshot "
        "WHERE snap_date = ?", [snap]).fetchone()[0]
    con.executemany(
        "INSERT OR REPLACE INTO board_member_snapshot (snap_date, board_code, code, weight) "
        "VALUES (?,?,?,?)",
        [(snap, m["board_code"], m["code"], 1.0) for m in out],
    )
    n = con.execute("SELECT count(*) FROM board_member_snapshot").fetchone()[0]
    nb = con.execute(
        "SELECT count(DISTINCT board_code) FROM board_member_snapshot").fetchone()[0]
    nb_snap = con.execute(
        "SELECT count(DISTINCT board_code) FROM board_member_snapshot "
        "WHERE snap_date = ?", [snap]).fetchone()[0]
    logger.info(f"已落库 board_member_snapshot：全库 {n} 条记录 / {nb} 个板块；"
                f"本日 {snap} 共 {nb_snap} 个板块"
                + (f"（本次 +{nb_snap - before}）" if nb_snap - before else "（无新增）"))
    if blocked_at:
        logger.warning(f"★ 本日为**不完整快照**（限流中断，{len(targets) - ok - bad} 个板块未抓）"
                       f"⇒ 因子计算须标注 subset，不可当全市场板块口径使用")

    # 覆盖度检查：有多少股票能被归到至少一个板块 —— 这决定 M5 强度计算的可信度
    covered = con.execute(
        "SELECT count(DISTINCT code) FROM board_member_snapshot").fetchone()[0]
    total = get_conn().execute("SELECT count(*) FROM stock").fetchone()[0]
    logger.info(f"成分覆盖：{covered}/{total} 只股票（{covered / total:.1%}）")

    # ★ 覆盖率有两个独立的分母，缺一不可：
    #   ① 股票覆盖（covered/total）—— 有多少只股票能归到某个板块
    #   ② 板块内完整度 —— 每板块只有 14~20 只
    per = con.execute(
        "SELECT count(*) c FROM board_member_snapshot WHERE snap_date = ? "
        "GROUP BY board_code ORDER BY c", [snap]).fetchall()
    sizes = [r[0] for r in per]
    if sizes:
        logger.info(f"每板块成分数：min={min(sizes)} max={max(sizes)} "
                    f"中位={sorted(sizes)[len(sizes) // 2]}")
        if max(sizes) <= 25:
            # 同花顺翻页 AJAX 返回 401（实测）⇒ 只能拿到首页 20 只
            logger.warning(
                f"★ 每板块仅 ≤{max(sizes)} 只：同花顺成分股翻页 AJAX 被反爬拦截（401），"
                f"只能读 DOM 首页 ⇒ 这是**子集口径**，不是全量成分。"
                f"因子计算必须显式标注 subset，禁止当全量用")
    if covered / total < 0.5:
        logger.warning("★ 覆盖率偏低，板块强度代表性有限，M5 实现时需显式标注")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())