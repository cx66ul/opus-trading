# -*- coding: utf-8 -*-
"""★ 抖音公开内容采集（M10 语料蒸馏的数据源）。

★★★ 规格红线（4.9.3）：规则必须逐条可回溯到**真实采集的语料**
----------------------------------------------------
> 🔴 **绝对禁止**：不得凭模型记忆或推测**编造**博主的方法论、口头禅、指标或战绩。

⇒ 本模块的**唯一职责**是把公开可见的内容原样落进 `corpus` 表，
   `transcript_ref` 保留可回溯的定位（作品 id / aweme_id）。
   它**不做任何提炼**——提炼是 `distill.py` 的事，且必须基于本表真实内容。

★★ 为什么必须用浏览器而不是 HTTP 请求
------------------------------------
实测（2026-10-10）：
  · `www.douyin.com/user/<sec_uid>` → 200但返回 **JS 挑战页**
    （含 `_$jsvmprt`，无 `RENDER_DATA` / `_ROUTER_DATA` / `aweme`）
  · `aweme/v1/web/search/item/` → 200但 `aweme_list: null`
    （需 `X-Bogus` / `a_bogus` 签名）

⇒ 纯 HTTP 拿不到数据。正确做法是**开真实浏览器执行 JS**，
   然后**拦截 XHR 响应**取结构化 JSON —— 这比解析 DOM 稳得多
   （DOM class 名会变，接口字段不会）。

★ 合规边界（规格 §12「蒸馏素材合规」）
------------------------------------
  · 只采集**公开发布**的内容
  · 仅用于个人研究学习
  · 蒸馏产出为**规则化抽象逻辑**，不复制传播原始语料的实质内容
  · **不得对外宣称与任何博主存在关联、合作或背书关系**

运行：
    python scripts/scrape_douyin.py --list
    python scripts/scrape_douyin.py yeren --limit 20 --dry-run
    python scripts/scrape_douyin.py yeren --limit 20            # 落库
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

CST = timezone(timedelta(hours=8))

# ★ 规格 4.9.3 指定的两个席位。**只允许采集公开内容**。
SOURCES = {
    "yeren": {
        "name": "全能的野人",
        "uid": "203775400",
        # 主页需要 sec_uid（不是数字 uid），先经搜索/分享页解析
        "profile_hint": "https://www.douyin.com/user/",
        "search_kw": "全能的野人",
        # ★ 实测（2026-10-10）定位到的 sec_uid，**已缓存**。
        #   为什么不每次重跑搜索：搜索页是抖音反爬最严的一环，
        #   首次能定位、紧接着重跑就定位不到（页面上「登录/验证码」字样）。
        #   sec_uid 是账号级稳定标识，一旦拿到就不该再依赖脆弱的搜索页。
        "sec_uid": "MS4wLjABAAAAjoG0q686OVKqPnPYAhZVaVl5Y6Ul8gbWprwF52ualFY",
    },
    "kuajing_yangyang": {
        "name": "跨境阳阳",
        "uid": "88075201596",
        "profile_hint": "https://www.douyin.com/user/",
        "search_kw": "跨境阳阳",
        "sec_uid": None,      # ★ 尚未定位到，需成功搜索一次后回填
    },
}

DB_PATH = _ROOT / "data" / "hawk.sqlite"


@dataclass
class Clip:
    """一条公开内容（视频/图文）。★ 字段与 `corpus` 表一一对应。"""
    source: str
    aweme_id: str
    title: str = ""
    desc: str = ""
    create_time: str = ""
    duration: int = 0
    transcript_ref: str = ""
    raw: dict = field(default_factory=dict)

    def to_row(self, strategy_id: str) -> tuple:
        """→ `corpus` 表的一行。

        ★ `content` 存**原文**（不加工）。
          `transcript_ref` 用 `aweme_id` —— 它是抖音作品的**稳定唯一标识**，
          可直接回溯到原页面，构成 evidence 链的锚点。

        ★★ 标题与描述相同则**只存一份**。
          抖音的 `desc` 是**唯一**的文案字段，`title` 从它截断而来
          （`_clip_from_aweme` 里 `title=desc[:120]`）⇒ 两者恒相同。
          原来无脑 `"\n".join([title, desc])` 会让**每一行语料都是同一段文字
          写两遍**（实测129 条里 122 条重复，94.6%）。
          除了撑大库体，更坏的是：下游任何按行计数的统计
          （"这则语料提到了 3 次半导体"）都会**凭空翻倍**。
        """
        t = self.title.strip()
        d = self.desc.strip()
        if t and d and (d.startswith(t) or t == d):
            content = d            # 截断版被完整版覆盖 ⇒ 只留完整的
        else:
            content = "\n".join(x for x in (t, d) if x).strip()
        return (strategy_id, self.source, self.create_time, content,
                self.transcript_ref,
                datetime.now(CST).isoformat(timespec="seconds"), 0)


def ensure_corpus_table(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS corpus (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL, source TEXT, pub_time TEXT,
          content TEXT, transcript_ref TEXT, collected_at TEXT,
          processed INTEGER DEFAULT 0
        )
    """)


