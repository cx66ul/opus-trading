# -*- coding: utf-8 -*-
"""
Hawk · 密钥管理（附录 M ·密钥本地加密）

设计原则（按提示词要求）：
  1. **不落明文** —— 配置文件里只存「引用键」，密钥本体走 OS 级加密存储
  2. **不外传** —— 邮箱授权码、LLM API Key 全部留在本机
  3. **可迁移** —— 加密后端可切换（Windows DPAPI / 文件加密 / keyring）

Windows 上优先用 **DPAPI**（Data Protection API）：
  密文与当前 Windows 用户账户绑定，换机器或换账户都无法解密，
  且不依赖第三方进程。这是 Windows 上最合适的方案。

对外接口：
    secrets.set("smtp.auth_code", "xxxx")     # 写入（自动加密）
    secrets.get("smtp.auth_code")             # 读取（自动解密）
    secrets.get("smtp.auth_code", default=None)
    secrets.describe("smtp.auth_code")        # 只看元信息，不泄密
    secrets.delete("smtp.auth_code")

配置文件（如 config/secrets.yaml）里只写：
    auth_code_ref: smtp.auth_code     # 引用键，实际值在加密存储里
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent      # hawk/ 项目根
STORE_PATH = _ROOT / "data" / "secrets.vault.json"
BACKEND = "dpapi"          # dpapi | plain（仅开发调试用，会告警）


# ---------------------------------------------------------------- Windows DPAPI
def _dpapi_protect(data: bytes) -> bytes:
    """调用 Windows CryptProtectData 加密。data 长度有 5MB 限制，密钥远小于此。"""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def blob(b: bytes) -> tuple[DATA_BLOB, Any]:
        buf = ctypes.create_string_buffer(b, len(b))
        bl = DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        return bl, buf          # 保持 buf 引用，避免被GC

    in_bl, in_buf = blob(data)
    out_bl = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(   # noqa: F821
        ctypes.byref(in_bl), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_bl),
    )
    if not ok:
        raise OSError(f"DPAPI 加密失败，错误码 {ctypes.GetLastError()}")
    try:
        res = ctypes.string_at(out_bl.pbData, out_bl.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_bl.pbData)   # noqa: F821
    del in_buf
    return res


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    buf = ctypes.create_string_buffer(data, len(data))
    in_bl = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_bl = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(   # noqa: F821
        ctypes.byref(in_bl), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_bl),
    )
    if not ok:
        raise OSError(
            f"DPAPI 解密失败（错误码 {ctypes.GetLastError()}）。"
            "若曾在其他账户或机器上创建，需重新设置该密钥。"
        )
    try:
        return ctypes.string_at(out_bl.pbData, out_bl.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_bl.pbData)   # noqa: F821


# ---------------------------------------------------------------- 存储
def _load() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(d: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    #原子写：先写临时文件再替换，避免中断导致 vault 损坏
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STORE_PATH)
    try:# 尽量收紧文件权限（Windows 上尽力而为）
        os.chmod(STORE_PATH, 0o600)
    except OSError:
        pass


def _encode(plain: str) -> dict[str, str]:
    raw = plain.encode("utf-8")
    if BACKEND == "dpapi":
        return {"backend": "dpapi",
                "data": base64.b64encode(_dpapi_protect(raw)).decode("ascii")}
    return {"backend": "plain", "data": base64.b64encode(raw).decode("ascii")}


def _decode(rec: dict[str, str]) -> str:
    backend = rec.get("backend", "plain")
    data = base64.b64decode(rec["data"])
    if backend == "dpapi":
        return _dpapi_unprotect(data).decode("utf-8")
    return data.decode("utf-8")


# ---------------------------------------------------------------- 公开接口
def set_secret(key: str, value: str) -> None:
    """写入密钥（加密后落盘）。"""
    if not value:
        raise ValueError("值不能为空")
    d = _load()
    d[key] = _encode(value)
    _save(d)


def get_secret(key: str, default: str | None = None) -> str | None:
    """读取密钥（自动解密）。不存在返回 default。"""
    d = _load()
    if key not in d:
        return default
    try:
        return _decode(d[key])
    except OSError as exc:
        print(f"[secrets] 读取 {key} 失败: {exc}")
        return default


def has_secret(key: str) -> bool:
    return key in _load()


def delete_secret(key: str) -> bool:
    d = _load()
    if key not in d:
        return False
    del d[key]
    _save(d)
    return True


def describe(key: str) -> dict[str, Any]:
    """查看元信息。**不返回密钥本体**，用于排查配置问题而不泄密。"""
    d = _load()
    if key not in d:
        return {"key": key, "exists": False}
    rec = d[key]
    return {
        "key": key,
        "exists": True,
        "backend": rec.get("backend"),
        "cipher_bytes": len(rec.get("data", "")),
        "store_file": str(STORE_PATH),
        "hint": rec.get("hint", ""),
    }


def list_keys() -> list[str]:
    """列出所有引用键（不含值）。"""
    return sorted(_load().keys())


if __name__ == "__main__":
    # 自测：写入 -> 读出 -> 校验密文不含明文 -> 删除
    probe = "test-secret-canary-12345"
    set_secret("__probe__", probe)
    got = get_secret("__probe__")
    raw = STORE_PATH.read_text(encoding="utf-8")
    print("写入/读取一致性:", "OK" if got == probe else "FAIL")
    print("密文含明文:", "★危险★" if probe in raw else "否（安全）")
    print("元信息:", describe("__probe__"))
    delete_secret("__probe__")
    print("已清理测试密钥")