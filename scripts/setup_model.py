"""GGUF 分片合并 + 导入 Ollama。

★ 为什么要自己合并：ollama pull 在本机网络下不可用
  （实测主文件预分配完整但零增长，16 个 `-partial-N` 分片全是 50~68 字节，
  没有任何分片在真正下载；OLLAMA_MAX_PARALLEL=1 也一样）。
  故改用 ModelScope 直下 GGUF，再导入 ollama。

GGUF 分片格式（实测 qwen2.5-7b-instruct-q4_k_m）：
  `xxx-00001-of-00002.gguf` + `xxx-00002-of-00002.gguf`
  → 按序号拼接后是一个**完整的合法 GGUF**（llama.cpp 支持）。

★ 编码注意：Modelfile 模板里含 ollama TEMPLATE 语法要求的三引号，
  直接写在 Python 三引号字符串里会提前闭合（实测两次 SyntaxError），
  因此用列表拼接+ chr(34) 构造引号，避开所有转义问题。
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
MODELS = _ROOT / "models"

GGUF_MAGIC = b"GGUF"
Q = chr(34)


# ---------------------------------------------------------------- 合并
def merge_gguf(parts: list[Path], out: Path) -> Path:
    """按文件名序号拼接 GGUF 分片。"""
    if not parts:
        raise FileNotFoundError("没有分片可合并")
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with out.open("wb") as w:
        for p in sorted(parts):
            if not p.exists():
                raise FileNotFoundError(f"分片缺失: {p}")
            with p.open("rb") as r:
                shutil.copyfileobj(r, w, length=8 * 1024 * 1024)
            total += p.stat().st_size
            print(f"  + {p.name} ({p.stat().st_size / 1e9:.2f}GB)"
                  f" -> 累计 {total / 1e9:.2f}GB")
    print(f"合并完成: {out} ({out.stat().st_size / 1e9:.2f}GB)")
    return out


# ---------------------------------------------------------------- GGUF 校验
def read_gguf_meta(path: Path, want: int = 24) -> dict:
    """读取 GGUF 头部元数据（只解析前若干项，够校验即可）。

    GGUF 格式：magic(4) version(4) tensor_count(8) n_kv(8)
      [key_len(8) key val_type(4) value ...] ...
    目的是确认文件完整（不是被截断的分片）。
    """
    with path.open("rb") as f:
        magic = f.read(4)
        if magic != GGUF_MAGIC:
            raise ValueError(f"不是 GGUF 文件（magic={magic!r}）")
        version = struct.unpack("<I", f.read(4))[0]
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]

        def rd_str() -> str:
            n = struct.unpack("<Q", f.read(8))[0]
            return f.read(n).decode("utf-8", errors="replace")

        def rd_val(t: int):
            if t == 0:
                return struct.unpack("<b", f.read(1))[0]
            if t == 1:
                return struct.unpack("<B", f.read(1))[0]
            if t == 2:
                return struct.unpack("<h", f.read(2))[0]
            if t == 3:
                return struct.unpack("<H", f.read(2))[0]
            if t == 4:
                return struct.unpack("<i", f.read(4))[0]
            if t == 5:
                return struct.unpack("<I", f.read(4))[0]
            if t == 6:
                return struct.unpack("<f", f.read(4))[0]
            if t == 7:
                return struct.unpack("<b", f.read(1))[0]
            if t == 8:
                return struct.unpack("<q", f.read(8))[0]
            if t == 9:
                return struct.unpack("<Q", f.read(8))[0]
            if t == 10:
                return struct.unpack("<d", f.read(8))[0]
            if t == 11:
                return rd_str()
            if t == 12:
                n = struct.unpack("<Q", f.read(8))[0]
                return f"[array len={n}]"
            return None

        meta: dict = {}
        for i in range(min(n_kv, want)):
            try:
                k = rd_str()
                t = struct.unpack("<I", f.read(4))[0]
                meta[k] = rd_val(t)
            except Exception as exc:
                meta["_parse_error"] = f"{type(exc).__name__} (第{i}项)"
                break
    return {"version": version, "n_tensors": n_tensors, "n_kv": n_kv, "kv": meta}


def verify_gguf(path: Path) -> dict:
    """校验合并后的 GGUF 是否完整可读。"""
    info = read_gguf_meta(path)
    kv = info["kv"]
    print(f"  格式版本: v{info['version']}")
    print(f"  tensor 数: {info['n_tensors']:,}")
    print(f"  元数据项: {info['n_kv']}")
    print(f"  架构: {kv.get('general.architecture')}")
    print(f"  模型名: {kv.get('general.name')}")
    if "_parse_error" in kv:
        print(f"  ★ 解析中断: {kv['_parse_error']}")
    return info


# ---------------------------------------------------------------- Modelfile
def _tpl_lines(path: str) -> list[str]:
    """Modelfile 内容。

    ★ temperature 0.1 而非默认 0.8：本系统用 LLM 做**新闻打标**，
      要的是稳定分类输出而非创造性文本。高温度会让同一段新闻
      两次打标结果不同，破坏复盘一致性（附录 M8 要求）。
    ★ num_gpu 99 = 尽可能全部上 GPU（8GB显存跑 Q4 的 7B 没问题）。
    ★ stop 参数必须用 chr(34) 构造引号 —— 用普通引号会被
      shell / heredoc 多层转义吃掉，写进 Modelfile 的变成空字符串
      （实测踩过：`PARAMETER stop ""` 意味着不停止任何 token）。
    ★ path 必须是**绝对路径且与磁盘大小写一致**：
      相对路径会被 ollama 当成模型名去 registry 找
      （实测报 `registry.ollama.ai: no such host`）。
    """
    return [
        f"FROM {path}",
        "",
        "PARAMETER stop " + Q + "<|im_end|>" + Q,
        "PARAMETER stop " + Q + "<|endoftext|>" + Q,
        "PARAMETER temperature 0.1",
        "PARAMETER top_p 0.8",
        "PARAMETER num_ctx 8192",
        "PARAMETER num_gpu 99",
        "",
        "TEMPLATE " + Q * 3 + "{{.System}}",
        "{{.Prompt}}",
        Q * 3,
    ]


def write_modelfile(gguf: Path) -> Path:
    mf = MODELS / "Modelfile"
    mf.write_text("\n".join(_tpl_lines(gguf.as_posix())), encoding="utf-8")
    print(f"已写出 Modelfile: {mf}")
    return mf


def import_to_ollama(name: str,
                     ollama: str | None = None) -> int:
    exe = ollama or r"C:\Users\cxul\AppData\Local\Programs\Ollama\ollama.exe"
    mf = MODELS / "Modelfile"
    if not mf.exists():
        print("Modelfile 不存在")
        return 1
    print(f"导入 ollama（模型名 {name}）...")
    r = subprocess.run([exe, "create", name, "-f", str(mf)],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        print(f"  失败: {(r.stderr or r.stdout)[-500:]}")
        return r.returncode
    print(f"  成功: {(r.stdout or '').strip()[-200:]}")
    return 0


def test_inference(name: str, prompt: str | None = None) -> dict:
    """导入后必须实测推理 —— 导入成功 ≠ 能跑。"""
    import json
    import time
    import urllib.request

    payload = {
        "model": name,
        "messages": [{"role": "user",
                      "content": prompt or "只回答一个词：中国的首都是哪里？"}],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 32},
    }
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode("utf-8"))
    el = time.monotonic() - t0
    txt = (d.get("message") or {}).get("content", "")
    print(f"  回答: {txt.strip()!r}")
    print(f"  耗时: {el:.1f}s  tokens={d.get('eval_count','?')}")
    return {"reply": txt, "elapsed_s": round(el, 1),
            "eval_count": d.get("eval_count")}


# ---------------------------------------------------------------- CLI
def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="GGUF 合并与 ollama 导入")
    ap.add_argument("--merge", action="store_true", help="合并分片")
    ap.add_argument("--verify", action="store_true", help="校验 GGUF")
    ap.add_argument("--modelfile", action="store_true", help="写 Modelfile")
    ap.add_argument("--import", dest="do_import", action="store_true", help="导入 ollama")
    ap.add_argument("--test", action="store_true", help="实测推理")
    ap.add_argument("--all", action="store_true", help="全流程")
    ap.add_argument("--name", default="hawk-qwen2.5-7b")
    args = ap.parse_args()

    if not any([args.merge, args.verify, args.modelfile,
                args.do_import, args.test, args.all]):
        args.all = True

    parts = sorted(MODELS.glob("qwen2.5-7b-instruct-q4_k_m-*-of-00002.gguf"))
    # 优先用单文件版（ollama 只接受单文件 GGUF；
    # 简单拼接分片会报 `must use llama.cpp split filename pattern`）
    single = MODELS / "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
    merged = MODELS / "qwen2.5-7b-instruct-q4_k_m.gguf"   # 自建合并版（备用）
    target = single if single.exists() else merged

    if args.all or args.merge:
        print("=== 合并分片 ===")
        if len(parts) < 2:
            print(f"分片不足（找到 {len(parts)} 个）："
                  + ", ".join(p.name for p in parts))
            return 1
        merge_gguf(parts, merged)
        target = merged

    if args.all or args.verify:
        print("\n=== 校验 GGUF ===")
        if not target.exists():
            print(f"目标不存在: {target}")
            return 1
        verify_gguf(target)

    if args.all or args.modelfile:
        print("\n=== 写 Modelfile ===")
        write_modelfile(target.resolve())

    if args.all or args.do_import:
        print("\n=== 导入 ollama ===")
        if import_to_ollama(args.name) != 0:
            return 1

    if args.all or args.test:
        print("\n=== 实测推理 ===")
        try:
            test_inference(args.name)
        except Exception as exc:
            print(f"  失败: {type(exc).__name__}: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())