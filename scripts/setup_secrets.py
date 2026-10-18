#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hawk · SMTP 凭据写入工具（DPAPI 加密落盘）

★ 本脚本是补齐交付缺口：``backend/common/mailer.py`` 在凭据缺失时
  提示用户「请先运行 scripts/setup_secrets.py 写入加密存储」，
  但该脚本此前并不存在，用户按提示操作只会得到「文件不存在」，
  邮件功能因此事实上无法配置。

用法（在项目根目录执行）：
    .venv/Scripts/python.exe scripts/setup_secrets.py            # 交互式
    .venv/Scripts/python.exe scripts/setup_secrets.py --show     # 只看现状
    .venv/Scripts/python.exe scripts/setup_secrets.py --delete   # 清除凭据

安全说明：
  · 凭据经 Windows DPAPI 加密，绑定当前 Windows 账户；
    换机器或换账户都无法解密，配置与代码中均无明文。
  · 授权码输入用 getpass，不回显、不落 shell 历史。
  · 本脚本不联网、不发信 —— 需要测试发信请另行调用邮件预览。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.common import secrets  # noqa: E402

# 键名与 MailConfig.from_secrets 的读取约定严格一致，勿改
KEYS = ["smtp.host", "smtp.port", "smtp.user", "smtp.auth_code",
        "smtp.sender", "mail.to"]

DEFAULTS = {
    "smtp.host": "smtp.qq.com",
    "smtp.port": "465",
}

PROMPTS = {
    "smtp.host": "SMTP 服务器",
    "smtp.port": "端口（465=SSL）",
    "smtp.user": "登录账号（通常为邮箱）",
    "smtp.auth_code": "SMTP 授权码（不是登录密码，输入不回显）",
    "smtp.sender": "发件人地址（留空则同登录账号）",
    "mail.to": "收件人（多个用分号分隔，留空则发给自己）",
}

SECRET_KEYS = {"smtp.auth_code"}


def show() -> int:
    print("=== 当前凭据状态（只显示元信息，不显示值）===")
    any_set = False
    for k in KEYS:
        info = secrets.describe(k)
        if info.get("exists"):
            any_set = True
            print(f"  ✓ {k:16s} backend={info.get('backend')} "
                  f"cipher={info.get('cipher_bytes')}B")
        else:
            print(f"  · {k:16s} 未设置")
    if not any_set:
        print("\n尚未配置。运行不带参数的 setup_secrets.py 开始配置。")
    return 0


def delete() -> int:
    n = 0
    for k in KEYS:
        if secrets.delete_secret(k):
            n += 1
    print(f"已删除 {n} 项凭据。")
    return 0


def interactive() -> int:
    print("=== Hawk SMTP 凭据配置 ===")
    print("凭据将用 Windows DPAPI 加密，仅当前 Windows 账户可解密。")
    print("直接回车表示保留原值（或使用默认值）。\n")

    for k in KEYS:
        if k in SECRET_KEYS:
            val = getpass.getpass(f"{PROMPTS[k]}: ").strip()
        else:
            default = DEFAULTS.get(k, "")
            hint = f"（默认 {default}）" if default else ""
            cur = secrets.get_secret(k)
            existing = "（已设置，回车保留）" if cur else ""
            val = input(f"{PROMPTS[k]}{hint}{existing}: ").strip()
        if not val:
            if secrets.has_secret(k):
                print(f"  → 保留原值\n")
                continue
            if k in DEFAULTS:
                val = DEFAULTS[k]
                print(f"  → 使用默认值 {val}")
            else:
                print(f"  → 跳过（未设置 {k}）\n")
                continue
        secrets.set_secret(k, val)
        print(f"  → 已写入\n")

    # sender 缺省时回落到登录账号（与 MailConfig 的读取逻辑一致）
    if not secrets.has_secret("smtp.sender") and secrets.has_secret("smtp.user"):
        u = secrets.get_secret("smtp.user")
        if u:
            secrets.set_secret("smtp.sender", u)
            print(f"发件人未指定，已按登录账号写入: {u}")

    print()
    return show()


def main() -> int:
    ap = argparse.ArgumentParser(description="Hawk SMTP 凭据写入（DPAPI 加密）")
    ap.add_argument("--show", action="store_true", help="只查看当前状态")
    ap.add_argument("--delete", action="store_true", help="删除全部凭据")
    a = ap.parse_args()

    if a.show:
        return show()
    if a.delete:
        return delete()
    return interactive()


if __name__ == "__main__":
    raise SystemExit(main())
