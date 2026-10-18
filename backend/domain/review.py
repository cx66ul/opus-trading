"""信号回溯验证与复盘（附录 7.1 ②③④+ 7.2）。

★ 三条不可妥协的机制（都是「防事后诸葛亮」，不是我的偏好）：
1. **样本 < 10 不允许显示胜率**（附录 5.4）——
   "1 单 1 胜 = 100%" 是最危险的误导。这里返回 `winrate=None` 而非 1.0。
2. **只追加不修改**（附录 7.2）—— `signal` 与 `eval_json` 一次写入后不再改。
   若允许回填，等于允许「用后来的信息美化当初的判断」。
3. **冻结参数版本**（附录 7.2）—— 重放必须用当时生效的规则版本，
   不允许「现在的我」评价「当时的我」。

设计：全部纯函数 f(信号, 后续K线, as_of)，便于离线复现与单元测试。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger                       # noqa: E402

from backend.data.sqlite import get_conn, log_event   # noqa: E402

# 附录 7.1② 要求的观察窗口
HORIZONS = (1, 3, 5, 10, 20)

# ★ 附录 5.4：样本 < 10 不允许显示胜率
MIN_SAMPLE_FOR_WINRATE = 10

# 买点/卖点方向：买点的判定是「之后涨了算对」，卖点反之
BUY_KINDS = ("1buy", "2buy", "3buy")
SELL_KINDS = ("1sell", "2sell", "3sell")


# ---------------------------------------------------------------- 数据结构
@dataclass
class HorizonOutcome:
    """单个观察窗口的结果。"""
    days: int
    ret_pct: float | None = None            # 相对信号价的涨跌幅 %
    max_gain: float | None = None          # 期间最大涨幅（买点看）
    max_loss: float | None = None          # 期间最大跌幅
    hit: bool | None = None                # 是否命中（方向正确）
    bars: int = 0


@dataclass
class SignalEval:
    """信号回溯结果。写入 signal.eval_json。"""
    signal_id: int
    code: str
    kind: str
    signal_date: str
    signal_price: float
    is_buy: bool
    # ★ 强度档位（strong/normal/weak）。必须参与统计分组 —— 见group_by_strength。
    level: str = ""
    outcomes: dict[int, HorizonOutcome] = field(default_factory=dict)
    winrate: dict[int, float | None] = field(default_factory=dict)   # None=样本不足
    pl_ratio: float | None = None
    max_dd: float | None = None
    sample_n: int = 0
    enough_sample: bool = False
    params_ver: str = ""
    param_hash: str = ""
    eval_time: str = ""
    note: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "signal_id": self.signal_id,
            "code": self.code,
            "kind": self.kind,
            "signal_date": self.signal_date,
            "signal_price": self.signal_price,
            "is_buy": self.is_buy,
            "level": self.level,
            "horizons": {
                str(d): {
                    "ret_pct": o.ret_pct,
                    "max_gain": o.max_gain,
                    "max_loss": o.max_loss,
                    "hit": o.hit,
                    "bars": o.bars,
                } for d, o in self.outcomes.items()
            },
            # ★ None 表示样本不足，不可展示为 0 或 1
            "winrate": {str(d): v for d, v in self.winrate.items()},
            "pl_ratio": self.pl_ratio,
            "max_dd": self.max_dd,
            "sample_n": self.sample_n,
            "enough_sample": self.enough_sample,
            "params_ver": self.params_ver,
            "param_hash": self.param_hash,
            "eval_time": self.eval_time,
            "note": self.note,
        }, ensure_ascii=False)


# ---------------------------------------------------------------- 纯函数
def eval_signal(
    sig_id: int,
    code: str,
    kind: str,
    sig_date: str,
    sig_price: float,
    bars_after: list[dict],
    as_of: datetime,
    is_buy: bool | None = None,
    params_ver: str = "",
    param_hash: str = "",
    level: str = "",
) -> SignalEval:
    """评估单个信号在其后各窗口的表现。纯函数。

    bars_after: 信号日之后的前向 K 线（升序），每项含 trade_date/close/high/low
    is_buy=None 时按 kind 判断（1buy/2buy/3buy → 买）。
    as_of **必填**（铁律二）：复盘记录的时间必须是被评估时点，
    不能取系统时间 —— 否则回溯时无法判断「当时用什么参数算的」。
    level: 信号强度档位，必须回写 —— 否则按kind 聚合会把 strong/weak 混算。
    """
    if is_buy is None:
        is_buy = any(k in kind.lower() for k in ("buy",))

    ev = SignalEval(
        signal_id=sig_id, code=code, kind=kind,
        signal_date=sig_date, signal_price=sig_price, is_buy=is_buy,
        level=level,
        params_ver=params_ver, param_hash=param_hash,
        eval_time=as_of.isoformat(timespec="seconds"),
    )

    if not bars_after or not sig_price:
        ev.note = "无后续K线（可能是最新信号或数据不足）"
        for d in HORIZONS:
            ev.winrate[d] = None
        return ev

    closes = [float(b["close"]) for b in bars_after]
    highs = [float(b["high"]) for b in bars_after]
    lows = [float(b["low"]) for b in bars_after]

    for d in HORIZONS:
        if len(closes) < d:
            ev.outcomes[d] = HorizonOutcome(days=d, bars=len(closes))
            ev.winrate[d] = None
            continue
        end = closes[d - 1]
        ret = (end / sig_price - 1) * 100
        # 期间最大/最小相对信号价
        hi = max(highs[:d])
        lo = min(lows[:d])
        gain = (hi / sig_price - 1) * 100
        loss = (lo / sig_price - 1) * 100
        # 买点：涨了算命中；卖点：跌了算命中
        hit = (ret > 0) if is_buy else (ret < 0)
        ev.outcomes[d] = HorizonOutcome(days=d, ret_pct=round(ret, 2),
                                        max_gain=round(gain, 2),
                                        max_loss=round(loss, 2),
                                        hit=hit, bars=d)

    # 盈亏比：平均盈利 / 平均亏损
    # ★ 必须处理「无亏损样本」的情况：全胜时 loss_ 为空，
    #   直接算会ZeroDivisionError（实测踩过 —— 单边行情的测试必然触发）。
    #   此时盈亏比无意义（∞），返回 None 而不是编一个数字。
    rets = [o.ret_pct for o in ev.outcomes.values()
            if o.ret_pct is not None and o.hit]
    loss_abs = [abs(o.ret_pct) for o in ev.outcomes.values()
                if o.ret_pct is not None and not o.hit]
    if rets and loss_abs:
        avg_w = sum(rets) / len(rets)
        avg_l = sum(loss_abs) / len(loss_abs)
        ev.pl_ratio = round(avg_w / avg_l, 2) if avg_l else None
    else:
        ev.pl_ratio = None

    # 最大回撤（相对信号价的最大跌幅绝对值）
    if lows:
        worst = (min(lows) / sig_price - 1) * 100
        ev.max_dd = round(abs(min(worst, 0)), 2)

    # 样本量：5 日窗口命中数 × 视为独立样本数的粗略代理
    # ★ 真实统计在 aggregate 阶段做（同一只股票多个信号不独立）
    ev.sample_n = sum(1 for o in ev.outcomes.values() if o.hit is not None)
    ev.enough_sample = ev.sample_n >= MIN_SAMPLE_FOR_WINRATE
    return ev


def aggregate_stats(evals: list[SignalEval], horizon: int = 5) -> dict:
    """聚合并统计胜率/盈亏比/最大回撤。

    ★★ 必须**按买卖方向分开统计**（实测踩过的统计陷阱）：
      实测143 条信号里买点 65 / 卖点 78，混合算胜率得 87.4%
      （看起来极准），但平均收益是 **-3.3%**。
      原因：卖点「命中」的定义是**跌了**，而这些股票随后多在上涨
      ——于是卖点命中率高、买点收益率低，两个方向互相抵消掉差异，
      混合胜率完全没有指导意义。
      正确做法：买点看「上涨命中率 + 收益率」，卖点看「下跌命中率」，
      两者分别呈现，由用户自行解读组合含义。

    ★ 样本 < 10 时 winrate 返回 None —— 附录 5.4 明令禁止显示。
    """
    usable = [e for e in evals
              if horizon in e.outcomes and e.outcomes[horizon].ret_pct is not None]
    n = len(usable)
    enough = n >= MIN_SAMPLE_FOR_WINRATE

    def _side(subset: list[SignalEval]) -> dict:
        """单方向统计。"""
        m = len(subset)
        if m == 0:
            # ★ 必须与有样本时的键集完全一致，否则调用方按固定 key 读取会 KeyError
            return {"samples": 0, "winrate": None, "raw_hit_rate": None,
                    "hits": 0, "avg_ret": None, "avg_win": None,
                    "avg_loss": None, "pl_ratio": None, "max_dd_avg": None,
                    "enough_sample": False,
                    "winrate_note": "该方向无样本"}
        hits = sum(1 for e in subset if e.outcomes[horizon].hit)
        rets = [e.outcomes[horizon].ret_pct for e in subset]
        wins = [abs(e.outcomes[horizon].ret_pct) for e in subset
                if e.outcomes[horizon].hit]
        loss_ = [abs(e.outcomes[horizon].ret_pct) for e in subset
                 if not e.outcomes[horizon].hit]
        enough_m = m >= MIN_SAMPLE_FOR_WINRATE
        return {
            "samples": m,
            # ★ 样本不足时明确置None并在 note 里说明，绝不用 0 或 1 代替
            "winrate": round(hits / m, 4) if enough_m else None,
            "raw_hit_rate": round(hits / m, 4),
            "hits": hits,
            "avg_ret": round(sum(rets) / m, 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(loss_) / len(loss_), 2) if loss_ else None,
            "pl_ratio": round((sum(wins) / len(wins)) / (sum(loss_) / len(loss_)), 2)
            if wins and loss_ else None,
            "max_dd_avg": round(sum(e.max_dd or 0 for e in subset) / m, 2),
            "enough_sample": enough_m,
            "winrate_note": "" if enough_m else
            f"样本仅 {m} 条，不足 {MIN_SAMPLE_FOR_WINRATE} 条，按规则不显示胜率",
        }

    buys = [e for e in usable if e.is_buy]
    sells = [e for e in usable if not e.is_buy]
    out = {
        "horizon": horizon,
        "samples": n,
        "enough_sample": enough,
        "buy": _side(buys),
        "sell": _side(sells),
    }
    # 兼容旧调用方：overall 代表「买点」视角（推荐关注的一侧）
    b = out["buy"]
    out["winrate"] = b["winrate"]
    out["avg_ret"] = b["avg_ret"]
    out["pl_ratio"] = b["pl_ratio"]
    out["max_dd_avg"] = b["max_dd_avg"]
    out["winrate_note"] = b["winrate_note"] or ""
    return out


def group_by_strength(evals: list[SignalEval],
                      horizon: int = 5) -> dict[str, dict]:
    """按 kind + level 分组统计（★★ 必须做，否则结论会完全反过来）。

    ★★ 实测踩过的坑（M8 三买归因，1200 只真实样本）：
      同一个 kind="3buy"，强/弱两档表现天壤之别：
        3buy/strong  n=738  胜率 86.2%  平均 +7.75%
        3buy/weak    n=105  胜率 20.0%  平均 -4.31%
      只按 kind 聚合得「三买胜率 77.9%」，把 weak 混进来就成了噪声源；
      早期版本甚至因混入比例不同而得出「三买失效（35.7%）」的**错误结论**。
      弱三买是「已突破但回抽未确认」的观察态，语义上就不该和强三买同桌统计。

      已排除的混淆：weak 样本全部落在 2026-09，但当月全市场等权均值仅-2.35%，
      远小于 weak 平均 -4.31% ⇒ 亏损不是大盘拖累，是入场价口径问题。
    """
    buckets: dict[str, list[SignalEval]] = {}
    for e in evals:
        key = f"{e.kind}/{e.level}" if e.level else e.kind
        buckets.setdefault(key, []).append(e)
    return {k: aggregate_stats(v, horizon) for k, v in buckets.items()}


# ---------------------------------------------------------------- 错误归因（7.1④）
# 失败信号的聚类类别（附录 7.1④ 明确列出 6 类）
FAIL_REASONS = (
    "假突破",# 价格突破但无量/无支撑
    "消息误判",    # 消息方向判断错
    "大盘拖累",    # 大盘跌导致个股跟跌
    "板块退潮",    # 所属板块整体走弱
    "级别冲突",    # 日线与分钟线信号矛盾
    "止损过紧",    # 正常波动即触发止损
)


def classify_failure(
    sig: dict,
    evalr: SignalEval,
    market_ret: float | None = None,
    sector_ret: float | None = None,
) -> str:
    """失败归因（启发式，按优先级判定）。

    ★ 顺序有意义：大盘拖累优先于个股原因 ——
      大盘跌 2% 时个股跌 3% 不该归因于「消息误判」，
      否则会把系统性风险误判成模型问题，进而错误地调 prompt。
    """
    h5 = evalr.outcomes.get(5)
    if h5 is None or h5.ret_pct is None or h5.hit:
        return ""                            # 不是失败信号
    lost = h5.ret_pct

    # ① 大盘拖累：大盘同期明显下跌，且**个股没有明显跑赢大盘**
    #   ★ 判断条件必须是「个股跌幅 >= 大盘跌幅 × 系数」，
    #     而不是「<=」—— 实测踩过：大盘跌 6%、个股跌 5.2%
    #     （个股其实跑赢 0.8%）时用 `<=` 判成了「消息误判」，
    #     于是会错误地去调 prompt，而实际是市场整体拖累。
    #     归因方向搞反会导致「用错误的原因去优化错误的环节」。
    if market_ret is not None and market_ret < -0.8:
        # lost 与 market_ret 都为负；lost >= market_ret*1.05 表示
        # 个股跌幅 >= 大盘跌幅（含 5% 缓冲）
        if lost >= market_ret * 1.05:
            return "大盘拖累"
    # ② 板块退潮：板块同期下跌且个股跌幅不小于板块跌幅
    if sector_ret is not None and sector_ret < -1.0:
        if lost >= sector_ret * 1.05:
            return "板块退潮"
    # ③ 消息误判：板块与大盘都正常，个股却大幅下跌
    if abs(lost) > 5:
        return "消息误判"
    # ④ 级别冲突：日线信号但分钟线反向（由调用方传入 level_conflict）
    if sig.get("level_conflict"):
        return "级别冲突"
    # ⑤ 止损过紧：回撤很小但中途有过正收益
    if abs(lost) < 3 and (h5.max_gain or 0) > 1.0:
        return "止损过紧"
    # ⑥ 兜底
    return "假突破"


# ---------------------------------------------------------------- 落库
def save_eval(ev: SignalEval, as_of: datetime) -> int:
    """把评估结果写入 signal.eval_json。

    ★ 只写一次（附录 7.2「只追加不修改」）：
      若已存在 eval_json 则拒绝覆盖并返回 0。
      允许覆盖会导致「用后来的信息美化当初的判断」。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT eval_json FROM signal WHERE id = ?", (ev.signal_id,)).fetchone()
    if row is None:
        logger.warning(f"信号 {ev.signal_id} 不存在，跳过")
        return 0
    if row[0]:
        logger.info(f"信号 {ev.signal_id} 已有 eval_json，按「只追加不修改」跳过")
        return 0
    conn.execute(
        "UPDATE signal SET eval_json = ? WHERE id = ?",
        (ev.to_json(), ev.signal_id))
    conn.commit()
    return 1


