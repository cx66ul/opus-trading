# -*- coding: utf-8 -*-
"""★★ 死纪律：每次做完训练都要备份数据（用户 2026-10-11 指示）

★★★ 为什么用代码而不是提醒
--------------------------------------------------
「记得备份」不是纪律 —— 记得会忘。本组测试守护三件事：

1. **备份器接在训练提交里**，调用方无法绕过
   （否则「每次都备份」只是一句约定）
2. **备份失败 ⇒ 整单判失败**，不吞、不降级、不「下次补」
   （「记录后继续」会让纪律变成空话）
3. **备份对象 == 训练对象**
   实测事故（2026-10-11）：`labels-all.jsonl`（训练真正用的累积样本）
   **从不落盘**，只是 `build_trainset()` 内存里的字节串。
   备份器按文件名抓 `data/labels-*` ⇒ 累积样本没备份到，
   却打印「备份成功」（只备份了 2 个文件）。
   ⇒ 现在必须从 `build_trainset()` 直接取那份字节串。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BACKUP = ROOT / "scripts" / "backup_after_train.py"
CLIENT = Path(r"C:/Users/cxul/WorkBuddy/每日选股以及评估/"
              r"新闻利好利空系统/liangjiang_client.py")


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestBackupIsWiredIntoTraining:
    def test_submit_calls_backup(self):
        """★ `submit_training()` 内部必须调用备份器。"""
        src = _src(CLIENT)
        i = src.index("def submit_training")
        j = src.index("\n    def ", i + 10)
        body = src[i:j]
        assert "_train_backup" in body, (
            "训练提交里没有备份调用 ⇒ 「每次训练都备份」形同虚设")

    def test_backup_failure_fails_the_whole_call(self):
        """★★ 备份异常 ⇒ 返回 ok=False，不得继续报成功。

        ★★ 判据不能是「块里出现过 return False」——
          我第一版这么写，变异把 return 包进 `if False:` 后**测试仍全绿**，
          因为那个 return 还在块里（只是永远到不了）。
          ⇒ 必须解析 AST 并检查 **If.test 为常假** 的分支：
            那正是「看着有 return、实际不 return」的伪装形态。
        """
        import textwrap
        src = _src(CLIENT)
        i = src.index("def submit_training")
        j = src.index("\n    def ", i + 10)
        body = textwrap.dedent(src[i:j])

        tree = ast.parse(body)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef))

        handlers = [n for n in ast.walk(fn)
                    if isinstance(n, ast.ExceptHandler)]
        assert handlers, "备份未做异常处理"

        # 找出备份那个 handler（含「备份失败」字样的）
        target = None
        for h in handlers:
            seg = ast.get_source_segment(body, h) or ""
            if "备份" in seg or "_train_backup" in seg:
                target = h
        assert target is not None, "未找到备份相关的 except 块"

        # 块内的 return 必须**无条件**执行（不在恒假 If 里）
        def _returns_under(node) -> bool:
            for n in ast.walk(node):
                if not isinstance(n, ast.If):
                    continue
                if isinstance(n.test, ast.Constant) and not n.test.value:
                    continue                      # `if False:` —— 不算
                if any(isinstance(x, ast.Return)
                       for x in ast.walk(n.body)):
                    return True
            return False

        ok = any(isinstance(n, ast.Return) and n not in
                 [x for i_ in ast.walk(target)
                  if isinstance(i_, ast.If)
                  and isinstance(i_.test, ast.Constant)
                  for x in ast.walk(i_.body)]
                 for n in ast.walk(target)) or _returns_under(target)
        assert ok, (
            "备份的 except 块没有**无条件** return ⇒ "
            "备份失败会静默继续并报成功（变异形态：return 被包进 if False）")

    def test_backup_helper_raises_on_failure(self):
        """★ `_train_backup()` 失败必须抛，不能返回 None 当成功。"""
        src = _src(CLIENT)
        i = src.index("def _train_backup")
        j = src.index("\n", src.index("return m.run(day)", i))
        body = src[i:j]
        assert "return m.run(day)" in body
        assert "try" not in body, (
            "_train_backup 不该吞异常 —— 异常必须上抛让调用方判失败")


class TestBackupIsComplete:
    def test_uses_build_trainset_not_filename(self):
        """★★ 累积样本必须从 `build_trainset()` 取，不能按文件名猜。"""
        src = _src(BACKUP)
        assert "build_trainset()" in src, (
            "备份器未使用训练同一份样本来源")
        # ★ 反向：不得只依赖 data/labels-*.jsonl 文件
        assert '"labels-all.jsonl",' not in src.split("blobs")[0], (
            "仍把 labels-all.jsonl 当成磁盘文件找 —— 它从不落盘")

    def test_missing_pieces_is_failure(self):
        """★ 缺件必须报错，不得「备份一部分也算成功」。"""
        src = _src(BACKUP)
        assert "拒绝报成功" in src, "备份不完整时未拒绝报成功"
        assert "must = " in src and "labels-all.jsonl" in src, (
            "未对训练实际使用的样本做必备份校验")

    def test_no_secret_in_backup(self):
        """★ 密钥类文件不得进备份。"""
        src = _src(BACKUP)
        assert "SECRET_PATTERNS" in src
        assert "跳过疑似密钥文件" in src

    def test_verifies_sha256(self):
        """★ 备份后必须自查 sha256 —— 「上传成功」不等于「内容正确」。"""
        src = _src(BACKUP)
        assert "sha256" in src
        assert "def verify" in src
        # verify 必须真的比对
        i = src.index("def verify")
        j = src.index("\ndef ", i + 10)
        assert "sha256" in src[i:j] and "bad" in src[i:j]


class TestBackupFailurePropagates:
    def test_top_level_exits_nonzero(self):
        """★ 顶层也必须非零退出，不能吞。"""
        src = _src(BACKUP)
        i = src.index("def main")
        body = src[i:]
        assert "return 1" in body, "备份失败时退出码不是 1"
