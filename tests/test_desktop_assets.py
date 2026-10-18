# -*- coding: utf-8 -*-
"""★ 桌面端资源：图标 / preload 暴露面 / 打包配置

★★★ 为什么图标也要测
--------------------------------------------------
「图标生成脚本跑完没报错」≠「图标能用」。
实测过的两个问题都是**无声的**：
  · `.ico` 只写进一个尺寸 ⇒ 任务栏某些缩放级别显示默认 Electron 图标
  · `tray.png` 用了 256px大图 ⇒ 托盘里糊成一团黑
两者都不抛异常，只有肉眼在特定环境下才看得出来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DESKTOP = ROOT / "desktop"
ASSETS = DESKTOP / "assets"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


class TestIconAssets:
    def test_ico_contains_all_sizes(self):
        """★ `.ico` 必须含全部 7 个尺寸。

        变异验证：只保存一个尺寸时本判据必须红。
        """
        from PIL import Image
        ico = ASSETS / "icon.ico"
        assert ico.exists(), "缺少 icon.ico"
        with Image.open(ico) as im:
            sizes = {s[0] for s in (im.info.get("sizes") or [])}
        missing = [s for s in ICO_SIZES if s not in sizes]
        assert not missing, f"icon.ico 缺尺寸 {missing}（实际 {sorted(sizes)}）"

    def test_png_256_exists(self):
        """electron-builder 的 buildResources 需要 icon.png。"""
        p = ASSETS / "icon.png"
        assert p.exists(), "缺少 icon.png"
        from PIL import Image
        with Image.open(p) as im:
            assert im.size == (256, 256), f"icon.png 是 {im.size}，应为 256x256"

    def test_tray_is_small(self):
        """★ 托盘图标必须**小**。

        用 256px 图当托盘，在高分屏上会糊成一团 ——
        而这既不报错、也不影响功能，只有天天用的人才发现得了。
        """
        from PIL import Image
        p = ASSETS / "tray.png"
        assert p.exists(), "缺少 tray.png"
        with Image.open(p) as im:
            assert im.size[0] <= 64, (
                f"tray.png 是 {im.size[0]}px，太大会在托盘里糊")

    def test_icons_are_square(self):
        """★ 图标不能被拉伸变形（非正方形源图要居中裁切）。"""
        from PIL import Image
        for f in ("icon.png", "tray.png"):
            with Image.open(ASSETS / f) as im:
                w, h = im.size
                assert w == h, f"{f} 是 {w}x{h}，非正方形（会变形）"


class TestIconWiring:
    def test_main_uses_assets(self):
        src = (DESKTOP / "main.js").read_text(encoding="utf-8")
        assert "icon: pickIcon()" in src, "窗口未设置图标"
        assert "tray.png" in src, "托盘未使用托盘尺寸图标"

    def test_pick_icon_has_fallback(self):
        """★ 找不到图标文件必须**返回 undefined**，不能抛。

        否则打包路径一变，窗口直接开不出来 ——
        而「图标路径不对」这种小问题不该导致整个应用启动失败。
        """
        src = (DESKTOP / "main.js").read_text(encoding="utf-8")
        i = src.index("function pickIcon")
        j = src.index("\n}", i)
        body = src[i:j]
        assert "return undefined" in body, (
            "pickIcon 找不到文件时必须返回 undefined 而不是抛异常")
        assert "existsSync" in body, "pickIcon 未做存在性检查"

    def test_builder_icon_configured(self):
        pkg = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        assert pkg["build"]["win"]["icon"], (
            "electron-builder 未配置 win.icon ⇒ 产物用默认 Electron 图标")
        assert "assets/**/*" in pkg["build"]["files"], (
            "assets 未打进 files ⇒ 打包后找不到图标")


class TestGeneratorScript:
    def test_script_verifies_output(self):
        """★ 生成脚本必须**校验产物**，不能「生成完就报成功」。"""
        src = (ROOT / "scripts" / "make_desktop_icons.py").read_text(
            encoding="utf-8")
        assert "def verify(" in src, "生成脚本缺校验函数"
        i = src.index("def verify(")
        j = src.index("\ndef ", i + 10)
        body = src[i:j]
        assert "icon.ico" in body and "sizes" in body, (
            "校验函数未检查 .ico 的尺寸集合")

    def test_script_squares_non_square_source(self):
        """★ 非正方形源图必须居中裁切而不是拉伸。

        ★★ 必须用 **AST** 判定，不能对源码做子串匹配：
          `square()` 的 docstring 里就写着「不能直接 `resize((s,s))`」
          —— 解释「不要做什么」的文字里必然出现那个词，
          子串判会永远红（本次就先踩了一次）。
          AST 只看真实调用，docstring 不参与。
        """
        import ast
        src = (ROOT / "scripts" / "make_desktop_icons.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "square"),
                  None)
        assert fn is not None, "未找到 square()"
        calls = {n.func.attr for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "crop" in calls, "square() 未做裁切（会变形）"
        assert "resize" not in calls, (
            "square() 里有 resize —— 应先裁成方再缩放，"
            "在 square 内直接 resize 就是拉伸变形")


class TestBootSequence:
    """★★★ 启动时序：**先加载壳层，再起后端**。

    实测（2026-10-11）：我把 `backend.start()` 挂在
    `did-finish-load` 上，而 `did-finish-load` 只有**加载了页面之后**
    才触发 —— 我又没在别处调 `loadApp()`
    ⇒ 互相等待 ⇒ 窗口永远空白、后端永远不启动，
      而**日志里一行错误都没有**（最难查的一类：无错的死锁）。

    实测症状：CDP 里 page 的 `url` 与 `title` 都是空字符串。
    """

    def test_loadapp_called_in_boot(self):
        src = (DESKTOP / "main.js").read_text(encoding="utf-8")
        i = src.index("async function boot()")
        j = src.index("\n}", i)
        body = src[i:j]
        assert "loadApp()" in body, (
            "boot() 里没有 loadApp() ⇒ 页面永不加载 ⇒ "
            "did-finish-load 永不触发 ⇒ 后端永不启动（无错死锁）")

    def test_start_is_not_the_trigger(self):
        """★ `backend.start()` 不能挂在 `did-finish-load` 上做触发源。

        它可以在回调里，但**必须另有 loadApp() 在外层调用**。
        """
        src = (DESKTOP / "main.js").read_text(encoding="utf-8")
        i = src.index("async function boot()")
        j = src.index("\n}", i)
        body = src[i:j]
        k = body.index("loadApp()")
        assert body.rindex("loadApp()") > body.index("did-finish-load"), (
            "loadApp() 必须出现在 did-finish-load 注册**之后**"
            "（否则仍是死锁）")

    def test_no_undeclared_identifier_guard(self):
        """★ 不得写 `undefinedVar && ...` 这种「守卫」。

        未声明的标识符即使在 `&&` 左边也会抛 ReferenceError
        ⇒ 错误信息本身被吞，比没有守卫更糟。

        ★★ 为什么用**正则行扫**而不是 AST：
          Python 的 `ast.parse` 解析不了 JS（对象字面量 `{a: 1}`、
          模板串、中文注释都会报错）。踩过两次：
            · 不剥注释 → `invalid character '·'`
            · 剥了注释 → `{ label: ... }` 仍是 dict 字面量，语法错
          ⇒ 对「找出 `ident &&` 里的 ident」这种需求，
            **按行正则足够**，而且不会引入跨语言解析的脆性。
        """
        import re
        src = (DESKTOP / "main.js").read_text(encoding="utf-8")
        # 已知存在的标识符（require 的模块 + 顶层声明的变量/函数）
        declared = set(re.findall(
            r"(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", src))
        declared |= set(re.findall(
            r"const\s*\{([^}]+)\}", src.replace("\n", " ")[0:2000]
        ) and re.findall(r"([A-Za-z_$][\w$]*)\s*,", src))
        declared |= {
            m for m in re.findall(r"\{([^}]+)\}\s*=\s*require\(", src)
            for m in re.findall(r"([A-Za-z_$][\w$]*)", m)
        }
        # ★ 逐行找 `ident &&`：注释行跳过
        bad = []
        for i, ln in enumerate(src.split("\n"), 1):
            code = ln.split("//", 1)[0]
            if not code.strip():
                continue
            for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*&&", code):
                name = m.group(1)
                if name in {"typeof", "function", "return"}:
                    continue
                if name not in declared:
                    bad.append((i, name))
        assert not bad, (
            f"出现未声明标识符的 `&&` 守卫：{bad} ⇒ 会抛 ReferenceError "
            f"并吞掉错误信息（已声明：{sorted(declared)[:14]}…）")
        # 启动失败必须留日志
        assert "console.error" in src, "启动失败必须写日志"


class TestPackagingActuallyContainsStuff:
    """★★★「打包成功」≠「东西打进去了」

    实测（2026-10-11）：electron-builder **生成了 74MB 的 exe**、
    退出码 0、日志里只有一行
        `file source doesn't exist  from=C:/tmp/data`
    而 `resources/` 里**只有 `app.asar` 与 `elevate.exe`**
    —— **没有 Python 运行时、没有数据库、没有后端代码**。
    ⇒ 装到任何一台没装 Python 的机器上都会直接启动失败，
      而打包流程「成功」。

    ★ 这属于最坏的一类假成功：**产出了、没报错、但不能用**。
    ⇒ 判据改成「构建配置指向的源路径必须真实存在」，
      而不只是「配置里有这几项」。
    """

    def test_extra_resources_exist(self):
        import json
        pkg = json.loads((DESKTOP / "package.json").read_text(
            encoding="utf-8"))
        items = pkg["build"]["extraResources"]
        assert items, "未配置 extraResources ⇒ 包里没有后端与运行时"
        for it in items:
            src = Path(it["from"])
            assert src.exists(), (
                f"extraResources 的 from 不存在：{src}\n"
                f"  ⇒ 打包会**静默跳过**（只打一行日志），"
                f"产出没有该文件的 exe\n"
                f"  提示：electron-builder 解析 from 时基准目录不直观，"
                f"实测 `../backend` 会被算成 <上上级>/backend。"
                f"建议直接用绝对路径。")

    def test_runtime_python_present(self):
        """★ Python 运行时必须有可执行文件。"""
        rt = ROOT / "runtime" / "Scripts" / "python.exe"
        assert rt.exists(), (
            "runtime/ 未生成 —— 先跑 scripts/build_runtime.py，"
            "否则打包出来的应用无法启动（用户机器没有 Python）")

    def test_db_present(self):
        """★ 数据库文件必须存在。"""
        assert (ROOT / "data" / "hawk.duckdb").exists(), (
            "缺少行情数据库，打包后是空壳")
        assert (ROOT / "data" / "hawk.sqlite").exists(), (
            "缺少 SQLite 侧库（自选/持仓/语料会读不到）")

    def test_main_js_points_at_resources_runtime(self):
        """★ `main.js` 的运行时路径必须与 `extraResources` 的 `to` 一致。

        两处各写各的 ⇒ 一个指 `resources/runtime`、另一个打到别处，
        打包成功但运行时找不到 Python。
        """
        import json
        src = (DESKTOP / "main.js").read_text(encoding="utf-8")
        pkg = json.loads((DESKTOP / "package.json").read_text(
            encoding="utf-8"))
        targets = {i["to"].rstrip("/") for i in pkg["build"]["extraResources"]}
        assert "runtime" in targets, "未把 runtime 打进包"
        assert "process.resourcesPath, 'runtime'" in src, (
            "main.js 的 Python 路径与 extraResources 的 to 不一致")
        assert "'app'" in src or "app/backend" in src or "app'," in src, (
            "main.js 未指向 resources/app（后端代码位置）")
