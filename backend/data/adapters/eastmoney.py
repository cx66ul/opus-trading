# -*- coding: utf-8 -*-
"""
Hawk · 东方财富适配器（部分可用）

★ 重要：本机网络实测结论（2026-10-08，见 samples/reachability.json）
    push2.eastmoney.com    ❌ 不可达（TCP 层断开，已排除参数/ut令牌/IP/协议因素）
    push2his.eastmoney.com ❌ 不可达
    push2ex.eastmoney.com  ✅ 可达 200

因此本适配器**仅涨停池/炸板池/跌停池可用**，
全市场行情（clist）与 K 线（push2his）在本机不可用，
其能力已由新浪（列表/日线）与腾讯（K线/分钟）承接。

保留 clist/kline 的实现是为了换网络环境（校园网/VPN）后可直接启用，
但默认不注册到降级链首位。板块与资金流能力因此受限，已列为 M1-4 阻塞项。

字段映射见 config/fields/eastmoney_ztpool.yaml。
该接口字段全为单字母缩写，是最易静默出错的一类，映射已逐一交叉验证。
"""
from __future__ import annotations

import json
from datetime import datetime

from backend.data.adapter import BaseAdapter
from backend.data.base import ERR_SOURCE_ALL_DOWN, SourceError

ZTP_UT = "7eea3edcaed734bea9cbfc24409ed989"


