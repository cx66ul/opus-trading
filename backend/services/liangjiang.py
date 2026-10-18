# -*- coding: utf-8 -*-
"""两江算力平台适配层（Hawk 前端 ↔ 平台）

★★★ 定位：只做「读」，不做「下单/建任务」
--------------------------------------------------
Hawk 是**决策副驾**，不接券商下单接口；同理这里也不代替用户
在算力平台上创建/销毁任务 —— 只把平台上的**现状**读出来给前端展示
（配额、任务状态、数据集、数据健康度）。

★ 复用既有客户端，不重写 RuoYi 协议：
  `C:/Users/cxul/WorkBuddy/每日选股以及评估/新闻利好利空系统/liangjiang_client.py`
  重写一套认证逻辑只会产生两套口径（项目已吃过这个亏）。

★ token 来源有二，各自都可能失效，**必须分别处理**：
  ① `data/.lj_token.json`（客户端自己的缓存，`expire_at` 可能为 None）
  ② 浏览器 localStorage（`czone-web-antd-*-prod-core-access.accessToken`）
  本模块读①；② 由 `scripts/sync_liangjiang_token.py` 同步过来。
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CST = timezone(timedelta(hours=8))

#: 客户端所在目录（另一个工作区，不在本项目内）
LJ_DIR = Path(r"C:/Users/cxul/WorkBuddy/每日选股以及评估/新闻利好利空系统")
TOKEN_FILE = LJ_DIR / "data" / ".lj_token.json"
CONFIG_FILE = LJ_DIR / "config.json"

# ★★ 本机实测（2026-10-11）：必须 `trust_env=False` / 绕过代理。
#   沙箱的 HTTP_PROXY 会把内网地址也走代理 ⇒ 502/连接被拒，
#   而浏览器能正常打开 ⇒ 极易误判成「平台挂了」。
_NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}))


class PlatformUnavailable(RuntimeError):
    """平台不可达或登录态失效 —— 与「平台返回空数据」严格区分。"""


def _client():
    """import 既有客户端。★ 不在本项目内重复实现认证。"""
    if str(LJ_DIR) not in sys.path:
        sys.path.insert(0, str(LJ_DIR))
    try:
        from liangjiang_client import LiangjiangClient      # type: ignore
    except Exception as e:                       # noqa: BLE001
        raise PlatformUnavailable(
            f"找不到两江客户端（{LJ_DIR}/liangjiang_client.py）：{e}") from e
    return LiangjiangClient


def load_config() -> dict:
    """平台配置。**密钥/密码永不回传前端** —— 只取非敏感字段。"""
    if not CONFIG_FILE.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))["liangjiang"]
    except Exception as e:                       # noqa: BLE001
        raise PlatformUnavailable(f"读 config.json 失败：{e}") from e
    return cfg


def token_status() -> dict:
    """token 状态。★ `expire_at` 可能为 None（长期 token，无 exp 字段）。

    实测（2026-10-11）：浏览器给的 JWT payload 只有
    `loginType/rnStr/tenantId/userId`，**没有 exp**。
    ⇒ 此时**不能**说「永不过期」，只能说「未携带过期字段」。
    """
    if not TOKEN_FILE.exists():
        return {"present": False, "valid": False,
                "reason": "无 token 缓存，需先登录平台"}
    try:
        obj = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception as e:                       # noqa: BLE001
        return {"present": True, "valid": False,
                "reason": f"token 文件损坏：{type(e).__name__}"}
    exp = obj.get("expire_at")
    if exp is None:
        return {"present": True, "valid": True, "expire_at": None,
                "note": "token 未携带过期字段，有效性以接口实际返回为准"}
    import time
    left = exp - time.time()
    return {"present": True, "valid": left > 60,
            "expire_at": datetime.fromtimestamp(
                exp, CST).isoformat(timespec="seconds"),
            "left_hours": round(left / 3600, 1)}


def probe() -> dict:
    """只读探测：平台是否可达、登录态是否有效、有哪些配额/任务。

    ★ 返回结构必须区分三态，不能糊在一起：
      · `unreachable` —— 网络不通（内网/VPN 没连）
      · `unauthorized` —— 能连但登录态失效
      · `ok` —— 能连且登录态有效
    前端据此显示「连不上」还是「要重新登录」，而不是一律报 500。
    """
    cfg = load_config()
    base = cfg.get("api_base", "").rstrip("/")
    if not base:
        return {"state": "unconfigured",
                "message": "config.json 未配置 liangjiang.api_base"}
    ts = token_status()
    if not ts.get("valid"):
        return {"state": "unauthorized", "token": ts,
                "message": ts.get("reason", "token 不可用")}

    try:
        Client = _client()
        c = Client()
        c.token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["token"]
        c.base = base
        c.client_id = cfg.get("client_id", "")
    except PlatformUnavailable as e:
        return {"state": "unconfigured", "message": str(e)}
    except Exception as e:                       # noqa: BLE001
        return {"state": "unreachable",
                "message": f"{type(e).__name__}: {e}"}

    try:
        quotas = c.quotas()
    except Exception as e:                       # noqa: BLE001
        return {"state": "unreachable",
                "message": f"读配额失败 {type(e).__name__}: {e}"}

    # 真实端点探针：任务列表能出200 才算登录态真的有效
    try:
        tasks = c.tasks()
        task_rows = (tasks or {}).get("rows") or []
        task_state = "ok"
    except Exception as e:                       # noqa: BLE001
        task_rows, task_state = [], f"fail:{type(e).__name__}"

    return {
        "state": "ok",
        "base": base,
        "tenant_id": cfg.get("tenant_id", ""),
        "token": ts,
        "quota_total": (quotas or {}).get("total", 0),
        #★ 只回聚合数，不回整个原始行（可能含 id/资源名等内部信息）
        "quota_summary": [
            {"name": r.get("quotaName"),
             "cpu": r.get("totalCpu"), "mem_gb": r.get("totalMemory"),
             "gpu": r.get("totalGpu")}
            for r in (quotas or {}).get("rows", [])],
        "task_total": len(task_rows),
        "task_state": task_state,
        "checked_at": datetime.now(CST).isoformat(timespec="seconds"),
    }
