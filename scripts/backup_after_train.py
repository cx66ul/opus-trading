#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hawk · 两江算力训练后备份（**死纪律**，用户 2026-10-11 指示）

★★★ 为什么这是代码而不是提醒
--------------------------------------------------
用户原话：「每次做完训练都要备份数据作为死纪律」。

「记得备份」不是纪律 —— 记得是会忘的。所以：
  · `submit_training()` **内部强制调用**备份，调用方无法绕过
  · 备份失败 ⇒ 训练任务**整体判失败**（不吞、不降级、不"下次补"）
  · 每份备份写 sha256 清单 ⇒ 事后能验证云端与本地是否一致

★ 与 Hawk 项目本部的 `lj_backup.py` 是两套：
  那个备份的是 Hawk 代码/数据到 oss；
  这个备份的是**训练输入与产物**（样本/脚本/数据集/任务日志）。
  两者对象不同，**不互相替代**。

用法（一般由 submit_training 自动调用，也可单独执行）：
    python scripts/backup_after_train.py --date 20261011
    python scripts/backup_after_train.py --verify 20261011
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger                      # noqa: E402

CST = timezone(timedelta(hours=8))
logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | {message}")

#: 训练侧的工作区（另一个工作区）
LJ_DIR = Path(r"C:/Users/cxul/WorkBuddy/每日选股以及评估/新闻利好利空系统")
DATA = LJ_DIR / "data"
LOCAL_BACKUP = LJ_DIR / "backup_after_train"
#: 备份到 OSS 的对象前缀（与 lj_backup.py 的 backup/<label>/ 不同层）
OSS_PREFIX = "train-backup"

#: **永不备份**的内容：密钥类文件进了备份就是安全事故
SECRET_PATTERNS = ("password", "token", "secret", "credential", ".env")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _client():
    if str(LJ_DIR) not in sys.path:
        sys.path.insert(0, str(LJ_DIR))
    from liangjiang_client import LiangjiangClient      # type: ignore
    return LiangjiangClient


def collect_targets(day: str) -> tuple[list[tuple[str, Path]], dict[str, bytes]]:
    """要备份的文件 + **必须重建**的字节串。

    ★★ 返回 `(文件清单, 字节串清单)` 而不是单一列表——
      `labels-all.jsonl`（训练真正用的那份累积样本）**从来不落盘**，
      它只是 `build_trainset()` 在内存里拼出来的字节串
      ⇒ 只按文件名抓 `data/labels-*`，结果**累积样本根本没被备份**，
        却照样打印「备份成功」（2026-10-11 实测：只备份到 2 个文件）。
      ⇒ 必须**从训练用的同一个函数**取那份字节串：
        「备份的对象」必须等于「训练的对象」，否则备份了另一份东西。
    """
    out: list[tuple[str, Path]] = []
    for pat in (f"labels-{day}.jsonl", f"news-{day}.json",
                "backup-manifest.json"):
        p = DATA / pat
        if p.exists():
            out.append((f"data/{pat}", p))
    script = LJ_DIR / "train_newsclf.py"
    if script.exists():
        out.append(("train_newsclf.py", script))
    # ★ 逐个过滤：文件名里带 token/password 等一律不备份
    safe = []
    for rel, p in out:
        low = p.name.lower()
        if any(k in low for k in SECRET_PATTERNS):
            logger.warning(f"★ 跳过疑似密钥文件：{rel}")
            continue
        safe.append((rel, p))

    # ★ 累积样本：从训练用的函数直接取，保证「备份的 == 训练的」
    blobs: dict[str, bytes] = {}
    try:
        C = _client()
        c = C()
        data, n = c.build_trainset()
        if not n:
            raise RuntimeError("build_trainset() 返回 0 行样本")
        blobs["labels-all.jsonl"] = data
        logger.info(f"累积样本 {n} 行 / {len(data)} 字节（取自 build_trainset）")
    except Exception as e:                       # noqa: BLE001
        #★ 取不到就**明确失败**，不能「备份一部分也算成功」
        raise RuntimeError(
            f"无法取得累积训练样本：{type(e).__name__}: {e}") from e
    return safe, blobs


