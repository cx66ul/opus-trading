# -*- coding: utf-8 -*-
"""Hawk · 统一日志配置（滚动文件 + 控制台）

★ 补齐交付缺口：此前全项目所有 ``logger.add`` 都指向 ``sys.stderr``，
  没有任何文件落盘、没有 rotation —— 而规格要求
  「loguru → JSONL 滚动文件」「结构化日志（滚动 30 天）」。
  后果是常驻运行出问题时**事后无从查证**：控制台一关，日志即消失。
  做 T / 竞价 / 盘中信号这类需要复盘的问题，没有日志等于无法定位。

设计：
  · 控制台：人读，带颜色，INFO 级。
  · 文件：JSONL，机器读，DEBUG 级，按天滚动、保留 30 天、gzip 压缩。
  · 目录：``<项目根>/logs/``，可用 ``HAWK_LOG_DIR`` 覆盖（便携包/测试用）。
  · 只应由**入口点**调用一次；重复调用是幂等的（先 remove 再加 sink）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent.parent

_configured = False


def log_dir() -> Path:
    """日志目录。HAWK_LOG_DIR 优先（测试与便携包用）。"""
    d = Path(os.environ.get("HAWK_LOG_DIR") or (_ROOT / "logs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup(level: str = "INFO", *, json_file: bool = True) -> Path:
    """配置 loguru。返回日志文件路径。

    :param level: 控制台最低级别
    :param json_file: 是否写 JSONL 文件（测试可关）
    """
    global _configured
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>",
    )
    path = log_dir() / "hawk.log"
    if json_file:
        # serialize=True -> 每行一个 JSON 对象，便于事后用 jq/脚本检索
        logger.add(
            path,
            level="DEBUG",
            rotation="00:00",      # 按天滚动
            retention="30 days",   # 保留 30 天
            compression="gz",
            encoding="utf-8",
            enqueue=True,          # 多线程/多进程写入安全
            serialize=True,
            backtrace=False,
            diagnose=False,        # 不把变量值写进日志，避免泄漏凭据
        )
    _configured = True
    return path


def is_configured() -> bool:
    return _configured


if __name__ == "__main__":                       # pragma: no cover
    p = setup()
    logger.info("日志自测：这条应同时出现在控制台与文件")
    logger.debug("debug 级只进文件")
    print(f"日志文件：{p}")
    print("末行内容：")
    with open(p, encoding="utf-8") as f:
        lines = f.readlines()
    print("  " + (lines[-1].strip() if lines else "（空）"))
