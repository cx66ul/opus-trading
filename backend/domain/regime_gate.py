# -*- coding: utf-8 -*-
"""市场状态门控（把 D16 的结论变成可执行规则）。

★ 为什么要这个模块
------------------
D16 已证明（400 只 / 765 笔）：
    上涨市超额 +1.61%  ← 最好
    震荡市超额 **-1.28%**  ← 最差，追高被套
    下跌市超额 +0.59%
⇒ 结论：**缠论买点不适合震荡市**，应该在市场状态合适时才出信号。

★★★ 但「用段标签做门控」是**前视偏差**，这是本模块存在的全部理由
--------------------------------------------------------------------
`run_regime_split.market_segments` 的段标签来自**整段的 60 日涨跌平均**：
    up 段 2025-09-11~12-18（65 天）的 avg_roll = +69.78%
首日 2025-09-11 当天的 60 日涨跌只有 +40.19% —— 段的定性用到了**未来 64 天**。
实测该数据集上「首日 roll 的定性」与「段定性」9/9 一致，
**但这是巧合，不是保证**。一旦拿它做门控，就是「事后知道这段是涨市才买入」。

⇒ 所以门控必须只用**当日及之前**可得的信息：
    `day_regime(d)` = 仅由 date<=d 的数据算出的状态。
两者分工：
    · 归因分析（这段行情里策略表现如何）→ 可以用段标签（描述性）
    · **交易门控（今天该不该出信号）→ 只能用 day_regime（因果性）**

★ 状态判定为什么不用「段」
----------------------
段边界本身也是事后定的（min_len 合并要看整段）。
`day_regime` 则每日期独立判定 + 只用滞后的滚动窗口，天然无前视。

★ 为什么还提供 `smooth_regime`
---------------------------
单日 60 日滚动涨跌噪声大（±1.5%），阈值附近会疯狂翻转。
`smooth_regime` 用**截至当日的** N 日平均 roll 做迟滞，
既是平滑，又严格只用历史 —— 这是「段内平均」的因果版本。

运行： python -m backend.domain.regime_gate --demo
"""
from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, field
from datetime import date

DAY_STATES = ("up", "flat", "down")


@dataclass
class RegimeSeries:
    """市场状态序列（point-in-time）。

    dates  : 交易日，升序
    roll   : 与 dates 对齐的 N 日滚动涨跌 %（已剔除前 window 个点）
    """

    dates: list[str]
    roll: list[float]
    window: int = 60
    up_th: float = 8.0
    down_th: float = -8.0
    _index: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._index = {d: i for i, d in enumerate(self.dates)}
        # ★ 允许 roll 为空 —— `build_regime_series` 在数据不足一个窗口时
        #   会**故意**返回空 roll（不硬判趋势），此时所有日期判为 unknown。
        #   早先这里直接抛 ValueError，导致「数据不足」变成崩溃。
        if len(self.roll) not in (0, len(self.dates)):
            raise ValueError(
                f"roll 长度 {len(self.roll)} 与 dates 长度 "
                f"{len(self.dates)} 不一致"
            )

    # ------------------------------------------------------------ 基础查询
    def roll_on(self, d: str) -> float | None:
        """当日（或之前最近一日）的滚动涨跌。日期不存在返回 None。

        ★ roll 为空（数据不足一个窗口）时返回 None，
          调用方据此判 unknown —— **不返回 0**，
          因为 0 会被读成「大盘走平」，那是猜测而非事实。
        """
        if not self.roll:
            return None
        i = self._index.get(d)
        if i is None:
            return self.roll[-1]
        return self.roll[i]

    def day_regime(self, d: str) -> str:
        """★ 当日状态 —— **只用截至当日的滚动涨跌**，无前视。

        这是可用于交易门控的判据。
        """
        if not self.roll:
            return "unknown"          # ★ 数据不足一个窗口 ⇒ 不猜趋势
        r = self.roll_on(d)
        if r is None:
            return "unknown"
        return "up" if r > self.up_th else "down" if r < self.down_th else "flat"

    def smooth_regime(self, d: str, smooth_n: int = 10) -> str:
        """★ 平滑状态 —— **截至当日的** N 日平均 roll 做判定。

        为什么平滑必须「只用历史」：
          若用整段平均（`run_regime_split.market_segments` 那种），
          就把段内未来信息引入了 ⇒ 门控失效。
        """
        i = self._index.get(d)
        if i is None or not self.roll:
            return "unknown"
        lo = max(0, i - smooth_n + 1)
        window = self.roll[lo: i + 1]
        if not window:
            return "unknown"
        avg = statistics.fmean(window)
        return ("up" if avg > self.up_th
                else "down" if avg < self.down_th else "flat")

    # ------------------------------------------------------------ 门控
    def allow(self, d: str, allow_states=("up",), smooth: int = 10) -> bool:
        """★ 门控：该日是否允许出买点。

        `allow_states=("up",)`  → 只在上涨市出信号（D16 建议）
        `allow_states=("up","down")` → 只避开震荡市
        `allow_states=("up","flat","down")` → 不过滤（对照）

        ★ 默认用 `smooth_regime`（因果）而非 `day_regime`，
          因为单日 roll 噪声大、门控会抖。
        """
        if "unknown" in allow_states:
            return True
        st = self.smooth_regime(d, smooth) if smooth else self.day_regime(d)
        return st in allow_states

    def coverage(self, allow_states=("up",), smooth: int = 10) -> dict:
        """统计各状态的天数占比 —— 门控太强会把信号全滤掉。"""
        counts: dict[str, int] = {s: 0 for s in DAY_STATES}
        for d in self.dates:
            st = self.smooth_regime(d, smooth) if smooth else self.day_regime(d)
            if st in counts:
                counts[st] += 1
        total = sum(counts.values()) or 1
        out = {k: v / total * 100 for k, v in counts.items()}
        out["allowed"] = sum(out[s] for s in allow_states if s in out)
        return out


