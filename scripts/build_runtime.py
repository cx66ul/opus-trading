#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Opus 辅助交易 · 打 Python 运行时目录（供 electron-builder 打进包）

★★ 为什么必须自带 runtime
--------------------------------------------------
用户机器上没有 Python、没有 duckdb。electron-builder 只打包 JS，
Python 后端跑不起来 ⇒ 装完即废。
⇒ 必须把**可移植的 Python + 依赖**一起打进去，
  `main.js` 里 `process.resourcesPath/runtime/python.exe` 就是它。

★★ 为什么不用 embeddable 官方包而用 venv
--------------------------------------------------
官方 embeddable 包不含 pip、且 `._pth` 会限制 `import site`，
装第三方包很别扭。venv 的可移植性够用且依赖能直接装。

用法：
    python scripts/build_runtime.py            # 生成 runtime/
    python scripts/build_runtime.py --verify   # 只校验现有 runtime/
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
SRC_PY = Path(sys.executable)

#: 打包进运行时的依赖。★ 与 requirements.txt 保持一致，
#:   但**不含 pytest/ruff**（开发期工具，装进产品纯属浪费体积）。
RUNTIME_PKGS = ["duckdb", "httpx", "loguru", "PyYAML", "fastapi", "uvicorn"]


def sh(*args: str) -> None:
    print("  $", " ".join(args), flush=True)
    subprocess.run(args, check=True)


def build() -> None:
    if RUNTIME.exists():
        # ★ 不用 shutil.rmtree 静默删除：先改名再删，出问题能恢复
        old = RUNTIME.with_name("runtime.old")
        if old.exists():
            shutil.rmtree(old)
        RUNTIME.rename(old)
        shutil.rmtree(old, ignore_errors=True)

    print("① 建 venv…")
    sh(str(SRC_PY), "-m", "venv", str(RUNTIME))

    py = RUNTIME / "Scripts" / "python.exe"
    if not py.exists():
        raise SystemExit(f"venv 未生成可执行文件：{py}")

    print("② 装依赖…")
    sh(str(py), "-m", "pip", "install", "--upgrade", "pip",
       "--disable-pip-version-check", "-q")
    sh(str(py), "-m", "pip", "install", *RUNTIME_PKGS,
       "--disable-pip-version-check", "-q")

    print("③ 清缓存/测试文件（省体积）")
    for pat in ("**/__pycache__", "**/*.pyc", "**/tests"):
        for p in RUNTIME.glob(pat):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)

    verify()


def verify() -> None:
    """★ 生成后必须校验，且校验的是**真的能跑**而不是「文件在」。"""
    py = RUNTIME / "Scripts" / "python.exe"
    if not py.exists():
        raise SystemExit("✗ runtime/Scripts/python.exe 不存在")

    # ★ 关键：从 runtime 里 import，看依赖是不是真的装在**这个 venv** 内
    code = ("import duckdb,fastapi,uvicorn,httpx,yaml,loguru;"
            "import pathlib;"
            "assert all(pathlib.Path(m.__file__).is_relative_to("
            f"pathlib.Path(r'{RUNTIME}')) for m in "
            "(duckdb,fastapi,uvicorn,httpx,yaml,loguru));"
            "print('deps-ok')")
    out = subprocess.run([str(py), "-c", code],
                         capture_output=True, text=True)
    if out.returncode != 0 or "deps-ok" not in out.stdout:
        raise SystemExit("✗ 依赖校验失败（可能被装到了 venv 之外）：\n"
                         + (out.stderr or out.stdout)[-800:])

    total = sum(f.stat().st_size for f in RUNTIME.rglob("*") if f.is_file())
    print(f"\n✓ runtime 就绪：{RUNTIME}")
    print(f"  依赖全部位于 runtime 内（不会误用系统 Python）")
    print(f"  体积：{total / 1048576:.1f} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成打包用 Python 运行时")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    if a.verify:
        verify()
    else:
        build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
