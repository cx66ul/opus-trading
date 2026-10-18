# -*- coding: utf-8 -*-
"""★ 产品改名：Hawk → **Opus 辅助交易**（用户 2026-10-11 指示）

★★★ 关键区分：品牌名 ≠ 内部标识符
--------------------------------------------------
改名时最容易犯的错是**全局替换**，把
    data/hawk.duckdb / data/hawk.sqlite / 项目目录 hawk/
一起改掉 ⇒ **历史数据全部"消失"**（旧文件还在，新路径读不到），
而且没有任何报错 —— 只是「数据空了」。

这三个属于「数据身份」不是「品牌名」：
    · `hawk.duckdb` / `hawk.sqlite`：库文件，改名=丢 137 万行行情
    · 目录 `hawk/`：被上面两个模块的 `_ROOT` 推导依赖
    · Python 模块路径 `backend.*`：改了会 ImportError（这个会报错，相对安全）

⇒ 本组测试守护「界面/包名已改名」且「数据身份未被误改」。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestUserVisibleNameChanged:
    """★ 用户能看到的地方必须显示 Opus 辅助交易。"""

    def test_page_title(self):
        html = (ROOT / "backend" / "web" / "index.html").read_text(
            encoding="utf-8")
        assert "Opus 辅助交易" in html, "页面标题未改名"
        assert "<h1>OPUS</h1>" in html, "页面左上角标识未改名"
        # ★ 旧名不得再出现在 title/h1 里
        assert not re.search(r"<title>[^<]*Hawk", html), "title 仍含旧名"

    def test_window_and_tray_title(self):
        src = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
        assert "title: 'Opus 辅助交易'" in src, "窗口标题未改名"
        assert "tray.setToolTip('Opus 辅助交易')" in src, "托盘提示未改名"

    def test_package_identity(self):
        pkg = json.loads((ROOT / "desktop" / "package.json")
                         .read_text(encoding="utf-8"))
        assert pkg["build"]["productName"] == "Opus 辅助交易", (
            "安装包/开始菜单显示名未改名")
        assert pkg["build"]["nsis"]["shortcutName"] == "Opus 辅助交易", (
            "桌面快捷方式名未改名")
        # ★ artifactName 决定产物文件名，必须也带新名
        assert "Opus-" in pkg["build"]["win"]["artifactName"], (
            "产物文件名仍是旧名")


class TestInternalIdentityUntouched:
    """★★ 数据身份**不得**被改名 —— 改了等于丢数据。"""

    def test_db_filenames_unchanged(self):
        duck = (ROOT / "backend" / "data" / "duck.py").read_text(
            encoding="utf-8")
        lite = (ROOT / "backend" / "data" / "sqlite.py").read_text(
            encoding="utf-8")
        assert '"hawk.duckdb"' in duck, (
            "DuckDB 文件名被改 ⇒ 历史行情读不到，且无报错")
        assert '"hawk.sqlite"' in lite, (
            "SQLite 文件名被改 ⇒ 自选/持仓/语料读不到")

    def test_project_dir_name_kept(self):
        """目录名保持 hawk/（被 _ROOT 推导依赖）。"""
        assert ROOT.name == "hawk", (
            f"项目目录被改名成 {ROOT.name} —— 数据库路径推导会失效")

    def test_actual_db_files_still_exist(self):
        """★ 更强的一条：**确认旧文件真的还在**。

        这条比「检查源码里的字符串」有效 ——
        源码写对了但文件被移动/删除，同样是丢数据。
        """
        data = ROOT / "data"
        assert (data / "hawk.duckdb").exists(), "hawk.duckdb 实际不见了"
        assert (data / "hawk.sqlite").exists(), "hawk.sqlite 实际不见了"


class TestPreloadExposure:
    """★ preload 暴露面必须最小（安全相关）。"""

    def test_does_not_expose_ipc_renderer(self):
        src = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")
        assert "exposeInMainWorld('ipcRenderer'" not in src, (
            "把 ipcRenderer 整体暴露出去 ⇒ 渲染进程可调任意通道")

    def test_only_named_apis(self):
        src = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")
        body = src.split("exposeInMainWorld", 1)[1]
        assert "invoke('" in body, "没有暴露任何接口"
        # ★ 通道名必须与 main.js 里 ipcMain.handle 的一致
        main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
        for ch in re.findall(r"invoke\('([^']+)'", body):
            assert f"ipcMain.handle('{ch}'" in main, (
                f"preload 调了未注册的通道 {ch}")