def upsert_corpus(rows: list[tuple]) -> int:
    """幂等写入。★ 用 `transcript_ref` 作去重键。

    为什么不用 `INSERT OR IGNORE` + 主键：`corpus.id` 是自增整数，
    同一内容重复采集会产生**新行** ⇒ 语料库被重复条目灌满，
    后续蒸馏会把同一条内容算成多个样本。
    """
    if not rows:
        return 0
    # ★ 这里原先误写`ensure_corpus_table(DB)` —— 模块里根本没有 `DB` 全局，
    #   于是**每次真落库必崩**（此前一直靠 --dry-run 掩盖着）。
    #   正确做法：先建连接再在同一连接上建表。
    con = sqlite3.connect(DB_PATH)
    try:
        ensure_corpus_table(con)
        seen = {r[0] for r in con.execute(
            "SELECT transcript_ref FROM corpus WHERE transcript_ref IS NOT NULL"
        )}
        fresh = []
        for r in rows:
            if r[4] in seen:      # transcript_ref
                continue
            seen.add(r[4])
            fresh.append(r)
        if fresh:
            con.executemany(
                "INSERT INTO corpus (strategy_id, source, pub_time, content,"
                " transcript_ref, collected_at, processed)"
                " VALUES (?,?,?,?,?,?,?)",
                fresh,
            )
            con.commit()
        return len(fresh)
    finally:
        con.close()


# ================================================================== 采集
# ★ 持久化登录态（Playwright storage_state）
#   未登录时抖音搜索接口**根本不返回数据**（实测 2026-10-10：
#   `?type=user` 只发出 ab/params、social/count 等接口，
#   **搜索结果接口未被调用**，页面同时含「登录」与「验证码」字样）。
STATE_DIR = _ROOT / "data" / "browser_state"


# ★ 登录态是**账号级**的，不是「每个博主一份」。
#   原实现按 key 存`douyin_<key>.json` ⇒ 采第二个博主时提示「未找到登录态」，
#   可抖音只有一个账号，为什么要你登录两次？实测确实多余。
#⇒ 改为**单一共享文件** `douyin_account.json`，
#   同时兼容旧的按源文件（迁移一次，读时回退）。
ACCOUNT_STATE = STATE_DIR / "douyin_account.json"


def _state_path(key: str) -> Path:
    """→ 共享登录态路径。

    ★ `key` 参数只为向后兼容保留（调用点不必改）——
      登录态与`key` 无关，它是**你的抖音账号凭据**。
    """
    if ACCOUNT_STATE.exists():
        return ACCOUNT_STATE
    legacy = STATE_DIR / f"douyin_{key}.json"
    if legacy.exists():
        return legacy
    return ACCOUNT_STATE


