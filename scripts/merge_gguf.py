"""GGUF 分片**真正**合并（llama.cpp split 格式 -> 单文件）。

★ 为什么不能简单字节拼接：
  llama.cpp 的 split GGUF 在 metadata 里有 `split.count=2` / `split.no` /
  `split.tensors.count` 三个标记。简单拼接后这些标记还在，ollama 读到
  `split.count=2` 就会去找第2 片而找不到，于是报：
      Error: split GGUF "xxx.gguf" must use llama.cpp split filename pattern
  实测踩过：拼接后字节数与官方单文件版完全一致（4683074240），
  但依然被拒 —— 说明**字节对齐不等于格式合法**。

正确做法：用 gguf 库读出全部 tensor，按序重写到新文件，
并**剔除 split.* 标记**。

实测数据（qwen2.5-7b-instruct-q4_k_m）：
  第 1 片：280 个 tensor，split.no=0，split.count=2，split.tensors.count=339
  第 2 片：59 个tensor（339 - 280）
  合并后应为 339 个 tensor、无 split 标记
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

MODELS = _ROOT / "models"

# 必须剔除的 split 标记（保留它们会让 ollama 以为文件不完整）
SPLIT_KEYS = (
    "split.no",
    "split.count",
    "split.tensors.count",
    "split.tensors.offset",
    "general.base_model.count",
    "general.base_model.0.name",
    "general.base_model.0.organization",
    "general.base_model.0.repo_url",
)


def merge_with_gguf(parts: list[Path], out: Path) -> dict:
    """用 gguf 库把 split GGUF 合并为单文件。"""
    import numpy as np

    from gguf import GGUFReader, GGUFWriter
    from gguf.quants import quant_shape_to_byte_shape

    if len(parts) < 2:
        raise ValueError(f"需要至少 2 个分片，收到 {len(parts)}")

    readers = [GGUFReader(str(p)) for p in parts]
    r0 = readers[0]

    # 架构名（合并后必须保留，否则 ollama 不认）
    arch = "qwen2"
    for f in r0.fields.values():
        if f.name == "general.architecture":
            try:
                arch = f.contents()
            except Exception:
                pass
    print(f"  架构: {arch}")

    total_tensors = sum(len(r.tensors) for r in readers)
    print(f"  各片 tensor 数: {[len(r.tensors) for r in readers]} -> 合计 {total_tensors}")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    w = GGUFWriter(path=None, arch=arch)

    # ① 拷贝全部 kv（跳过 split.* 标记）
    # ★ field.parts 是**原始字节 memmap**，不是已解码的值；
    #   field.types 才是目标 GGUFValueType。必须用 add_key_value 原样搬运，
    #   自己按Python 类型重新赋值会丢失量化/二进制类型信息。
    n_kv = 0
    skipped = []
    for field in r0.fields.values():
        name = field.name
        if name.startswith("split."):
            skipped.append(name)
            continue
        try:
            vtype = field.types[0] if field.types else None
            raw = field.parts[0]
            # ★ 必须是 numpy 标量/数组，不能是 memmap ——
            #   struct.pack 无法直接转换 memmap（实测报
            #   "only integer scalar arrays can be converted to a scalar index"）。
            #   tolist() 对uint8数组（字符串内容）会得到 int 列表，
            #   故按类型分别处理。
            if hasattr(raw, "tobytes") and raw.dtype == np.uint8:
                val = bytes(raw.tobytes()).decode("utf-8", errors="replace")
            elif hasattr(raw, "item") and getattr(raw, "ndim", 1) == 0:
                val = raw.item()
            elif hasattr(raw, "tolist"):
                val = raw.tolist()
                if isinstance(val, list) and len(val) == 1:
                    val = val[0]
            else:
                val = raw
            w.add_key_value(name, val, vtype)
            n_kv += 1
        except Exception as exc:
            print(f"    [skip kv] {name}: {type(exc).__name__}: {exc}")
    print(f"  写入 kv {n_kv} 项；剔除 {len(skipped)} 个 split 标记: {skipped[:6]}")

    # ② 按顺序写全部 tensor
    for i, r in enumerate(readers):
        for t in r.tensors:
            # ★ 量化 tensor：reader 的 t.shape 是**元素 shape**，
            #   add_tensor_info 需要**字节 shape**（内部会转回元素 shape）。
            #   直接传元素 shape 会报
            #   "bytes per row 3584 not multiple of Q6_K type size 210"。
            try:
                byte_shape = quant_shape_to_byte_shape(t.shape, t.tensor_type)
            except Exception:
                byte_shape = t.shape
            w.add_tensor_info(t.name, byte_shape, t.data.dtype, t.n_bytes,
                              raw_dtype=t.tensor_type)
            # tensor 内容挂在 TensorInfo.tensor 上（write_tensors_to_file 只认它）
            ti = w.tensors[-1][t.name]
            ti.tensor = t.data
        print(f"  已加入第 {i + 1} 片累计 {sum(len(d) for d in w.tensors)} 个 tensor")

    # ③ 落盘
    w.write_header_to_file(out)
    w.write_kv_data_to_file()
    w.write_tensors_to_file()
    w.close()

    size = out.stat().st_size
    print(f"  合并完成: {out.name} {size / 1e9:.2f}GB / {len(w.tensors)} tensor")
    return {"path": str(out), "size": size, "tensors": len(w.tensors),
            "kv_written": n_kv, "split_keys_removed": skipped}


def logger_skip(msg: str) -> None:
    print(f"    [skip] {msg}")


def verify(path: Path) -> dict:
    """校验合并结果：可读、无 split 标记、tensor 数正确。"""
    from gguf import GGUFReader

    r = GGUFReader(str(path))
    leftover = [f.name for f in r.fields.values() if f.name.startswith("split.")]
    arch = None
    for f in r.fields.values():
        if f.name == "general.architecture":
            arch = f.contents()
    print(f"  可读: 是")
    print(f"  架构: {arch}")
    print(f"  tensor 数: {len(r.tensors)}")
    print(f"  残留 split 标记: {leftover if leftover else '无（正确）'}")
    return {"arch": arch, "tensors": len(r.tensors),
            "split_leftover": leftover, "size": path.stat().st_size}


if __name__ == "__main__":
    parts = sorted(MODELS.glob("qwen2.5-7b-instruct-q4_k_m-*-of-00002.gguf"))
    if len(parts) < 2:
        print(f"分片不足: {[p.name for p in parts]}")
        raise SystemExit(1)
    out = MODELS / "qwen25-7b-q4km-merged.gguf"
    print("=== 合并 ===")
    info = merge_with_gguf(parts, out)
    print()
    print("=== 校验 ===")
    v = verify(out)
    print()
    print("★ 若残留 split 标记为空且 tensor 数 = 339，说明合并正确")