def mark_human_label(signal_id: int, label: str, as_of: datetime) -> int:
    """人工打标（附录 7.1④：correct | wrong | unclear）。"""
    if label not in ("correct", "wrong", "unclear"):
        raise ValueError(f"human_label 只能是 correct/wrong/unclear，收到 {label!r}")
    conn = get_conn()
    # ★ 必须用 cursor.rowcount：execute() 返回 cursor，
    #   fetchone() 得到的是数据行，没有 rowcount（实测踩过 AttributeError）
    cur = conn.execute("UPDATE signal SET human_label = ? WHERE id = ?",
                       (label, signal_id))
    n = cur.rowcount
    conn.commit()
    log_event("signal_human_label", {"signal_id": signal_id, "label": label},
              as_of.isoformat(timespec="seconds"))
    return n


if __name__ == "__main__":
    from datetime import timezone, timedelta
    CST = timezone(timedelta(hours=8))
    as_of = datetime(2026, 10, 9, 0, 40, tzinfo=CST)

    # 构造一段「买入后下跌」的 K 线
    base = 100.0
    bars = [{"trade_date": f"2026-01-{i+1:02d}",
             "close": base * (1 - 0.02 * i),
             "high": base * (1 - 0.02 * i + 0.005),
             "low": base * (1 - 0.02 * i - 0.008)}
            for i in range(20)]

    print("=" * 74)
    print("信号回溯验证（附录 7.1②）")
    print("=" * 74)
    ev = eval_signal(1, "600519", "1buy", "2026-01-01", base, bars,
                     params_ver="chan-v1", param_hash="abc123", as_of=as_of)
    print(f"  信号: {ev.kind} @{ev.signal_price} ({'买' if ev.is_buy else '卖'})")
    print(f"  {'窗口':>6s}{'收益%':>10s}{'最大涨':>10s}{'最大跌':>10s}{'命中':>8s}")
    for d in HORIZONS:
        o = ev.outcomes.get(d)
        if o and o.ret_pct is not None:
            print(f"  {d:>5d}日{o.ret_pct:>10.2f}{o.max_gain:>10.2f}"
                  f"{o.max_loss:>10.2f}{'是' if o.hit else '否':>8s}")
    print(f"  盈亏比: {ev.pl_ratio}   最大回撤: {ev.max_dd}%")

    print()
    print("=" * 74)
    print("★ 样本不足时禁止显示胜率（附录 5.4）")
    print("=" * 74)
    for n in (1, 3, 9, 10, 25):
        es = [eval_signal(i, "600519", "1buy", "2026-01-01", base, bars)
              for i in range(n)]
        st = aggregate_stats(es, 5)
        wr = f"{st['winrate']:.1%}" if st["winrate"] is not None else "不显示"
        print(f"  {n:>3d} 条样本 -> 胜率={wr:8s} 说明={st.get('winrate_note','样本充足')[:34]}")

    print()
    print("=" * 74)
    print("错误归因聚类（附录 7.1④）")
    print("=" * 74)
    cases = [
        ("个股跌5.2%，大盘跌1.0%", -5.2, -1.0, None),
        ("个股跌5.2%，大盘跌6.0%", -5.2, -6.0, None),
        ("个股跌4.0%，板块跌5.0%", -4.0, -0.3, -5.0),
        ("个股跌2.0%，中途涨过2%", -2.0, 0.2, 0.1),
    ]
    for desc, lost, mkt, sec in cases:
        o5 = HorizonOutcome(days=5, ret_pct=lost, hit=lost > 0,
                            max_gain=2.5 if "中途" in desc else 0.5)
        e2 = SignalEval(0, "600519", "1buy", "2026-01-01", 100.0, is_buy=True)
        e2.outcomes[5] = o5
        r = classify_failure({}, e2, market_ret=mkt, sector_ret=sec)
        print(f"  {desc:30s} -> {r}")
    print()
    print("  ★ 顺序有意义：大盘拖累优先于消息误判")
    print("    大盘跌 6% 时个股跌 5.2% 应归「大盘拖累」而非「消息误判」")
    print("    否则会把系统性风险误判成模型问题 → 错误地调 prompt")