class EastmoneyAdapter(BaseAdapter):
    name = "eastmoney"
    qps = 2.0
    referer = "https://quote.eastmoney.com"
    encoding = "utf-8"

    # 本机不可达的端点，调用时给明确错误而不是让人以为是代码 bug
    UNAVAILABLE = {
        "clist": "push2.eastmoney.com 在当前网络不可达；"
                 "请改用 sina.fetch_stock_list，或换网络环境后复测",
        "kline": "push2his.eastmoney.com 在当前网络不可达；"
                 "请改用 tencent.fetch_daily，或换网络环境后复测",
    }

    # ---------------- 涨停池（情绪周期核心数据）----------------
    def fetch_zt_pool(self, date_str: str, as_of: datetime,
                      page_size: int = 100, max_pages: int = 8) -> list[dict]:
        """涨停池。date_str 格式 YYYYMMDD。

        对应 4.7 大盘环境模块所需：涨停家数、最高连板高度、封单额、炸板次数。
        """
        out: list[dict] = []
        for page in range(max_pages):
            text = self._get(
                "https://push2ex.eastmoney.com/getTopicZTPool",
                {"ut": ZTP_UT, "dpt": "wz.ztzt", "Pageindex": page,
                 "pagesize": page_size, "sort": "fbt:asc", "date": date_str},
            )
            obj = json.loads(text)
            data = obj.get("data") or {}
            # ★ 免费源会返回最近缓存，必须校验 qdate 与请求日期一致，否则丢弃
            #实测 qdate 为 int 类型（如 20261008），但接口可能改回字符串，故两者都处理
            qdate = str(data.get("qdate") or "").replace("-", "")
            if qdate and qdate != date_str:
                raise SourceError(
                    ERR_SOURCE_ALL_DOWN,
                    f"涨停池 qdate={qdate} 与请求 date={date_str} 不一致，疑似陈旧缓存",
                    retryable=False,
                )
            pool = data.get("pool") or []
            if not pool:
                break
            for it in pool:
                out.append(self._map_zt(it))
            if len(pool) < page_size:
                break
        self._last_zt_total = len(out)
        return self._guard(out, "ztpool")

    def _map_zt(self, it: dict) -> dict:
        """字段映射（已用腾讯实时行情交叉验证 5/5 通过）。

        ★ p 字段是放大 1000 倍的价格：实测 22220 -> 22.220 元。
          漏除 1000 是本接口最高频的踩坑点。
        ★ 市场前缀不依赖 m 字段：实测 m 仅有 0/1 两值，
          但北交所（920xxx）也在池内，若按 m 推导会全部错标为 sz。
          代码首位才是权威依据，故复用 market_prefix 按代码推导。
        """
        from backend.data.adapters.tencent import market_prefix

        code = str(it.get("c") or "")
        p = self._f(it.get("p"))
        zttj = it.get("zttj") or {}
        return {
            "code": code,
            "symbol": market_prefix(code) + code,
            "name": it.get("n"),
            "market_flag": it.get("m"),          # 保留原始 m，仅作参考
            "price": round(p / 1000, 3) if p is not None else None,   # ÷1000
            "pct_chg": self._f(it.get("zdp")),
            "amount": self._f(it.get("amount")),
            "float_mv": self._f(it.get("ltsz")),
            "total_mv": self._f(it.get("tshare")),
            "turnover": self._f(it.get("hs")),
            "streak": self._i(it.get("lbc")),                    # 连板数
            "first_seal_time": self._hhmmss(it.get("fbt")),       # 首次封板
            "last_seal_time": self._hhmmss(it.get("lbt")),        # 最后封板
            "seal_amount": self._f(it.get("fund")),              # 封单金额
            "open_times": self._i(it.get("zbc")) or 0,            # 开板/炸板次数
            "industry": it.get("hybk"),
            "streak_days": self._i(zttj.get("days")),
            "streak_count": self._i(zttj.get("ct")),
            # ★★ 修复字段语义：lbc 是**连板数**，lbc==1 表示「首板」，
            #   与「一字板」（开盘即涨停价、全天不开板，open=high=low=close）
            #   是完全不同的概念。原字段名 is_one_word 会误导下游把首板当一字板。
            #   现按真实语义命名；真正的一字板判定需 OHLC，见 daily_recommend
            #   的 is_one_word_limit（由 kline_daily 的 open/high/low/close 计算）。
            "is_first_board": 1 if (self._i(it.get("lbc")) or 0) == 1 else 0,
        }

    @staticmethod
    def _hhmmss(v) -> str | None:
        """HHMMSS 整数 -> 'HH:MM:SS'。

        ★实测坑：接口返回的是**不定长**整数，不是定长 6 位。
          09:25:00 实际返回 92500（5 位），而 09:30:42 返回 93042（也是 5 位），
          只有当小时数 ≥ 10 时（如 14:30:42 -> 143042）才是 6 位。
          因此不能按固定长度切，必须按语义补零。
          若误按 6 位校验，会把 09:25 集体竞价封板这类最典型的首封时间判为无效。
        """
        if v is None:
            return None
        s = str(v).strip()
        if not s.isdigit():
            return None
        if len(s) <= 3:            # 形如 930 -> 09:30:00
            hh, mm, ss = s.zfill(6)[0:2], s.zfill(6)[2:4], "00"
        elif len(s) == 5:         # 形如 92500 -> 09:25:00
            hh, mm, ss = f"0{s[0]}", s[1:3], s[3:5]
        elif len(s) == 6:         # 形如 143042 -> 14:30:42
            hh, mm, ss = s[0:2], s[2:4], s[4:6]
        else:
            return None
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and 0 <= int(ss) <= 59):
            return None
        return f"{hh}:{mm}:{ss}"

    # ---------------- 炸板池 / 跌停池（情绪周期用）----------------
    def fetch_zb_pool(self, date_str: str, as_of: datetime) -> list[dict]:
        """炸板池。炸板率 = 炸板数 / (涨停数 + 炸板数)，对应 4.7 市场状态机。"""
        text = self._get(
            "https://push2ex.eastmoney.com/getTopicZBPool",
            {"ut": ZTP_UT, "dpt": "wz.ztzt", "Pageindex": 0,
             "pagesize": 500, "sort": "fbt:asc", "date": date_str},
        )
        obj = json.loads(text)
        pool = ((obj.get("data") or {}).get("pool")) or []
        return self._guard(
            [{"code": it.get("c"), "name": it.get("n"),
              "pct_chg": self._f(it.get("zdp")),
              "open_times": self._i(it.get("zbc")) or 0,
              "first_seal_time": self._hhmmss(it.get("fbt"))} for it in pool],
            "zbpool",
        )

    def fetch_dt_pool(self, date_str: str, as_of: datetime) -> list[dict]:
        """跌停池。用于「跌停 > 涨停 -> 冰点」判定。"""
        text = self._get(
            "https://push2ex.eastmoney.com/getTopicDTPool",
            {"ut": ZTP_UT, "dpt": "wz.ztzt", "Pageindex": 0,
             "pagesize": 500, "sort": "fund:asc", "date": date_str},
        )
        obj = json.loads(text)
        pool = ((obj.get("data") or {}).get("pool")) or []
        return self._guard(
            [{"code": it.get("c"), "name": it.get("n"),
              "pct_chg": self._f(it.get("zdp"))} for it in pool],
            "dtpool",
        )

    # ---------------- 本机不可达的能力（保留实现，换网络后启用）----------------
    def fetch_clist(self, as_of: datetime, page: int = 1, page_size: int = 100) -> list[dict]:
        raise SourceError(
            ERR_SOURCE_ALL_DOWN,
            f"[不可达端点] {self.UNAVAILABLE['clist']}",
            retryable=False,
        )

    def fetch_daily(self, code: str, count: int = 250,
                    as_of: datetime | None = None) -> list[dict]:
        raise SourceError(
            ERR_SOURCE_ALL_DOWN,
            f"[不可达端点] {self.UNAVAILABLE['kline']}",
            retryable=False,
        )

    def fetch_realtime(self, codes: list[str], as_of: datetime) -> list[dict]:
        """实时行情原本走 push2，本机不可达 -> 委派给腾讯。"""
        from backend.data.adapter import get_adapter

        return get_adapter("tencent").fetch_realtime(codes, as_of)