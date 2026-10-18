# -*- coding: utf-8 -*-
"""
Hawk · LLMProvider 抽象（附录 3.3 · 双支持云端/本地 + 自动降级）

D2 决策（2026-10-08 21:19）：**暂时只用本地 7B**。
云端 DeepSeek 仅在抽象里保留接口，当前不接入、不实现 ——
但保留扩展点，避免将来加云端时改动业务代码（铁律一：接口 + 注册表）。

★ 本地优先的两个理由：
  1. 数据不出本机（持仓、个股属敏感信息）
  2. 零调用成本，可无限次重跑 —— 调prompt 不心疼

实现要点：
  · 输出**强制 JSON**，解析失败时重试一次并降级为规则兜底
  · 温度固定为 0：打标任务要可复现，不能有随机性
  · 记录 prompt 版本与模型名，便于回溯「当时用什么打的标」
"""
from __future__ import annotations

import json
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@dataclass
class LLMUsage:
    """单次调用的用量与耗时（规格 2.4 硬性要求 4）。

    ★ 为什么单独抽一个 dataclass 而不是让 chat() 返回元组
    ---------------------------------------------------
    `chat()` 原签名返回 `str`，被 `tag_news()` 与可能存在的其他调用方依赖。
    改成返回 `(str, LLMUsage)` 会**同时改两个接口**（铁律一：接口要稳定）。
    ⇒ 这里保持 `chat() -> str` 不变，用 `self.last_usage` 传递用量：
      「最近一次调用」的语义对同步调用天然成立，且不破坏任何调用方。
    ★ 如果将来并发调用，必须换成显式返回元组 —— last_usage 在并发下会串。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: float = 0.0
    load_ms: float = 0.0          # 模型加载耗时（仅首次推理非零）
    ok: bool = True
    err: str = ""
    raw_meta: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost_cny(self, price_in: float = 0.0, price_out: float = 0.0) -> float:
        """估算成本（元）。**本地模型恒为 0** —— 这是真实成本，不是占位。

        保留入参是为了将来接云端（DeepSeek 等）时同一套代码直接可用；
        本地推理的真实成本是电费与硬件折旧，不摊到单次调用上。
        """
        return (self.prompt_tokens / 1e6 * price_in
                + self.completion_tokens / 1e6 * price_out)


@dataclass(frozen=True)
class NewsTag:
    """单条消息的打标结果（结构化）。

    字段与附录 B.2 的 news 表对齐。
    direction: bull（利好）/ bear（利空）/ neutral（中性）
    strength:  0-5，对应附录 4.1 的消息强度分级
    horizon:   short（当日盘中）/ mid（一周内）/ long（一月以上）
    """

    direction: str
    strength: int
    horizon: str
    category: str
    related_codes: list[str]
    reason: str
    confidence: float
    llm_provider: str = ""
    prompt_ver: str = ""
    raw_output: str = ""

    def to_row(self, ts: str, title: str, content: str, url: str,
               source: str, simhash: str, tags: list[str],
               as_of: datetime) -> dict:
        """转成 news 表的插入行。

        ★ as_of **必填**，不使用 datetime.now() —— 铁律二。
          这里若取系统时间，回测重放时打标时间会与事件时间错乱，
          复盘面板的「消息→信号延迟」将完全失真。
          强制必填而非给默认值，是为了让违规在调用时就暴露，
          而不是悄悄退化成"看起来能跑"的行为。
        """
        return {
            "ts": ts,
            "source": source,
            "sources": source,
            "title": title or "",
            "content": content,
            "url": url or "",
            "simhash": simhash,
            "category": self.category,
            "related_codes": json.dumps(self.related_codes, ensure_ascii=False),
            "direction": self.direction,
            "strength": self.strength,
            "horizon": self.horizon,
            "llm_reason": self.reason,
            "confidence": self.confidence,
            "verified": 0,
            "llm_provider": self.llm_provider,
            "prompt_ver": self.prompt_ver,
            "created_at": as_of.isoformat(timespec="seconds"),
        }


class LLMProvider(ABC):
    """LLM 提供方基类。"""

    name: str = "base"

    #: 最近一次调用的用量（见 LLMUsage 的说明：靠它避免改 chat() 返回类型）
    last_usage: LLMUsage = field(default_factory=LLMUsage)

    @abstractmethod
    def available(self) -> bool:
        """是否可用。不可用时上层会自动降级到下一档。"""

    @abstractmethod
    def chat(self, system: str, user: str, as_of: datetime,
             json_mode: bool = True, temperature: float = 0.0) -> str:
        """对话补全。as_of注入，保证可复现（铁律二）。

        实现方**必须**把用量写进 `self.last_usage`（含 token 与耗时）。
        """

    def audit(self, *, task: str, stage: str, attempt: int, as_of: datetime,
              wallclock: datetime, usage: LLMUsage | None = None) -> None:
        """写一条调用审计（规格 2.4 要求 4 / M3）。

        ★ `wallclock` 由调用方传入而非在这里取 —— 与本项目其余代码一致的纪律：
          墙钟只在**入口脚本**取一次然后往下传，领域层不碰系统时间。
        ★ 审计写失败**绝不影响主流程**（LLM 已经返回了结果，
          不能因为日志表出问题就把打标结果丢掉）。
        """
        u = usage if usage is not None else self.last_usage
        try:
            from backend.data.sqlite import log_llm_audit

            log_llm_audit({
                "ts": wallclock.isoformat(timespec="seconds"),
                "as_of": as_of.isoformat(timespec="seconds"),
                "task": task,
                "provider": self.name,
                "model": getattr(self, "model", ""),
                "prompt_ver": PROMPT_VERSION,
                "ok": 1 if u.ok else 0,
                "attempt": attempt,
                "stage": stage,
                "elapsed_ms": round(u.elapsed_ms, 1),
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "cost_cny": round(u.cost_cny(), 6),
                "err": (u.err or "")[:300],
                "extra": json.dumps({"load_ms": round(u.load_ms, 1),
                                     "total_tokens": u.total_tokens},
                                    ensure_ascii=False),
            })
        except Exception as exc:                # pragma: no cover
            logger.warning(f"[{self.name}] 审计写入失败（不影响主流程）: {exc}")

    def tag_news(self, content: str, as_of: datetime,
                 candidate_codes: list[str] | None = None,
                 wallclock: datetime | None = None) -> NewsTag:
        """给单条消息打标。含 JSON 解析、重试、规则兜底。

        candidate_codes 为本地股票代码全集，LLM 输出的代码必须在此集合内，
        否则丢弃 —— 这是附录 4.6 明确要求的「代码本地化校验，防幻觉」。

        ★ `wallclock` 用于审计表的墙钟列。**只在入口脚本取一次**，
          领域层不自己调 now()（铁律二）。
        """
        system = SYSTEM_PROMPT
        user = build_user_prompt(content, candidate_codes or [])
        raw = ""
        # 墙钟：入口未给就退回 as_of —— 宁可与业务时点相同，也不要在领域层取系统时间
        wc = wallclock or as_of
        for attempt in range(1, 3):
            try:
                raw = self.chat(system, user, as_of, json_mode=True, temperature=0.0)
                parsed = extract_json(raw)
                if parsed:
                    self.audit(task="news_classify", stage="ok", attempt=attempt,
                               as_of=as_of, wallclock=wc)
                    return self._to_tag(parsed, content, raw, candidate_codes or [])
                logger.warning(f"[{self.name}] JSON 解析失败，第 {attempt} 次，重试")
                self.last_usage.ok = False
                self.last_usage.err = "JSON 解析失败"
                self.audit(task="news_classify", stage="parse_fail", attempt=attempt,
                           as_of=as_of, wallclock=wc)
            except Exception as exc:
                logger.warning(f"[{self.name}] 调用失败第 {attempt} 次: {exc}")
                self.last_usage = LLMUsage(ok=False, err=str(exc))
                self.audit(task="news_classify", stage="call_fail", attempt=attempt,
                           as_of=as_of, wallclock=wc)
                if attempt == 1:
                    time.sleep(1.0)
        # 兜底：规则版，不静默丢弃消息
        logger.warning(f"[{self.name}] LLM 不可用，降级为规则打标")
        # ★★ 必须传**显式空的** LLMUsage，不能用 `self.last_usage`。
        #   last_usage 里还留着**上一次真实调用**的 token 与耗时
        #   （实测：一次 450/50 token、2071ms 的调用之后走 fallback，
        #     审计行会把这些数原样记到 fallback 上）
        #   ⇒ 成本统计会**虚报**，而且是每一条降级都虚报 —— 数字看着合理，
        #     没有任何报错，只有对着账单才发现。
        #   正确语义：fallback 没调模型 ⇒ token=0、耗时≈0（规则匹配确实花了时间，
        #   但那是微秒级，放进 elapsed_ms 反而混淆「LLM 耗时」这个指标的含义）。
        self.audit(task="news_classify", stage="fallback", attempt=3,
                   as_of=as_of, wallclock=wc,
                   usage=LLMUsage(ok=False, err="规则兜底，未调用模型"))
        return rule_based_fallback(content, self.name)

    def _to_tag(self, d: dict, content: str, raw: str,
                valid_codes: list[str]) -> NewsTag:
        """校验并归一 LLM 输出。**所有非法值一律丢弃并用默认值兜底**。"""
        direction = str(d.get("direction", "neutral")).lower()
        if direction not in ("bull", "bear", "neutral"):
            direction = "neutral"

        try:
            strength = int(d.get("strength", 1))
        except (TypeError, ValueError):
            strength = 1
        strength = max(0, min(5, strength))          # 附录 4.1 定义 0-5

        horizon = str(d.get("horizon", "short")).lower()
        if horizon not in ("short", "mid", "long"):
            horizon = "short"

        try:
            conf = float(d.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))

        # ★ 名称 → 代码映射（v1.1 核心改动）。
        #   模型输出 related_names（公司名/简称），由本地 stock_index 映射成代码：
        #   · 命中 -> 保留（别名表 5948 条，含品牌简称与 ST 前缀剥离）
        #   · 未命中 -> 丢弃（模型编造的公司名，或美股/债券等非A股标的）
        #   模型不需要记代码（它记不准：说「茅台」会给 002049），本地映射 100% 准确。
        #   仍兼容旧的 related_codes 字段（万一模型照旧格式输出）。
        names = d.get("related_names") or []
        raw_codes = d.get("related_codes") or []
        if isinstance(names, str):
            names = [names]
        if isinstance(raw_codes, str):
            raw_codes = [raw_codes]

        valid = set(valid_codes)
        checked: list[str] = []
        unresolved: list[str] = []

        from backend.domain.stock_index import get_index
        idx = get_index()
        for n in names:
            code = idx.lookup(str(n)) or idx.resolve_code_or_name(str(n))
            if code and code in valid:
                checked.append(code)
            else:
                unresolved.append(str(n))
        # 兼容旧格式：直接给的代码仍走「在库中」校验
        n_reject = 0
        for c in raw_codes:
            code = idx.resolve_code_or_name(str(c))
            if code and code in valid:
                checked.append(code)
            else:
                n_reject += 1

        # 去重保序
        checked = list(dict.fromkeys(checked))
        if unresolved:
            logger.info(f"[{self.name}] {len(unresolved)} 个名称未命中本地股票库"
                        f"（丢弃）: {unresolved[:6]}")
        if n_reject:
            logger.info(f"[{self.name}] 丢弃 {n_reject} 个不存在的代码（防幻觉）")

        return NewsTag(
            direction=direction,
            strength=strength,
            horizon=horizon,
            category=str(d.get("category", "其他"))[:20],
            related_codes=checked,
            reason=str(d.get("reason", ""))[:300],
            confidence=conf,
            llm_provider=self.name,
            prompt_ver=PROMPT_VERSION,
            raw_output=raw[:500],
        )


# ------------------------------------------------------------------ Ollama
class OllamaProvider(LLMProvider):
    """本地 Ollama 后端（D2 决策的默认实现）。"""

    name = "ollama"

    def __init__(self, model: str = "hawk-qwen2.5-7b",
                 host: str = "http://127.0.0.1:11434",
                 timeout: float = 120.0) -> None:
        # ★ 默认模型名 = 实际导入 ollama 的名字。
        #   `ollama pull` 在本机网络下不可用（分片下载全卡死），
        #   故走「ModelScope 直下 GGUF → llama-gguf-split --merge → ollama create」，
        #   导入时命名为 hawk-qwen2.5-7b（详见 scripts/setup_model.py + merge_gguf.py）。
        #   实测：首次推理 83s（模型加载），后续 0.7s。
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        try:
            import httpx

            r = httpx.get(f"{self.host}/api/tags", timeout=5.0,
                          trust_env=False)     # 本地服务，不走代理
            if r.status_code != 200:
                return False
            names = [m.get("name", "") for m in r.json().get("models", [])]
            hit = any(n == self.model or n.split(":")[0] == self.model.split(":")[0]
                      for n in names)
            if not hit:
                logger.warning(
                    f"[ollama] 服务在线但模型 {self.model} 未就绪。"
                    f"请执行： ollama pull {self.model}"
                )
            return hit
        except Exception as exc:
            logger.warning(f"[ollama] 不可用: {exc}")
            return False

    def chat(self, system: str, user: str, as_of: datetime,
             json_mode: bool = True, temperature: float = 0.0) -> str:
        import httpx

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            # ★ 温度固定 0：打标要可复现。as_of 放进 prompt 上下文，
            #   但不改变采样随机性（temperature=0 已足够）
            "options": {"temperature": temperature, "num_predict": 512},
        }
        if json_mode:
            payload["format"] = "json"

        # ★ 这里读墙钟：LLM 推理耗时是物理事实，as_of 冒充不了。
        #   与领域层的区别是「测时间」而不是「决定业务时点」——
        #   审计表 ts 列记录的正是这件事（见 sqlite.py DDL 注释）。
        t0 = time.perf_counter()
        try:
            r = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout,
                           trust_env=False)
            r.raise_for_status()
        except Exception as exc:
            self.last_usage = LLMUsage(
                ok=False, err=str(exc),
                elapsed_ms=(time.perf_counter() - t0) * 1000.0)
            raise

        data = r.json()
        # ★ token 字段名是**实测得来**的，不是照文档抄的
        #   （`prompt_eval_count` = 输入 token，`eval_count` = 输出 token）。
        #   所有 duration 字段单位是**纳秒** ⇒ 必须 /1e6 才是毫秒。
        #   ⚠ load_duration 只在首次推理非零（实测 9.3s），后续约 0
        self.last_usage = LLMUsage(
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            load_ms=float(data.get("load_duration") or 0) / 1e6,
            ok=True,
            raw_meta={
                "done_reason": data.get("done_reason"),
                "total_duration_ms": float(data.get("total_duration") or 0) / 1e6,
                "prompt_eval_cached_count": data.get("prompt_eval_cached_count"),
            },
        )
        return data["message"]["content"]

    def connectivity_check(self, as_of: datetime) -> dict:
        """LLM 连通性自检（规格 2.4 硬性要求 5「一键测试 provider」）。

        ★ 返回**结构化结果**而不是 bool：自检页面要展示耗时与 token，
          只回一个 True/False 会让「慢但可用」和「快且可用」无法区分。
        ★ 这里**真的发一次请求**（不只打 /api/tags）——
          「服务在线但模型加载失败」「模型在但显存不足」这两种情况
          只有真推理才暴露。
        """
        t0 = time.perf_counter()
        out = {"provider": self.name, "model": self.model,
               "reachable": False, "model_loaded": False,
               "infer_ok": False, "elapsed_ms": 0.0, "err": ""}
        try:
            import httpx

            out["reachable"] = httpx.get(f"{self.host}/api/tags", timeout=5.0,
                                         trust_env=False).status_code == 200
        except Exception as exc:
            out["err"] = f"服务不可达: {exc}"
            out["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
            return out

        out["model_loaded"] = self.available()
        if not out["model_loaded"]:
            out["err"] = out["err"] or "服务在线但模型未就绪"
        else:
            #★ 这里**不要**再单独探一次 /api/tags 来填 reachable ——
            #   `available()` 内部已经探过且成功了，把它写成 False 会输出
            #   「服务不可达 但 模型已就绪」这种自相矛盾的结果
            #   （实测踩过：reachable=False / model_loaded=True）。
            #   判据记忆：**同一个事实只探一次**，否则两份探法必然不一致。
            out["reachable"] = True
        try:
            self.chat("只输出 JSON", '输出 {"ok":1}', as_of,
                      json_mode=True, temperature=0.0)
            out["infer_ok"] = True
        except Exception as exc:
            out["err"] = f"推理失败: {exc}"
        out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        return out


# ------------------------------------------------------------------ Prompt
PROMPT_VERSION = "v1.1-20261009"

SYSTEM_PROMPT = """你是A股消息面分析助手。给定一条财经快讯，输出 JSON，字段如下：

