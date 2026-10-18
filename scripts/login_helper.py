# -*- coding: utf-8 -*-
"""★ 后台版登录助手：拉起可见浏览器 → 轮询登录 cookie → 自动存盘。

为什么需要它
------------
`scrape_douyin.save_login_state()` 用 `input("按回车确认…")` 等用户确认。
在**后台/非交互**进程里 stdin 是关闭的 ⇒ 立刻 `EOFError` ⇒
直接跳到 `wait_for_timeout(120_000)` 固定等 2 分钟，
用户如果 30 秒就扫完码，也得干等；超过 2 分钟则登录态根本没存。

⇒ 这里改成**轮询 cookie**：抖音登录成功后核心 cookie 是 `sessionid`
   （+ `sessionid_ss`）。每 2 秒探一次，一拿到就立刻存盘退出。

★ 不代替用户操作：只负责开窗口 + 存登录态，扫码仍需用户在浏览器里完成。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from scripts.scrape_douyin import (  # noqa: E402
    SOURCES, STATE_DIR, _state_path,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ★ 抖音 web 端判定「已登录」的 cookie 组合。
#   sessionid 是核心；sessionid_ss / sid_tt 常常只在特定端上出现，故为或关系。
LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}

MAX_WAIT = 300  # 秒


def login_and_wait(key: str, max_wait: int = MAX_WAIT) -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = SOURCES[key]
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, channel="chromium",
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"])
        ctx = browser.new_context(
            locale="zh-CN", viewport={"width": 1440, "height": 900},
            user_agent=UA)
        pg = ctx.new_page()
        pg.goto(f"https://www.douyin.com/search/{cfg['search_kw']}",
                wait_until="domcontentloaded", timeout=60_000)
        print(f"[info] 已打开抖音搜索页：{cfg['search_kw']}", flush=True)

        ok = False
        t0 = time.time()
        while time.time() - t0 < max_wait:
            time.sleep(2)
            try:
                names = {c["name"] for c in ctx.cookies()}
            except Exception:
                continue
            if names & LOGIN_COOKIES:
                ok = True
                print(f"[info] 检出登录 cookie：{sorted(names & LOGIN_COOKIES)}",
                      flush=True)
                break

        # 不管是否检到，都存一次 —— 用户可能已手动关闭/或用别的方式登了
        out = _state_path(key)
        ctx.storage_state(path=str(out))
        browser.close()

    if ok:
        print(f"✔ 登录成功，登录态已存到 {out}", flush=True)
    else:
        print(f"⚠ 超时 {max_wait}s 未检测到登录 cookie。"
              f"已尝试存盘到 {out}，请确认浏览器里是否真的登录成功。",
              flush=True)
    return ok


if __name__ == "__main__":
    k = sys.argv[1] if len(sys.argv) > 1 else "yeren"
    if k not in SOURCES:
        print(f"未知源 {k}，可选：{sorted(SOURCES)}")
        raise SystemExit(2)
    raise SystemExit(0 if login_and_wait(k) else 1)
