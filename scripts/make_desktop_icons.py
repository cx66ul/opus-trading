#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从一张源图生成桌面端全部图标资源。

★ 为什么必须脚本化
--------------------------------------------------
手工截一次图改个文件名，下��人换图标时就会漏掉：
  · `icon.ico` 的 7 个尺寸缺一个 ⇒ 任务栏某处显示成默认图标
  · `tray.png` 用 256px 源 ⇒ 托盘图标在高分屏上糊成一团
  · 源图非正方形时直接拉伸 ⇒ 图标变形
⇒ 一条命令产出全部尺寸，且**校验产物**（不是「生成完就算」）。

用法：
    python scripts/make_desktop_icons.py <源图路径>
    python scripts/make_desktop_icons.py <源图> --out desktop/assets
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("需要 Pillow：pip install pillow")

#: Windows 图标需要的尺寸（缺一个就可能在某个缩放级别显示默认图标）
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
#: 托盘图标：Windows 在 100%/125%/150% 缩放下分别取 16/20/24 逻辑像素
TRAY_SIZES = [16, 20, 24, 32]


def square(im: Image.Image) -> Image.Image:
    """居中裁成正方形。

    ★ 不能直接 `resize((s,s))` —— 源图若非正方形会**变形**，
    而图标变形在高分屏上非常明显。
    """
    w, h = im.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return im.crop((left, top, left + side, top + side))


def build(src: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    im = Image.open(src).convert('RGB')
    sq = square(im)
    made: dict[str, int] = {}

    ico = out_dir / 'icon.ico'
    sq.save(ico, format='ICO', sizes=[(s, s) for s in ICO_SIZES])
    made['icon.ico'] = ico.stat().st_size

    png = out_dir / 'icon.png'
    sq.resize((256, 256), Image.LANCZOS).save(png)
    made['icon.png'] = png.stat().st_size

    for s in TRAY_SIZES:
        name = 'tray.png' if s == max(TRAY_SIZES) else f'tray@{s}.png'
        p = out_dir / name
        sq.resize((s, s), Image.LANCZOS).save(p)
        made[name] = p.stat().st_size
    return made


def verify(out_dir: Path) -> list[str]:
    """★ 生成完必须**校验**，否则「跑完没报错」被当成「产物可用」。

    实测过的两个真实问题：
      1. `.ico` 只写进了一个尺寸 ⇒ 任务栏某些位置显示默认图标
      2. `tray.png` 直接用了 256px 图 ⇒ 托盘在高分屏上糊
    """
    problems = []
    ico = out_dir / 'icon.ico'
    if not ico.exists():
        return ['icon.ico 未生成']
    with Image.open(ico) as im:
        sizes = {s[0] for s in (im.info.get('sizes') or [])}
    missing = [s for s in ICO_SIZES if s not in sizes]
    if missing:
        problems.append(f"icon.ico 缺尺寸 {missing}（实际 {sorted(sizes)}）")

    tray = out_dir / 'tray.png'
    if not tray.exists():
        problems.append('tray.png 未生成')
    else:
        with Image.open(tray) as im:
            if im.size[0] > 64:
                problems.append(
                    f"tray.png 是 {im.size[0]}px，太大会在托盘里糊成一团")
    if not (out_dir / 'icon.png').exists():
        problems.append('icon.png 未生成（electron-builder 需要）')
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description='生成桌面端图标')
    ap.add_argument('src', help='源图路径（jpg/png）')
    ap.add_argument('--out', default='desktop/assets')
    a = ap.parse_args()
    src = Path(a.src)
    if not src.exists():
        sys.exit(f"源图不存在：{src}")
    out = Path(a.out).resolve()

    made = build(src, out)
    for k, v in made.items():
        print(f"  {k:16s} {v:>8,} bytes")

    problems = verify(out)
    if problems:
        print("\n★ 校验未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"\n✓ 图标已生成并校验通过 → {out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
