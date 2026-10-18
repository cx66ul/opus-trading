# -*- coding: utf-8 -*-
"""
Hawk · 板块数据源（同花顺）—— 东财 push2 不可达的替代方案

背景（D1 决策）：
    东财 `push2.eastmoney.com` / `push2his.eastmoney.com` 在本机网络不可达，
    板块列表、板块成分股、资金流三项能力原本依赖东财 clist。
    实测替代方案：**同花顺 q.10jqka.com.cn 可达**，能提供行业与概念板块列表。

★ 2026-10-09 重写：把 12 列全部解析出来
------------------------------------------------
此前本适配器只取了 3 列（code/name/序号），
而实测页面**本身就是一张完整的板块行情表**（表头实测）：

    序号 | 板块 | 涨跌幅(%) | 总成交量(万手) | 总成交额(亿元) | 净流入(亿元)
         | 上涨家数 | 下跌家数 | 均价 | 领涨股 | 最新价 | 涨跌幅(%)

⇒ 涨跌幅、成交额、**净流入**、涨跌家数、领涨股全部可得，
   正好填满 `board_strength` 表的 `strength / net_inflow / up_ratio / leader_code`。

★ 为什么「领涨股」那一列要连代码一起取
------------------------------------
`board_strength.leader_code` 需要的是**代码**不是名称，
而页面里领涨股是 `<a href="http://stockpage.10jqka.com.cn/920627/">力王股份</a>`
⇒ 代码就在 href 里，顺手取走（否则还得反查代码）。

★ 仍不可得的能力（实测确认，不要再试）
------------------------------------
  ✗ 板块**成分股** ajax → 401（chameleon 反爬 + Nginx IP 限制）
  ✗ 板块**历史 K 线** → `d.10jqka.com.cn/v6/line/hs_881281/01/last.js`404
  ⇒ `board_strength` **只能当日采集后积累**，无法回填历史。
  ⇒ `board_member_snapshot` 仍只能每板块拿首页 20 只。

实测结论（2026-10-08/ 09）：
  ✓ http://q.10jqka.com.cn/thshy/                200，行业板块列表可完整解析
  ✓ http://q.10jqka.com.cn/gn/                   （概念板块，同结构）
  ✗ /thshy/detail/.../ajax/1/code/<code>         401 JS 跳转页，非数据

字段映射见 config/fields/ths_board.yaml。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

# 支持 `python backend/data/adapters/ths_board.py` 直接运行
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.data.adapter import DEFAULT_UA, BaseAdapter
from backend.data.base import SourceError

# 板块列表页
THS_INDUSTRY_URL = "http://q.10jqka.com.cn/thshy/"
#★ 概念板块的**行情表**不在 `/gn/` —— 那里是「概念驱动事件新闻页」
#   （实测表头：日期/ 概念名称 / 驱动事件 / 龙头股 / 成分股数量）。
#   概念行情走同一个 index ajax 路径前缀。
THS_CONCEPT_URL = "http://q.10jqka.com.cn/gn/"

# 分页 ajax（实测 page/1 起有效，每页 40 个）
RE_AJAX = re.compile(
    r"http://q\.10jqka\.com\.cn/(?P<kind>thshy|gn)/index/field/\d+/order/desc/page/(?P<page>\d+)/ajax/1/"
)
# 每页条数（实测首屏 50，ajax 页 40）
THS_PAGE_SIZE = 50
THS_MAX_PAGES = 20                # 安全上限，防无限翻页

# ================================================================== 解析
# 板块代码与名称：`<a href=".../detail/code/881281/" target="_blank">电池</a>`
RE_BOARD_LINK = re.compile(
    r'<a href="http://q\.10jqka\.com\.cn/(?P<kind>thshy|gn)/detail/code/(?P<code>\d+)/"[^>]*>'
    r'(?P<name>[^<]+)</a>'
)

# ★ 整行解析：一次性吃掉 12 列，顺序与 `THS_COLUMNS` 严格对应。
#   早期版本用 `RE_BOARD_ROW` 只取前 5 个<td>，把后面 7 列全丢了
#   （净流入、涨跌家数、领涨股都没拿到，浪费了整张表）。
#
#   为什么不能用「逐列正则」：`c-rise` / `c-fall` 决定正负号与颜色，
#   而 <td> 数量在「有/无领涨股」时可能变化 ⇒ 按位置取更稳，
#   但必须**校验列数**，少列要显式报错而不是静默补None。
RE_ROW_BLOCK = re.compile(r"<tr[^>]*>(?P<body>.*?)</tr>", re.S)
RE_TD = re.compile(r"<td[^>]*>(?P<cell>.*?)</td>", re.S)
RE_TAGS = re.compile(r"<[^>]+>")

# 表头（实测顺序，用于文档与自检）
THS_COLUMNS = (
    "序号", "板块", "涨跌幅(%)", "总成交量(万手)", "总成交额(亿元)", "净流入(亿元)",
    "上涨家数", "下跌家数", "均价", "领涨股", "最新价", "涨跌幅(%)",
)
N_COLUMNS = len(THS_COLUMNS)

# 领涨股链接里带股票代码
RE_LEADER = re.compile(r"stockpage\.10jqka\.com\.cn/(\d+)/")

# ★ 概念板块（`/gn/`）走**另一条路径**：
#   `/gn/` 首页是「概念驱动事件**新闻**页」（表头：日期/概念名称/驱动事件/
#   龙头股/成分股数量），**没有行情表**。
#   行情藏在 ajax 返回的 `<input id="gnSection" value='{...}'>` 里 ——
#   一个 JSON，键是板块序号，值含 platecode/platename/涨跌幅/净流入/涨幅家数。
#   实测该 JSON 含**293 个**概念板块。
RE_GN_SECTION = re.compile(r"id=\"gnSection\" value='(?P<j>.*?)'\s*/?>", re.S)

# gnSection 里各字段的含义（键名是同花顺的内部 field id，无语义）
GN_FIELD = {
    "platecode": "板块代码",
    "platename": "板块名称",
    "199112": "涨跌幅(%)",
    "zjjlr": "净流入(亿元)",
    "zfl": "涨幅家数",
    "zdl": "跌幅家数",
}


def parse_concept_section(text: str) -> list[dict]:
    """解析概念板块的 `gnSection` JSON。

    ★ 为什么概念要单独一套解析：页面结构与行业板块**完全不同**
      （行业是 <table>，概念是 hidden input 里的 JSON）。
      若用同一个 `parse_board_rows`，会静默返回空列表 ——
      实测「共 0 个」就是这么来的，看起来像「当天没有概念板块」。

    ★ 注意 `199112` / `zjjlr` / `zfl` 是同花顺的**内部字段 id**，不是语义名。
      字段含义见 `GN_FIELD`，改动需同步（实测样本：
      {"platecode":"885333","platename":"移动支付","cid":"300188",
       "199112":-2.02,"zjjlr":-3.52,"zfl":13}
      ⇒ 涨跌幅 -2.02%、净流入 -3.52亿、涨13 家）
    """
    m = RE_GN_SECTION.search(text)
    if not m:
        return []
    raw = m.group("j").replace("&quot;", '"').replace("&amp;", "&")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for _key, v in (data or {}).items():
        if not isinstance(v, dict):
            continue
        code = str(v.get("platecode") or "").strip()
        name = str(v.get("platename") or "").strip()
        if not code or not name or code in seen:
            continue
        seen.add(code)
        pct = _f(v.get("199112"))
        inflow = _f(v.get("zjjlr"))
        up = _f(v.get("zfl"))
        # ★ gnSection **没有** `zdl`（跌幅家数）字段 —— 实测 293 个概念全是None。
        #   不能因为「下跌家数为 0」就算出「广度 100%」——
        #   那是把「无数据」当「全部上涨」，会让概念板块广度恒为满分。
        down = _f(v.get("zdl"))
        if down is None:
            # 概念板块成分数未知（详情页 ajax 401）⇒ 广度不可算
            ur = None
        else:
            ur = _up_ratio(up, down)
        out.append({
            "code": code,
            "name": name,
            "type": "concept",
            "board_code": f"THSCON{code}",
            "pct_chg": pct,
            "net_inflow_yi": inflow,
            "up_cnt": up,
            "down_cnt": down,
            "up_ratio": ur,
            "leader_code": None,     # gnSection 不含领涨股
            "leader_name": None,
        })
    return out


def _f(v) -> float | None:
    """JSON 值 → float。缺失/非数字返回 None（不当0）。"""
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cell_text(cell: str) -> str:
    """取 <td> 的纯文本（去标签、去 &nbsp;、去首尾空白）。"""
    t = RE_TAGS.sub("", cell)
    return t.replace("&nbsp;", " ").strip()


def _num(cell: str) -> float | None:
    """单元格 → 浮点数。**空/非数字返回 None**（不当0）。

    同花顺用 `--` 表示停牌/无数据（实测概念板块页出现），
    当 0 会被读成「净流入 0 元」这类假数据。
    """
    s = _cell_text(cell).replace(",", "").replace("%", "").strip()
    if not s or s in ("--", "-", "—", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_board_rows(text: str, kind: str = "industry") -> list[dict]:
    """从页面 HTML 解析板块行情表。

    返回每行一个 dict，字段见 `THS_COLUMNS` 对应的键。
    ★ 列数不足时**显式跳过并计数**，不静默产出半截数据 ——
      半截数据会被读成「涨停 0 家」这类错误结论。
    """
    out: list[dict] = []
    seen: set[str] = set()
    bad_cols = 0

    for m in RE_ROW_BLOCK.finditer(text):
        cells = RE_TD.findall(m.group("body"))
        cells = [c for c in cells if RE_TAGS.sub("", c).strip() != "" or len(cells) == N_COLUMNS]
        # 首列必须是纯数字序号
        if len(cells) < 4:
            continue
        seq = _num(cells[0])
        if seq is None or not _cell_text(cells[0]).isdigit():
            continue

        # 板块代码/名称从第二列的 <a> 里取
        lm = RE_BOARD_LINK.search(cells[1])
        if not lm:
            continue
        code = lm.group("code")
        name = lm.group("name").strip()
        if code in seen:
            continue

        if len(cells) < N_COLUMNS:
            bad_cols += 1
            continue
        seen.add(code)

        leader_cell = cells[9]
        leader_lm = RE_LEADER.search(leader_cell)
        rec = {
            "code": code,
            "name": name,
            "type": kind,
            "board_code": f"THS{kind[:3].upper()}{code}",
            "pct_chg": _num(cells[2]),          # 板块涨跌幅 %
            "volume_wan": _num(cells[3]),        # 总成交量（万手）
            "amount_yi": _num(cells[4]),         # 总成交额（亿元）
            "net_inflow_yi": _num(cells[5]),     # 净流入（亿元）★ D1 缺的那项
            "up_cnt": _num(cells[6]),            # 上涨家数
            "down_cnt": _num(cells[7]),          # 下跌家数
            "avg_price": _num(cells[8]),         # 均价
            "leader_name": _cell_text(leader_cell) or None,
            # ★ 领涨股代码在 href 里；取不到就是 None，不猜
            "leader_code": leader_lm.group(1) if leader_lm else None,
            "leader_price": _num(cells[10]),
            "leader_pct_chg": _num(cells[11]),
        }
        rec["up_ratio"] = _up_ratio(rec["up_cnt"], rec["down_cnt"])
        out.append(rec)

    if bad_cols:
        logger.warning(
            f"[ths] {kind} 有 {bad_cols} 行列数不足 {N_COLUMNS}，已跳过"
            f"（页面结构可能变了）")
    return out


def _up_ratio(up, down) -> float | None:
    """上涨占比 0~1。分母为 0 或数据缺失时返回 None（不当0）。

    「没有成分股」与「全部下跌」是两回事，返回 0 会被读成「全跌」。
    """
    if up is None or down is None:
        return None
    total = up + down
    if total <= 0:
        return None
    return up / total


class ThsBoardAdapter(BaseAdapter):
    name = "ths_board"
    qps = 2.0                     # 同花顺限流较严，保守取 2 QPS
    referer = "http://q.10jqka.com.cn/"
    encoding = "gbk"

    #★ ajax 分页必须带 XHR 头，否则 401（实测：不带 → 401/0 codes，
    #   带 → 200/40 codes）。这是 chameleon 反爬的第一道。
    #   `BaseAdapter` 的 client 头在构造时固定，故这里单开一个带 XHR 的请求。
    XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest",
                   "Referer": "http://q.10jqka.com.cn/"}

    def __init__(self, qps: float | None = None) -> None:
        super().__init__(qps)
        self._ajax_client = None

    def _ajax_page(self, kind: str, page: int, retries: int = 2) -> str:
        """取第 `page` 页的板块行情表 HTML（1 起），带重试。

        走独立 client：ajax 分页需要 `X-Requested-With`，
        而 `BaseAdapter.client` 的头是构造时固定的全局默认。

        ★失败抛 `SourceError`（而非 httpx 原生异常）——
          上层按 `SourceError` 决定是否切源/降级；抛 httpx 异常会绕过该机制。

        ★★ 401 是**偶发限流**而非「页不存在」（实测：page3 首调401、
          隔 1.5s 重试即200）⇒ 必须重试，否则会**静默少一个板块**：
          翻页循环把 401 当成「到最后一页」而break，
          结果 90 个板块只拿到 87 个，且日志只是 warning。
        """
        import httpx as _httpx

        url = (f"http://q.10jqka.com.cn/{kind}/index/field/199112/"
               f"order/desc/page/{page}/ajax/1/")
        last: Exception | None = None
        for attempt in range(retries + 1):
            self.bucket.acquire()
            t0 = datetime.now()
            try:
                if self._ajax_client is None:
                    self._ajax_client = _httpx.Client(
                        timeout=_httpx.Timeout(20.0, connect=10.0),
                        follow_redirects=True,
                        trust_env=False,
                        headers={**self.XHR_HEADERS, "User-Agent": DEFAULT_UA},
                    )
                r = self._ajax_client.get(url)
                r.raise_for_status()
            except Exception as exc:
                last = exc
                self.health.fail(f"{type(exc).__name__}: {exc}")
                if attempt < retries:
                    # 限流用**线性**退避即可（同花顺不是指数退避型）
                    time.sleep(1.2 * (attempt + 1))
                    continue
                self.backoff.next_delay()
                raise SourceError(
                    1003, f"同花顺 {kind} 第 {page} 页 ajax 失败"
                          f"（重试 {retries} 次）: {exc}",
                    retryable=True,
                ) from exc
            r.encoding = self.encoding
            self.health.ok(int((datetime.now() - t0).total_seconds() * 1000))
            return r.text
        raise SourceError(1003, f"同花顺 {kind} 第 {page} 页 ajax 失败: {last}",
                          retryable=True)

    def close(self) -> None:
        super().close()
        if getattr(self, "_ajax_client", None) is not None:
            self._ajax_client.close()
            self._ajax_client = None

    def fetch_board_list(self, kind: str = "industry",
                         as_of: datetime | None = None,
                         max_pages: int = THS_MAX_PAGES) -> list[dict]:
        """拉取板块列表 + 当日行情（**自动翻页**取全量）。

        kind: "industry"（行业板块）| "concept"（概念板块）
        返回 [{"code","name","type","board_code","pct_chg","net_inflow_yi",...}]

        ★★★ 必须翻页，否则只能拿到首屏
          实测 `/thshy/` 首页表格只渲染 **50** 行，但页面里有 **90** 个板块
          （左侧分类导航列出了全部 code）⇒ 不翻页就静默少 40 个板块，
          看起来「拿到了 50 个」，实际是残缺的。

        ★★ 行业与概念的取数路径**完全不同**，不能共用
          · 行业：首页 `<table>` + ajax 分页（每页 40）
          · 概念：ajax 第 1 页的 `gnSection` JSON（**一次给全 293 个**，
            无需翻页；`/gn/` 首页是新闻页，没有行情表）
          用同一套解析处理概念会**静默返回空**（实测「共 0 个」），
          看起来像「当天没有概念板块」。

        ★ 这是**当日快照**语义：不含历史。`board_strength` 只能靠逐日积累，
          不能回填（实测板块 K 线接口 404、成分股 ajax 401）。
        """
        seg = "thshy" if kind == "industry" else "gn"
        complete = True

        if kind == "concept":
            # 概念：一次 ajax 拿到全部（gnSection JSON）
            html = self._ajax_page(seg, 1)
            rows = parse_concept_section(html)
            pages_done = 1
            if not rows:
                logger.warning("[ths] ★ 概念板块 gnSection 解析为空 —— "
                               "页面结构可能已变（曾实测 293 个）")
        else:
            # 行业：首页 + ajax 翻页
            text = self._get(THS_INDUSTRY_URL)
            rows = parse_board_rows(text, kind)
            pages_done = 1
            if not rows:
                # 兜底：至少把 code/name 拿回来（页面结构变化时不至于完全失联）
                rows = self._fallback_links(text, kind)

            seen = {r["code"] for r in rows}
            for page in range(2, max_pages + 1):
                try:
                    html = self._ajax_page(seg, page)
                except SourceError as exc:
                    complete = not exc.retryable
                    # ★翻页失败**不能静默** —— 只能拿到部分板块却当全量，
                    #   会让「板块排名」失真而不报错
                    logger.warning(f"[ths] {kind} 第 {page} 页获取失败：{exc}；"
                                   f"已取 {len(rows)} 个板块（**不完整**）")
                    break
                new = [r for r in parse_board_rows(html, kind)
                       if r["code"] not in seen]
                if not new:
                    break# 到最后一页
                seen.update(r["code"] for r in new)
                rows.extend(new)
                pages_done += 1

        rows = self._guard(rows, f"board_list_{kind}")
        for row in rows:
            row["collection_complete"] = complete
        with_num = sum(1 for r in rows if r.get("pct_chg") is not None)
        logger.info(f"[ths] {kind} 板块 {len(rows)} 个"
                    f"（{pages_done} 页，含涨跌幅 {with_num} 个）")
        if rows and with_num == 0:
            logger.warning("[ths] ★ 全部板块都无涨跌幅 —— 页面结构可能已变，"
                           "此时 board_strength 会写入全 NULL 且不报错")
        return rows

    def _fallback_links(self, text: str, kind: str) -> list[dict]:
        """只取 code + name（页面结构变化时的降级）。"""
        out: list[dict] = []
        seen: set[str] = set()
        for m in RE_BOARD_LINK.finditer(text):
            code, name = m.group("code"), m.group("name").strip()
            if code in seen:
                continue
            seen.add(code)
            out.append({
                "code": code, "name": name, "type": kind,
                "board_code": f"THS{kind[:3].upper()}{code}",
                "pct_chg": None, "net_inflow_yi": None,
                "up_cnt": None, "down_cnt": None, "up_ratio": None,
                "leader_code": None, "leader_name": None,
            })
        return out

    def fetch_board_daily(self, board_code: str, as_of: datetime) -> dict:
        """单板块日涨幅。

        成分股/历史需Playwright 执行 JS / 板块K线接口，本期不实现
        （见模块 docstring 的「仍不可得的能力」）。
        """
        raise SourceError(
            1004,
            "同花顺板块详情为 JS 渲染页（ajax 实测 401），"
            "且板块历史 K 线接口 404 ⇒ 无法取单板块历史；"
            "当日数据请直接用 fetch_board_list()",
            retryable=False,
        )

    def fetch_realtime(self, codes: list[str], as_of: datetime) -> list[dict]:
        """板块不适用个股实时行情，直接委派腾讯。"""
        from backend.data.adapter import get_adapter

        return get_adapter("tencent").fetch_realtime(codes, as_of)

    def fetch_daily(self, code: str, count: int = 250,
                    as_of: datetime | None = None) -> list[dict]:
        raise SourceError(1004, "同花顺适配器不提供个股 K 线", retryable=False)


if __name__ == "__main__":
    import sys as _s
    from datetime import timedelta, timezone

    _s.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from datetime import datetime as _dt

    logger.remove()
    logger.add(_s.stderr, level="INFO")
    CST = timezone(timedelta(hours=8))
    as_of = _dt(2026, 10, 9, 15, 0, tzinfo=CST)

    a = ThsBoardAdapter()
    for kind in ("industry", "concept"):
        try:
            rows = a.fetch_board_list(kind, as_of)
            print(f"\n=== {kind} 板块（前 10 个，按涨跌幅降序）===")
            rows2 = [r for r in rows if r.get("pct_chg") is not None]
            rows2.sort(key=lambda r: -(r["pct_chg"] or 0))
            print(f"  {'板块':<14s}{'代码':<8s}{'涨跌%':>8s}{'净流入亿':>10s}"
                  f"{'涨':>5s}{'跌':>5s}{'广度':>7s}  领涨股")
            for r in rows2[:10]:
                ur = r.get("up_ratio")
                urs = f"{ur:.1%}" if ur is not None else "无"
                print(f"  {r['name']:<14s}{r['board_code']:<14s}"
                      f"{r['pct_chg']:>7.2f}%{(r.get('net_inflow_yi') or 0):>10.2f}"
                      f"{int(r.get('up_cnt') or 0):>5d}{int(r.get('down_cnt') or 0):>5d}"
                      f"{urs:>7s}  {r.get('leader_name')}({r.get('leader_code')})")
            print(f"  共 {len(rows)} 个，其中含行情 {len(rows2)} 个")
        except Exception as exc:
            print(f"{kind} 失败: {type(exc).__name__}: {exc}")
    print("\n健康度:", a.health.snapshot())