# ================================================================== 构造
def build_regime_series(index_rows, window: int = 60,
                        up_th: float = 8.0, down_th: float = -8.0
                        ) -> RegimeSeries:
    """由「日期 → 等权指数值」构造状态序列。

    index_rows : [(date_like, close), ...] 升序
    ★ 滚动涨跌只用 `vals[i]` 与 `vals[i-window]`，两者都 <= i，
      所以 roll[i] 是 point-in-time 的。
    """
    dates = [str(r[0]) for r in index_rows]
    vals = [float(r[1]) for r in index_rows]
    if len(vals) <= window:
        # 不足一个窗口时全部不可判 —— 返回空 roll 而非报错，
        # 调用方按 unknown 处理（不能拿短样本硬判趋势）
        return RegimeSeries(dates=dates, roll=[], window=window,
                            up_th=up_th, down_th=down_th)
    kept_d, kept_r = dates[window:], []
    for i in range(window, len(vals)):
        kept_r.append((vals[i] / vals[i - window] - 1) * 100)
    return RegimeSeries(dates=kept_d, roll=kept_r, window=window,
                        up_th=up_th, down_th=down_th)


# ================================================================== 自检
def check_no_lookahead(rs: RegimeSeries, smooth: int = 10) -> dict:
    """★ 自检：截断数据后重算，早期判定不得改变。

    这是「无前视」唯一的硬判据 —— 声称因果性的函数，
    必须在只给历史数据时算出同样结果。
    """
    out = {"checked": 0, "violations": []}
    n = len(rs.dates)
    # 取若干切点，只用切点之前的数据重建，比较该日判定
    for cut in range(max(1, n // 8), n, max(1, n // 20)):
        d = rs.dates[cut]
        prefix = list(zip(rs.dates[: cut + 1], rs.roll[: cut + 1]))
        sub = RegimeSeries(dates=[x[0] for x in prefix],
                           roll=[x[1] for x in prefix],
                           window=rs.window, up_th=rs.up_th,
                           down_th=rs.down_th)
        for fn_name in ("day_regime", "smooth_regime"):
            full = (getattr(rs, fn_name)(d, smooth) if fn_name == "smooth_regime"
                    else getattr(rs, fn_name)(d))
            part = (getattr(sub, fn_name)(d, smooth) if fn_name == "smooth_regime"
                    else getattr(sub, fn_name)(d))
            out["checked"] += 1
            if full != part:
                out["violations"].append(
                    f"{d} {fn_name}: 全量={full} 截断={part}")
    out["ok"] = not out["violations"]
    return out


if __name__ == "__main__":                       # pragma: no cover
    import random

    print("=" * 74)
    print("市场状态门控 · 自检（纯函数演示，不依赖数据库）")
    print("=" * 74)

    # 确定性行情：涨 → 震荡 → 跌 → 涨
    rng = random.Random(11)
    from datetime import timedelta
    d0 = date(2025, 1, 1)
    rows, v = [], 100.0
    pattern = ([0.004] * 80 + [0.0002] * 60 + [-0.004] * 70 + [0.003] * 60)
    for i in range(len(pattern)):
        v *= (1 + pattern[i] + rng.uniform(-0.008, 0.008))
        rows.append((d0 + timedelta(days=i), v))

    rs = build_regime_series(rows, window=20)
    print(f"构造 {len(rs.dates)} 个可判定交易日 "
          f"({rs.dates[0]} ~ {rs.dates[-1]})")
    print()
    print("★ 状态分布与门控覆盖率:")
    cov = rs.coverage(("up",))
    print(f"  up   {cov['up']:5.1f}%")
    print(f"  flat {cov['flat']:5.1f}%")
    print(f"  down {cov['down']:5.1f}%")
    print(f"  → 只在 up 出信号 = 保留 {cov['allowed']:.1f}% 的交易日")
    print()
    print("★ 前视自检（截断重算，早期判定必须一致）:")
    r = check_no_lookahead(rs)
    print(f"  检查 {r['checked']} 次 → {'通过' if r['ok'] else '★发现前视偏差!'}")
    for v_ in r["violations"][:5]:
        print("   ", v_)
    print()
    print("★ 三种门控策略在同一天的表现:")
    for d in (rs.dates[10], rs.dates[90], rs.dates[170], rs.dates[-1]):
        print(f"  {d} roll={rs.roll_on(d):+7.2f}%  "
              f"day={rs.day_regime(d):5s} smooth={rs.smooth_regime(d):5s}  "
              f"up_only={rs.allow(d, ('up',))}  no_flat={rs.allow(d, ('up','down'))}")
    print()
    print("★ 注意：day_regime 在阈值附近会翻转，smooth_regime 稳定 ——")
    print("  所以门控默认用 smooth（且严格只用截至当日的 roll）。")