def backup_local(day: str) -> dict:
    """① 本地副本 —— 即使 OSS 挂了也要有。"""
    dest = LOCAL_BACKUP / day
    dest.mkdir(parents=True, exist_ok=True)
    files, blobs = collect_targets(day)
    manifest = []
    for rel, src in files:
        blob = src.read_bytes()
        dst = dest / Path(rel).name
        dst.write_bytes(blob)
        manifest.append({"file": Path(rel).name, "size": len(blob),
                         "sha256": _sha256(blob), "source": str(src)})
    for name, blob in blobs.items():
        (dest / name).write_bytes(blob)
        manifest.append({"file": name, "size": len(blob),
                         "sha256": _sha256(blob),
                         "source": "build_trainset()（训练实际使用的同一份）"})
    # ★★★ **缺件即失败**：死纪律要求「备份完整」。
    #   只备份到 2 个文件（丢了累积样本）却打印「备份成功」，
    #   等于把纪律变成了形式 —— 上面那次实测就是这样蒙混过去的。
    if not manifest:
        raise RuntimeError(f"{day} 没有任何可备份内容 —— 拒绝报成功")
    must = {"labels-all.jsonl"}
    missing = must - {m["file"] for m in manifest}
    if missing:
        raise RuntimeError(
            f"备份不完整，缺 {missing}（训练实际使用的样本未备份）—— 拒绝报成功")
    mf = dest / "manifest.json"
    mf.write_text(json.dumps(
        {"day": day, "at": datetime.now(CST).isoformat(timespec="seconds"),
         "files": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"dir": str(dest), "n_files": len(manifest)}


def backup_oss(day: str) -> dict:
    """② 云端副本 —— 上传到训练用的同一个 bucket。

    ★ 为什么必须用**训练用的那个 bucket**：
      备份与训练数据放同一个存储，Pod 挂载即可自取；
      分开放则「数据丢了才发现备份也在同一台机器」。
    """
    try:
        C = _client()
        c = C()
        tok_file = DATA / ".lj_token.json"
        if tok_file.exists():
            c.token = json.loads(tok_file.read_text(
                encoding="utf-8"))["token"]
        bucket = (c.cfg.get("train") or {}).get("bucket")
        if not bucket:
            return {"ok": False, "reason": "未配置 train.bucket"}
        manifest = []
        dest = LOCAL_BACKUP / day
        for f in sorted(dest.glob("*")):
            if f.name == "manifest.json" or not f.is_file():
                continue
            blob = f.read_bytes()
            obj = f"{OSS_PREFIX}/{day}/{f.name}"
            r = c.upload_stream(bucket, obj, blob,
                                "application/octet-stream")
            manifest.append({"object": f"oss://{bucket}/{obj}",
                             "ok": r.get("code") == 200,
                             "size": len(blob), "sha256": _sha256(blob)})
        out = {"ok": all(m["ok"] for m in manifest), "bucket": bucket,
               "n": len(manifest)}
        if not out["ok"]:
            out["failed"] = [m for m in manifest if not m["ok"]]
        (dest / "oss-manifest.json").write_text(json.dumps(
            {"day": day, "at": datetime.now(CST).isoformat(timespec="seconds"),
             **out, "objects": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return out
    except Exception as e:                        # noqa: BLE001
        #★ 失败**必须上抛**，不能「记录后继续」——
        #   备份失败而任务报成功 = 纪律形同虚设。
        raise RuntimeError(f"OSS 备份失败：{type(e).__name__}: {e}") from e


def verify(day: str) -> dict:
    """事后核对：本地副本的 sha256 是否与源一致。"""
    dest = LOCAL_BACKUP / day
    mf = dest / "manifest.json"
    if not mf.exists():
        return {"ok": False, "reason": f"没有 {day} 的备份清单"}
    m = json.loads(mf.read_text(encoding="utf-8"))
    bad = []
    for f in m["files"]:
        p = dest / f["file"]
        if not p.exists():
            bad.append({"file": f["file"], "why": "文件缺失"})
        elif _sha256(p.read_bytes()) != f["sha256"]:
            bad.append({"file": f["file"], "why": "sha256 不符"})
    return {"ok": not bad, "day": day, "n": len(m["files"]), "bad": bad}


def run(day: str) -> dict:
    """完整备份流程。本地 + OSS，任一失败即抛错。"""
    loc = backup_local(day)
    logger.info(f"本地备份：{loc['dir']}（{loc['n_files']} 个文件）")
    oss = backup_oss(day)
    if not oss.get("ok"):
        raise RuntimeError(f"OSS 备份未全部成功：{oss}")
    logger.info(f"OSS 备份：bucket={oss['bucket']}，{oss['n']} 个对象")
    v = verify(day)
    if not v["ok"]:
        raise RuntimeError(f"备份自检不通过：{v}")
    logger.info(f"备份自检通过（{v['n']} 个文件 sha256 一致）")
    return {"ok": True, "day": day, "local": loc, "oss": oss, "verify": v}


def main() -> int:
    ap = argparse.ArgumentParser(description="训练后备份（死纪律）")
    ap.add_argument("--date", default=datetime.now(CST).strftime("%Y%m%d"))
    ap.add_argument("--verify", action="store_true", help="只做核对，不备份")
    a = ap.parse_args()
    try:
        if a.verify:
            print(json.dumps(verify(a.date), ensure_ascii=False, indent=2))
            return 0
        print(json.dumps(run(a.date), ensure_ascii=False, indent=2))
        return 0
    except Exception as e:                        # noqa: BLE001
        #★ 顶层也必须**明确失败**（退出码 1），不能吞
        logger.error(f"备份失败（训练应判为未完成）：{e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