def migrate_legacy_state() -> None:
    """把旧的按源登录态合并到共享文件。"""
    if ACCOUNT_STATE.exists():
        return
    for old in STATE_DIR.glob("douyin_*.json"):
        if old == ACCOUNT_STATE or old.name == SEC_UID_CACHE.name:
            continue
        ACCOUNT_STATE.write_bytes(old.read_bytes())
        print(f"[info] 旧登录态已迁移：{old.name} → {ACCOUNT_STATE.name}")
        old.unlink()
        return


# ★ sec_uid 缓存（账号级稳定标识，但获取入口= 反爬最严的搜索页）
SEC_UID_CACHE = STATE_DIR / "sec_uid_cache.json"


def load_cached_sec_uid(key: str) -> str | None:
    if not SEC_UID_CACHE.exists():
        return None
    try:
        return json.loads(SEC_UID_CACHE.read_text("utf-8")).get(key) or None
    except Exception:
        return None


def save_cached_sec_uid(key: str, sec_uid: str) -> None:
    """把定位到的 sec_uid 落盘。

    ★ 为什么必须缓存：抖音搜索页是最容易被反爬拦的一环，
      实测「首次能定位 → 紧接着重跑就定位不到」，
      页面同时出现「登录/验证码」字样。
      sec_uid 一旦拿到就是账号的永久标识，不该反复去够脆弱的搜索页。
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if SEC_UID_CACHE.exists():
        try:
            data = json.loads(SEC_UID_CACHE.read_text("utf-8"))
        except Exception:
            data = {}
    if data.get(key) == sec_uid:
        return
    data[key] = sec_uid
    SEC_UID_CACHE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    print(f"[info] sec_uid 已缓存到 {SEC_UID_CACHE.name}")


def save_login_state(key: str) -> None:
    """打开可见浏览器让你扫码登录，登录态存盘复用。

    用法：`python scripts/scrape_douyin.py yeren --login`
    登录态是**你的账号凭据**，故：
      · 存本地 `data/browser_state/`，不进 git
      · `.gitignore` 已覆盖 `data/`
    """
    from playwright.sync_api import sync_playwright

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chromium")
        ctx = browser.new_context(
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"))
        pg = ctx.new_page()
        pg.goto(f"https://www.douyin.com/search/{SOURCES[key]['search_kw']}",
                wait_until="domcontentloaded", timeout=60_000)
        print("★ 请在弹出的浏览器里扫码登录（完成后关掉窗口即可）")
        try:
            input("按回车确认已完成登录…")
        except EOFError:
            pg.wait_for_timeout(120_000)
        ctx.storage_state(path=str(_state_path(key)))
        browser.close()
    print(f"✔ 登录态已存到 {_state_path(key)}")


def scrape_with_browser(key: str, limit: int = 20,
                        headless: bool = True) -> list[Clip]:
    """用真实浏览器打开博主主页，**拦截 XHR** 取作品列表。

    ★ 为什么拦 XHR 而不是解析 DOM：
      抖音 DOM 的 class 名是构建产物（会变），
      而接口 JSON 的字段是业务契约（相对稳定）。
      且列表是**滚动加载**的，拦 XHR 能自然覆盖翻页。

    ★ `channel="chromium"` 必需：headless_shell 在本机报 `spawn EFTYPE`。
    """
    from playwright.sync_api import sync_playwright

    cfg = SOURCES[key]
    clips: list[Clip] = []
    seen_ids: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless, channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        # ★ 复用已保存的登录态（没有也能跑，只是搜不到内容）。
        #   注意：**不要**在 if 之后再无条件 `ctx.new_page()` ——
        #   那会用默认 context 覆盖掉刚加载登录态的 context，
        #   导致「有登录态也等于没登录」（曾如此写错过）。
        st = _state_path(key)
        if st.exists():
            try:
                ctx = browser.new_context(
                    storage_state=str(st), locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"))
                print(f"[info] 已加载登录态 {st.name}")
            except Exception as exc:
                print(f"[warn] 登录态加载失败（改用无登录态）：{exc}")
        page = ctx.new_page()

        def on_response(resp):
            """拦截作品列表接口。"""
            url = resp.url
            if "aweme" not in url and "user/post" not in url:
                return
            if resp.status != 200:
                return
            try:
                data = resp.json()
            except Exception:
                return
            items = (data.get("aweme_list")
                     or (data.get("data") or {}).get("aweme_list") or [])
            for it in items:
                aid = str(it.get("aweme_id") or "")
                if not aid or aid in seen_ids:
                    continue
                seen_ids.add(aid)
                clips.append(_clip_from_aweme(cfg, it))

        page.on("response", on_response)

        # ★ 先经搜索定位 sec_uid（数字 uid 不是主页路径）
        sec_uid = cfg.get("sec_uid") or load_cached_sec_uid(key)     # ★ 优先用已缓存的
        if sec_uid:
            print(f"[info] 使用已缓存 sec_uid：{sec_uid}（跳过搜索页）")
        if not sec_uid:
            # ★ 从搜索结果里找博主主页链接。
            #   必须**逐个检查候选**并剔除 JS 里的 `self`/`undefined` 之类
            #   字面量 —— 页面脚本里常有 `location.href="/user/self"` 这类占位，
            #   `get_attribute` 取到第一个 href 就停会拿到 "self"（实测）。
            try:
                page.goto(
                    f"https://www.douyin.com/search/{cfg['search_kw']}",
                    wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(4_000)
            except Exception as exc:
                print(f"[warn] 搜索页打开失败：{exc}")

            seen_hrefs: set[str] = set()
            for _ in range(4):
                try:
                    links = page.eval_on_selector_all(
                        'a[href*="/user/"]',
                        "els => els.map(e => e.getAttribute('href'))")
                except Exception:
                    links = []
                for link in links:
                    if not link or link in seen_hrefs:
                        continue
                    seen_hrefs.add(link)
                    m = re.search(r"/user/([\w-]{20,})", link)
                    # ★ sec_uid 是长base64 串（≥20 字符）；"self"/"undefined" 都短
                    if m:
                        cand = m.group(1)
                        if cand.lower() not in ("self", "undefined", "null"):
                            sec_uid = cand
                            break
                if sec_uid:
                    break
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(1_800)

        if not sec_uid:
            print(f"[warn] 未能定位 {cfg['name']} 的 sec_uid"
                  f"（可能需登录或触发了验证码）")
            return clips

        print(f"[info] sec_uid = {sec_uid}")
        save_cached_sec_uid(key, sec_uid)
        try:
            page.goto(f"https://www.douyin.com/user/{sec_uid}",
                       wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(4_000)
            # 滚动触发更多作品
            for _ in range(6):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1_500)
                if len(clips) >= limit:
                    break
        except Exception as exc:
            print(f"[warn] 主页打开失败：{exc}")

        browser.close()

    return clips[:limit]


def _clip_from_aweme(cfg: dict, it: dict) -> Clip:
    """抖音作品 JSON → `Clip`（★ 只取公开字段，不做提炼）。"""
    aweme_id = str(it.get("aweme_id") or "")
    ts = it.get("create_time")
    create_time = (datetime.fromtimestamp(int(ts), CST).isoformat(
        timespec="seconds") if ts else "")
    return Clip(
        source=f"douyin:{cfg['uid']}",
        aweme_id=aweme_id,
        title=str(it.get("desc") or "")[:120],
        desc=str(it.get("desc") or ""),
        create_time=create_time,
        duration=int(it.get("duration") or 0),
        # ★ 可回溯锚点：抖音作品 id 可直接拼出页面 URL
        transcript_ref=f"douyin:aweme:{aweme_id}",
        raw={k: it.get(k) for k in ("aweme_id", "desc", "create_time")
             if k in it},
    )


# ================================================================== CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="抖音公开内容采集（M10 语料源）")
    #★ `source` 不用 `default="list"` —— 它必须和"列出源"这个动作分开：
    #   两者共用一个参数会让 `--list` 报「invalid choice 'list'」
    #   （argparse 先校验再赋值，default 值也在 choices 里被校验）。
    ap.add_argument("source", nargs="?", choices=sorted(SOURCES) + ["all"],
                    default=None, help=f"语料源，可选：{sorted(SOURCES)} / all")
    ap.add_argument("--list", action="store_true", help="列出可用语料源")
    ap.add_argument("--login", action="store_true",
                    help="打开浏览器扫码登录并保存登录态（首次采集前必做）")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = ap.parse_args()

    if args.list or args.source is None:
        print("=" * 68)
        print("M10 语料源（规格 4.9.3指定的两个席位）")
        print("=" * 68)
        for k, v in SOURCES.items():
            print(f"  {k:20s} {v['name']:12s} uid={v['uid']}")
        print()
        print("★ 合规：只采集公开发布内容，仅用于个人研究学习；")
        print("  不得对外宣称与任何博主存在关联、合作或背书关系。")
        print("★ 红线：规则必须逐条可回溯到本表真实内容，**不得凭记忆编造**。")
        return 0

    keys = sorted(SOURCES) if args.source == "all" else [args.source]

    migrate_legacy_state()

    if args.login:
        for k in keys:
            print(f"\n=== 登录 {k}（{SOURCES[k]['name']}）===")
            try:
                save_login_state(k)
            except Exception as exc:
                print(f"[fail] {type(exc).__name__}: {exc}")
        return 0

    # ★ 没登录态时**提前告知**，而不是跑完才说采不到 ——
    #   未登录时搜索接口根本不返回数据（实测），跑一遍要40 秒且必然为空。
    if not any(_state_path(k).exists() for k in keys):
        print("⚠ 未找到登录态。未登录时抖音**不返回任何搜索数据**"
              "（实测 2026-10-10）。")
        print("  请先执行： python scripts/scrape_douyin.py "
              f"{keys[0]} --login")
        return 2

    total = 0
    # ★ 记住每个源「采到多少」，用于区分「没采到」与「dry-run 没写库」
    _last_n: dict[str, int] = {}
    for k in keys:
        print(f"\n=== 采集 {k}（{SOURCES[k]['name']}）===")
        try:
            clips = scrape_with_browser(k, args.limit, headless=not args.headed)
        except Exception as exc:
            print(f"[fail] {type(exc).__name__}: {exc}")
            continue
        print(f"  采到 {len(clips)} 条")
        _last_n[k] = len(clips)
        for c in clips[:3]:
            print(f"    {c.create_time[:10]} {c.aweme_id} "
                  f"{(c.title or '(无标题)')[:40]}")
        if clips and not args.dry_run:
            rows = [c.to_row(k) for c in clips]
            n = upsert_corpus(rows)
            print(f"  ✔ 落库 {n} 条（去重后）")
            total += n
        elif args.dry_run:
            print("  --dry-run：未写库")

    # ★ 尾部提示必须区分「采集失败」与「dry-run 没写库」两种0 条原因，
    #   否则 dry-run 明明采到 20 条却打印「未采到任何内容」——
    #   属于会误导人的假告警（早期版本已犯）。
    got = 0
    for k in keys:
        got += _last_n.get(k, 0)
    if got:
        print(f"\n共采到 {got} 条")
        if args.dry_run:
            print("（--dry-run：未写库）")
    else:
        print("\n★ 未采到任何内容。可能原因：需登录态 / 触发验证码 / 反爬升级。")
        print("  此时**不得凭记忆补写语料**（规格 4.9.3 红线）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())