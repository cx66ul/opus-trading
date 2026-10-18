# -*- coding: utf-8 -*-
"""★ 长跑器 + cron 分钟字段

★★★ 这组测试防的是「任务静默永不执行」
--------------------------------------------------
实测（2026-10-11）：`scheduler._in_cron_window` 解构cron 时写的是

    _minute, hour, _dom, _mon, dow = parts     # ★ 分钟被丢掉

理论触发点一律取 `dtime(h, 0)`（整点零分）⇒ `30 8 * * 1-5`
只在 08:00 那一分钟为真，窗口给到 08:01~08:59 全判 False。

⇒ 后果（两个都静默）：
  · `catch_up` 在 08:31 唤醒 → **补不到 premarket**
  · 常驻长跑器按分钟轮询 → 等不到那个「08:00 那一分钟」
⇒ 盘前任务**永不执行且不报任何错**。

运行：pytest tests/test_soak.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.scheduler import TASK_TABLE, _in_cron_window   # noqa: E402

CST = timezone(timedelta(hours=8))


def hits(cron: str, probe: str, width_min: int = 1) -> list[str]:
    t = datetime.strptime(probe, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
    return [m[0] for m in TASK_TABLE
            if _in_cron_window(m[1], t - timedelta(minutes=width_min), t)]


class TestCronMinuteIsHonoured:
    """★ cron 的分钟字段必须参与判定。"""

    def test_premarket_fires_at_0830_not_0800(self):
        """`30 8 * * 1-5`（周一至周五 08:30）。"""
        assert "premarket" in hits("30 8 * * 1-5", "2026-10-12 08:30")
        assert "premarket" not in hits("30 8 * * 1-5", "2026-10-12 08:31")

    def test_close_fires_at_1505(self):
        assert "close" in hits("5 15 * * 1-5", "2026-10-12 15:05")
        assert "close" not in hits("5 15 * * 1-5", "2026-10-12 15:06")

    def test_daily_sync_fires_at_1530(self):
        assert "daily_sync" in hits("30 15 * * 1-5", "2026-10-12 15:30")

    def test_midday_fires_at_1130(self):
        assert "midday" in hits("30 11 * * 1-5", "2026-10-12 11:30")

    def test_ref_sync_daily_at_2000(self):
        assert "ref_sync" in hits("0 20 * * *", "2026-10-11 20:00")

    def test_weekly_only_saturday(self):
        """`0 10 * * 6` 只在周六命中。"""
        assert "weekly" in hits("0 10 * * 6", "2026-10-10 10:00")
        assert "weekly" not in hits("0 10 * * 6", "2026-10-09 10:00")


class TestCatchUpWindowWidth:
    """★ 窗口给宽一点也必须命中（模拟「31 分钟才醒」）。"""

    def test_wide_window_still_hits(self):
        """`catch_up(as_of, window_hours=24)` 窗口很宽，
        但**分钟字段精确**才是关键 —— 08:31 醒、窗口 24h，
        理论触发点 08:30 已过 ⇒ 应当命中。
        """
        t = datetime(2026, 10, 12, 8, 31, tzinfo=CST)
        assert _in_cron_window("30 8 * * 1-5", t - timedelta(hours=24), t), (
            "24h 窗口下应命中 08:30 的 premarket")


class TestEveryScheduledTaskHasProbe:
    """★ 每个任务都必须有至少一个能命中它的时刻 —— 否则它永不执行。"""

    # 交易日的代表时刻（周一 2026-10-12）
    PROBES = ["2026-10-12 08:30", "2026-10-12 09:20", "2026-10-12 10:05",
              "2026-10-12 11:30", "2026-10-12 13:30", "2026-10-12 15:05",
              "2026-10-12 15:30", "2026-10-12 09:30",
              # 非交易日任务用周日（2026-10-11）
              "2026-10-11 20:00", "2026-10-11 23:00", "2026-10-11 08:45",
              # 周六 weekly
              "2026-10-10 10:00"]

    @pytest.mark.parametrize("name,cron,td_only", [
        (m[0], m[1], m[2]) for m in TASK_TABLE])
    def test_task_reachable(self, name, cron, td_only):
        """★ 变异验证：把分钟字段去掉，本判据必须失败。"""
        probes = self.PROBES
        if td_only:
            probes = [p for p in probes if not p.startswith("2026-10-11")
                      and not p.startswith("2026-10-10")]
        hit = any(_in_cron_window(cron,
                                  datetime.strptime(p, "%Y-%m-%d %H:%M")
                                  .replace(tzinfo=CST) - timedelta(minutes=1),
                                  datetime.strptime(p, "%Y-%m-%d %H:%M")
                                  .replace(tzinfo=CST))
                  for p in probes)
        assert hit, f"任务 {name}({cron}) 在所有探针时刻都命中不到 ⇒ 永不执行"


class TestSoakRunnerShape:
    """★ 长跑器本身的结构守护。"""

    SRC = ROOT / "scripts" / "soak_runner.py"

    def test_exists(self):
        assert self.SRC.exists(), "缺少常驻长跑器"

    def test_reuses_project_cron_judge(self):
        """★ 必须复用 `scheduler._in_cron_window`，不自己解析 cron。

        我第一版按「cron 分钟数 == 当前分钟」筛，
        而项目函数按整点判 ⇒ 两套口径，
        表现是「长跑一晚上 premarket 一次没跑且不报错」。
        """
        src = self.SRC.read_text(encoding="utf-8")
        assert "_in_cron_window" in src, "长跑器未复用项目 cron 判定"
        assert "cron.split()[0]" not in src, "长跑器自行解析 cron 分钟"

    def test_has_heartbeat(self):
        """★ 必须有心跳：区分「无事可做」与「进程已死」。"""
        src = self.SRC.read_text(encoding="utf-8")
        assert "soak_heartbeat" in src
        assert "run_state" in src

    def test_per_job_timeout_isolated(self):
        """★ 单任务超时不得阻塞整轮（用 thread + join(timeout)）。"""
        src = self.SRC.read_text(encoding="utf-8")
        assert "threading.Thread" in src, "缺少线程隔离"
        assert "join(timeout" in src, "缺少超时上限"

    def test_no_bare_except_pass(self):
        """★ 不得 `except: pass` —— 那就是静默中断的定义。"""
        import ast
        tree = ast.parse(self.SRC.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.ExceptHandler)
                    and len(n.body) == 1
                    and isinstance(n.body[0], ast.Pass)):
                raise AssertionError(
                    f"第 {n.lineno} 行 except...pass ⇒ 异常被静默吞掉")


class TestNoGuessedAttributeNames:
    """★★ 不许猜 dataclass 字段名。

    实测（2026-10-11）：`run_one()` 里我凭印象写 `j.timeout_s`，
    而真实字段叫 `timeout_sec` ⇒ 长跑**每轮** AttributeError，
    11 次失败全是这一个笔误，且日志里是一条重复的同款错误
    —— 不看字段列表根本想不到是「名字打错」而不是「任务坏了」。

    ⇒ 判据：长跑器引用的每个 `Job` 属性都必须**真实存在**。
    """

    def test_referenced_attrs_exist(self):
        import ast

        from backend.scheduler import Job, REGISTRY
        real = set(Job.__dataclass_fields__)
        src = (ROOT / "scripts" / "soak_runner.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)

        # 只抓 `j.<attr>` / `job.<attr>` 这种「从 Job 变量取属性」的写法
        used = set()
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id in {"j", "job"}):
                used.add(n.attr)
        assert used, "未找到对 Job 的属性访问，判据本身失效"
        unknown = {a for a in used
                   if a not in real and not a.startswith("_")}
        assert not unknown, (
            f"长跑器访问了 Job 上不存在的属性 {unknown}；"
            f"真实字段：{sorted(real)}")
        # 反向自证：REGISTRY 里确有任务可取
        assert REGISTRY, "REGISTRY 为空，无法验证属性来源"


class TestNoDuplicateRunRecords:
    """★★ 不得**重复记账**。

    实测（2026-10-11）：长跑器在 `run_job()` 之外**又**写了一条
    `kind='job_run'`，而 `run_job()` 内部已经调 `_log_run()` 记过
    ⇒ 每条执行记录**时间戳完全相同地出现两次**
    ⇒ 样本量翻倍、失败率算错，而**没有任何报错**。

    ★ 判据：长跑器里只能出现两种 `log_event` ——
      `run_state`（心跳）与 `soak_tick`（自身异常），
      **不得**对任务执行结果再记一次。
    """

    def test_soak_runner_does_not_log_job_run(self):
        import ast
        src = (ROOT / "scripts" / "soak_runner.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        bad = []
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id == "log_event"
                    and n.args
                    and isinstance(n.args[0], ast.Constant)
                    and n.args[0].value == "job_run"):
                # ★ 只放行自身异常（soak_tick），不放行任务执行结果
                seg = ast.get_source_segment(src, n) or ""
                if "soak_tick" not in seg:
                    bad.append(n.lineno)
        assert not bad, (
            f"第 {bad} 行重复记录 job_run —— `run_job()` 内部已经记过了，"
            f"重复记账会让统计翻倍且不报错")

    def test_scheduler_logs_itself(self):
        """★ 反向自证：`run_job()` 确实自己记了一条。"""
        src = (ROOT / "backend" / "scheduler.py").read_text(encoding="utf-8")
        assert "_log_run(res, as_of, attempt)" in src, (
            "run_job 不再自己记账 ⇒ 那么长跑器的记录就是唯一来源，"
            "此时删掉长跑器的记录会导致完全没有执行日志")
        assert '"job_run"' in src