{
  "direction": "bull" | "bear" | "neutral",   // 对A股整体情绪的影响方向
  "strength": 0-5,                            // 0=无影响 5=重大
  "horizon": "short"|"mid"|"long",            // 影响时效：当日/一周/一月以上
  "category": "宏观政策"|"行业"|"公司"|"海外"|"市场"|"其他",
  "related_names": ["青岛港"],   // 消息中明确提到的A股公司名称
  "reason": "一句话理由，不超过50字"
}

判断规则：
1. direction 看对A股**整体情绪**的影响，不是对单只股票。
   涉及他国经济、普通海外新闻 → neutral 或 bull/bear 但 strength<=2
2. strength 评分：
   5=央行/国常会级重大政策、重大地缘冲突升级
   4=行业政策、部委表态、上市公司重大事项
   3=重要行业数据、财报超预期
   2=一般公司新闻、分析师观点
   1=行业动态  0=与A股无关闲聊
3. ★ related_names 只写**消息里真实出现的公司名称**（可简称，如「青岛港」
   「茅台」），不要写代码、不要凭记忆补充消息没提到的公司。
   这条很重要：模型最容易在这里编造不存在的公司，下游会用本地股票库校验丢弃。
   只输出完整 JSON，不要任何解释文字。"""


def build_user_prompt(content: str, candidate_codes: list[str]) -> str:
    """构造用户 prompt。

    ★ v1.1 关键修正（实测踩坑）：原prompt 只给 800 个**代码**不给名称，
      模型无法把消息里的「青岛港」对上「601298」，于是按「无法确定→给[]」
      的规则返回空列表 —— 实测 6/6 条关联个股全空，青岛港这种明确提到的
      公司也识别不出。
      改为：让模型输出**公司名称**，由本地 stock_index（5948 个别名）
      做名称→代码映射。模型不需要记代码（它记不准），本地映射 100% 准确。
      candidate_codes 仍传入，仅用于校验该代码是否在库中。
    """
    return (
        f"快讯内容：\n{content[:600]}\n\n"
        f"请提取这段快讯中**明确提到**的 A 股上市公司名称，"
        f"填入 related_names（用消息里的写法，如「青岛港」；"
        f"消息没提到公司就给空数组）。\n\n"
        f"按 system 提示输出 JSON。"
    )


# ------------------------------------------------------------------ 解析
def extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON。

    7B 模型常带多余文字（如 ```json 包裹或前后解释），
    因此用括号配平法截取第一个完整 JSON 对象，而不是简单正则。
    """
    if not text:
        return None
    t = text.strip()
    # 去掉 markdown 代码围栏
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # 花括号配平截取
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ------------------------------------------------------------------ 规则兜底
def rule_based_fallback(content: str, provider: str) -> NewsTag:
    """LLM 不可用时的规则兜底。

    ★ 关键设计：**不静默丢弃消息**。
      没有 LLM 也返回结构化结果（strength=0、direction=neutral），
      这样下游能区分「已分析但无关」与「没分析过」。
    """
    c = content or ""
    bull_kw = ("降准", "降息", "利好", "增持", "回购", "超预期", "中标", "获批", "提振")
    bear_kw = ("暴跌", "利空", "减持", "处罚", "退市", "低于预期", "违约", "制裁",
               "调查", "亏损", "下调")
    bull = [k for k in bull_kw if k in c]
    bear = [k for k in bear_kw if k in c]
    if bull and not bear:
        direction, strength = "bull", min(3, len(bull))
    elif bear and not bull:
        direction, strength = "bear", min(3, len(bear))
    elif bull and bear:
        direction, strength = "neutral", 2
    else:
        direction, strength = "neutral", 0
    return NewsTag(
        direction=direction, strength=strength, horizon="short",
        category="其他", related_codes=[],
        reason=f"规则兜底（{provider} 不可用）" + (f" 命中:{bull or bear}" if bull or bear else " 无关键词"),
        confidence=0.3, llm_provider=f"{provider}_fallback",
        prompt_ver=PROMPT_VERSION,
    )


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("=== Ollama 可用性 ===")
    p = OllamaProvider()
    ok = p.available()
    print(f"  模型 {p.model}: {'就绪' if ok else '未就绪'}")

    print("\n=== JSON 提取鲁棒性（7B 模型常带多余文字）===")
    cases = [
        ('{"direction":"bull","strength":3}', "纯净 JSON"),
        ('```json\n{"direction":"bear","strength":2}\n```', "markdown 围栏"),
        ('好的，分析如下：\n{"direction":"neutral","strength":1,"reason":"x"}\n以上。', "前后夹带文字"),
        ('{"reason":"含}括号与\\"引号","strength":4}', "含特殊字符"),
        ("完全不是 JSON", "非 JSON"),
    ]
    for raw, desc in cases:
        got = extract_json(raw)
        print(f"  {desc:16s} -> {'成功' if got is not None else '失败'} "
              f"{got if got else ''}")

    print("\n=== 输出校验：非法值一律兜底 ===")
    for bad in [
        {"direction": "暴涨", "strength": 99, "horizon": "永远", "confidence": 5.0},
        {"direction": "bull", "strength": "强", "horizon": "short"},
    ]:
        tag = p._to_tag(bad, "测试", "", [])
        print(f"  输入 {bad}")
        print(f"  -> direction={tag.direction} strength={tag.strength} "
              f"horizon={tag.horizon} confidence={tag.confidence}")

    print("\n=== 防幻觉：不在本地库的代码必须丢弃 ===")
    tag = p._to_tag(
        {"direction": "bull", "strength": 3, "related_codes": ["600519", "999999", "宁德时代"]},
        "测试", "", ["600519", "000001", "300750"],
    )
    print(f"  输入 [600519, 999999, '宁德时代'] 本地库[600519,000001,300750]")
    print(f"  -> 保留 {tag.related_codes}（999999 与中文名被丢弃）")

    print("\n=== 规则兜底（LLM 不可用时）===")
    for text in ["央行宣布降准0.5个百分点", "某公司被证监会调查", "今天天气不错"]:
        t = rule_based_fallback(text, "ollama")
        print(f"  {text[:20]:22s} -> {t.direction:8s} strength={t.strength} {t.reason[:30]}")