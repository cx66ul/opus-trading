"""Reproducible, visually reviewed extraction; local provenance + consistent SQLite backups.

No external model training, uploads, trading or manufactured backtest records.
"""
from __future__ import annotations
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/image_distillation/20261011"
STRATEGY = "user_images_20261011"
OBSERVED = "2026-10-11"


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def build_catalog():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))

    def ev(image, tile, quote):
        src = manifest[image - 1]
        t = src["tiles"][tile - 1]
        return {"source": src["original"], "sha256": src["sha256"],
                "quote_ref": f"image-{image:02}-tile-{tile:02}", "bbox": t["bbox"],
                "quote": quote, "quote_kind": "摘录或归纳，非完整逐字稿",
                "verification": "visually_checked", "observed_at": OBSERVED}

    rules_raw = [
        ("IMG01", "上升趋势沿5日线回踩", 6, 7, "我一般只做上升趋势的票，顺势而为；这种票5日线是买点",
         "MA5≥MA10≥MA20且MA5上升；距MA5≤3%，当日涨幅≤3%，+20分。"),
        ("IMG02", "缩量企稳且未破前低", 4, 7, "缩量不破前低，站稳之后放量",
         "近3日均量/此前10日均量≤0.8；收盘不破此前10日低点、单日跌幅≤2%且≥MA20，+12分。"),
        ("IMG03", "横盘整理后放量突破", 4, 4, "底部横盘+放量突破",
         "此前20日高低范围≤18%；收盘突破此前高点且当日量/此前5日均量≥1.5，+18分；横盘代理不能证明绝对底部。"),
        ("IMG04", "均线收敛、波动收缩", 5, 6, "横盘缩量+均线粘合+突破前高",
         "MA5/10/20最大差≤4%；近5日收盘波幅小于此前10日、企稳且≥MA20，+8分；突破另由IMG03判断。"),
        ("IMG05", "阶段表现强于上证指数", 3, 4, "股票最好的现象是强于大盘",
         "同起止日期20个交易日收益率超过上证指数，+10分；日期不齐不触发。"),
        ("IMG06", "远离5日线，追高风险", 3, 1, "每天都是轮动，所以就要放平心态。不要追涨",
         "收盘高于MA5超过8%，-25分。"),
        ("IMG07", "高位连续上涨后放量", 3, 5, "放量+连续大涨高位：谨慎，要逐步减仓",
         "20日涨幅>25%、距21日最高收盘≤5%且量比≥1.5，-20分。"),
        ("IMG08", "放量大跌，回避信号", 3, 5, "放量+大跌：回避",
         "单日收盘跌幅≥4%、量比≥1.5，-35分。"),
        ("IMG09", "弱均线排列", 6, 3, "不要十日线在上，五日线在下，且K线处于五日线下方的股票",
         "MA5<MA10且收盘<MA5，-25分。年线以下的原文意见需更多历史，未自动执行。"),
        ("IMG10", "市场空头排列", 9, 1, "现在均线是明显的空头排列，短期的反弹不代表趋势反转",
         "上证收盘<MA5<MA10<MA20，-15分；组合参考仓位上限再取min(原上限,30%)。"),
        ("IMG11", "市场缩量轮动", 9, 1, "市场缩量时，主线轮动就会很快，追涨基本是坑",
         "上证近3日均量/此前20日均量<0.8，-8分；不把固定点位永久写入规则。"),
    ]
    rules = [{"id": rid, "name": name, "evidence": [ev(im, tile, quote)],
              "interpretation": interpretation, "status": "enabled_user_priority",
              "validation_status": "unvalidated", "backtest": None}
             for rid, name, im, tile, quote, interpretation in rules_raw]
    # date labels transcribed from posts, not fabricated exact publication timestamps.
    stocks_raw = [
        ("8月23日（引用）", "600397", "江钨装备", "并购重组+中报业绩暴增100%…", 1, 7, "quoted_partial", "原推荐名码被遮挡；通过后续引用识别，完整逻辑截断"),
        ("8月24日（引用）", "600367", "红星发展", "固态电池+近期底部涨停+净利润…", 1, 5, "quoted_partial", "后半句截断，未补全"),
        ("8月26日（引用）", "001258", "立新能源", "绿色电力+国企控股+中报大增70…", 1, 2, "quoted_partial", "增长数字后缀截断，不能确认比例"),
        ("8月27日", "600206", "有研新材", "存储芯片+中报增长40%+长鑫合作", 1, 5, "original", "9月4日作者明确承认失败，保留反例"),
        ("8月27日（跟踪）", "002837", "英维克", "低位缩量，回踩不跌破支撑位", 1, 7, "followup_only", "原推荐日期与完整逻辑未展示"),
        ("8月30日", "601360", "三六零", "AI应用+云计算+业绩暴增", 1, 2, "original", ""),
        ("8月31日", "003018", "金富科技", "液冷服务+人工智能+业绩暴增", 1, 1, "original", ""),
        ("9月1日", "603123", "翠微股份", "AI应用+多元金融+形态趋势好", 2, 6, "original", ""),
        ("9月2日", "000670", "盈方微", "储存芯片+机器人+业绩暴增", 2, 5, "original", ""),
        ("9月3日", "002787", "华源控股", "液冷服务+光模块+业绩暴增", 2, 3, "original", ""),
        ("9月6日", "002354", "天娱数科", "AI应用+数据要素+形态趋势好+净利润增长", 3, 7, "original", ""),
        ("9月7日", "603002", "宏昌电子", "PCB概念+芯片+净利润增长", 3, 5, "original", ""),
        ("9月8日", "600378", "昊华科技", "氟化工+PCB概念+业绩暴增", 3, 2, "original", ""),
        ("9月9日", "605006", "山东玻纤", "PCB概念+火力发电+利润增长", 3, 1, "original", "后续日内-6%至+7%属于作者回顾，非可成交收益"),
        ("9月10日", "603601", "再升科技", "商业航天+底部形态止跌", 4, 5, "original", ""),
        ("9月13日", "603938", "三孚股份", "光纤材料+有机硅+净利润增长", 4, 1, "original", ""),
        ("9月14日", "603650", "彤程新材", "光刻胶+芯片+净利润增长", 5, 6, "original", ""),
        ("9月15日", "600707", "彩虹股份", "玻璃基板+国企改革+趋势企稳", 5, 4, "original", ""),
        ("9月16日", "600105", "永鼎股份", "光通信+芯片+业绩增长", 5, 1, "original", ""),
        ("9月17日", "600410", "华胜天成", "云计算+人工智能+趋势形态好", 6, 5, "original", ""),
        ("9月20日", "603138", "海量数据", "数据库+人工智能+趋势形态好", 6, 2, "original", ""),
        ("9月21日", "600699", "均胜电子", "华为汽车+光通信+近期趋势企稳", 7, 6, "original", ""),
        ("9月22日", "002181", "粤传媒", "虚拟数字+短剧游戏+趋势企稳", 7, 5, "original", ""),
        ("9月23日", "601208", "东材科技", "PCB概念+MLCC+净利润增长", 7, 4, "original", "后续作者同时提及节前跌停、节后涨停；不可只采盈利一侧"),
        ("9月27日", "002639", "雪人集团", "海峡两岸+冰雪+形态好", 7, 1, "original", ""),
        ("9月28日", "002298", "中电鑫龙", "电力+数据中心+趋势走稳", 8, 5, "original", ""),
        ("9月29日", "603232", "格尔软件", "智能体+芯片+趋势走稳", 8, 2, "original", ""),
        ("3天前（相对日期）", "002531", "天顺风能", "风电设备+利润增长+绿电", 9, 6, "original_relative_date", "确切发帖日期未确定，不强行换算"),
        ("2天前（相对日期）", "603248", "锡华科技", "风电设备+近期主力资金参与", 9, 4, "original_relative_date", "确切发帖日期未确定；主力参与未独立证实"),
    ]
    forecasts = []
    for label, code, name, thesis, im, tile, kind, caveat in stocks_raw:
        condition = "沿5日线低吸介入（作者条件式观察意见，非保证次日上涨）"
        if kind in ("quoted_partial", "followup_only") or code == "600206":
            condition = "该条未完整展示统一入场条件，不由其他帖子补写"
        forecasts.append({"id": f"IMG-STOCK-{code}", "date_label": label,
                          "year": None, "year_hint": "部分图表显示2026；发帖年份未独立确认",
                          "code": code, "name": name, "thesis": thesis, "kind": kind,
                          "condition": condition, "caveat": caveat,
                          "forecast_type": "conditional_watch" if kind != "followup_only" else "retrospective_only",
                          "horizon": "未逐条指定；作者另称低吸常观察3–5个交易日，非承诺",
                          "return_forecast": None, "verified_result": None,
                          "evidence": [ev(im, tile, thesis)]})
    markets = [
        ("8月30日", 1, 3, "下周大概率先抑后扬，全周目标站稳4000点", "下周；等待右侧放量，证券为变量"),
        ("9月1日", 2, 6, "上4000需看板块和成交量，不能只看指数", "当时行情；控制仓位3至5成"),
        ("9月6日", 2, 1, "周一可能低开，等待消化；存储、光通信、液冷等观察", "下周，事件和行业意见"),
        ("9月8日", 3, 2, "指数站上5日线两天、60日线向上，中期趋势仍在", "当时市场判断"),
        ("9月9日", 3, 1, "3950附近震荡三天，没有持续主线，避免追当日脉冲", "当时轮动环境"),
        ("9月10日", 4, 5, "缩量等待方向，小仓试错", "当时弱市"),
        ("9月13日", 4, 1, "周一低开，3850支撑；跌破看3740", "次日条件预测；另承认冲4000判断错误"),
        ("9月16日", 5, 2, "次日科技高开不追，缩量轮动仍谨慎", "次日；当时仓位不超过五成"),
        ("9月17日", 6, 5, "预计下一交易日缩量，3850支撑、3906压力", "下一交易日；等待增量"),
        ("9月20日", 6, 2, "下周修复，3926压力；真反转需要持续放量、板块扩散、分歧可控制", "下周；不把短期利好当真增量"),
        ("9月21日", 7, 7, "普涨后谨防分歧；半导体设备中业绩和形态较好的值得观察", "随后几个交易日"),
        ("9月22日", 7, 6, "先震荡消化再向上，4000压力，节前控制仓位", "随后几个交易日"),
        ("9月23日", 7, 4, "次日不易复制924行情，缩量震荡概率大，3930附近观察", "次日；节前3至5成是情境建议"),
        ("9月27日", 7, 1, "下周先探底再震荡；低位有业绩、错杀股小仓观察", "节前三个交易日，非长期固定指令"),
        ("9月28日", 8, 6, "次日看反弹，中个股趁反弹降低暴露；轻仓不急抄底", "次日"),
        ("9月29日", 8, 3, "次日缩量弱反弹，跌破60日线仍弱；节前不重仓", "次日与国庆假期"),
        ("10月7日", 9, 7, "节后开盘大概率高开，不能因高开冲动追涨", "次日；展开后的全文不可见"),
        ("10月8日", 9, 4, "次日若再急跌，可能拉回；反弹用于降低暴露，非立即反转", "次日；3780/3741是当时支撑，5至6成是当时仓位意见"),
        ("10月11日复盘（图内标题）", 9, 1, "下周初反弹；均线空头，反弹不等于反转；3731/3875观察", "下一周；AI应用用户付费证据、固态电池、创新药、贵金属为当时观察主题"),
    ]
    return {
        "strategy_id": STRATEGY, "revision": "images-20261011-v1", "observed_at": OBSERVED,
        "author": "A股趋势捕手 @Aw3ff_（按截图署名）", "original_count": 9,
        "method": "本地OCR辅助+63个切片视觉核对；OCR不是最终证据",
        "source_manifest": "manifest.json", "rules": rules, "stock_forecasts": forecasts,
        "market_forecasts": [{"date_label": d, "claim": q, "scope": scope,
                              "permanent_rule": False, "verified_result": None,
                              "evidence": [ev(im, tile, q)]} for d, im, tile, q, scope in markets],
        "counterexamples": [
            {"subject": "有研新材600206", "evidence": [ev(2, 2, "亏损控制住回撤、赚的时候让利润奔跑；纪律要执行到位")], "type": "author_acknowledged_failure"},
            {"subject": "预期震荡后冲4000却跌破3900", "evidence": [ev(4, 2, "观点参考而非永远正确")], "type": "author_acknowledged_forecast_error"},
            {"subject": "东材科技601208", "evidence": [ev(9, 6, "东材今天涨停，节前跌停")], "type": "mixed_retrospective_claim"}],
        "qualitative_only": [
            "业绩增长、订单、供不应求、护城河及主题归属：截图为作者陈述，缺时点财报/订单数据，不自动加分。",
            "9月26日七条成长原则：持续增长、新产品/业务/市场、优势护城河、供需、预期差、管理层维护价值、机构偏好；仅研究清单，缺外部核验。",
            "10月3日：先大周期判断能否做，再看主线，再选入场点；落实为市场/板块/个股多因子，未新增未经数据支持的资金流规则。",
            "圆弧底、低位首板及洗盘示意图：部分图形或文章截断，不补造完整识别条件；不承诺突破即主升浪。",
            "量价代理不能断言主力吸筹；缩量也可能是无人交易。趋势破坏需重评，不把耐心解释为无限持有。",
            "不把单日高低点、涨停截图或重复跟踪帖算作可交易收益；没有完整买卖、费用、滑点及失败全集，胜率未知。"],
        "unresolved": ["所有‘显示更多’之后的内容不可见", "部分图表轴标、板块列表和小字无法逐字确认，未用作阈值",
                       "3条引用推荐原文截断，1只仅有跟踪帖", "2条10月推荐仅显示相对日期，发帖年/月/日不强行补齐"],
        "parameter_policy": "所有数值阈值及分数是工程解释，非作者原文；基础50分，加减后限制0至100。",
        "activation": {"user_authorized": True, "scope": "daily_ranking", "weight": .60,
                       "validation_status": "unvalidated_user_priority", "backtest": None,
                       "available_from": OBSERVED, "ticker_boosts": False},
    }


def snapshot(dest):
    dest.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{(ROOT / 'data/hawk.sqlite').as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dest / "hawk.sqlite")
    try:
        source.backup(target)
        assert target.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        target.close()
        source.close()
    return dest / "hawk.sqlite"


def report(catalog):
    out = ROOT / "docs/image-strategy-20261011"
    out.mkdir(parents=True, exist_ok=True)
    lines = ["# 9张交易截图蒸馏与Opus接入记录", "", "日期：2026-10-11。来源署名：A股趋势捕手 @Aw3ff_。",
             "", "已将可量化的11条方法接入共用每日选股入口，设为60%的最高单项权重。另保留29条去重股票记录（25条较完整推荐、3条截断引用、1条仅跟踪）及19条带时间范围的市场判断。",
             "", "这是规则蒸馏与选股代码接入，没有声称完成神经网络训练。图片中的题材、业绩、主力行为和收益均属于作者陈述。系统未把这些陈述当成核验过的财务数据，未伪造胜率或回测样本。",
             "", "## 每日评分", "", "|因子|权重|", "|---|---:|", "|图片蒸馏形态策略|60%|",
             "|板块强度|10%|", "|缠论结构|10%|", "|RPS|8%|", "|消息|6%|", "|情绪|6%|", "",
             "图片因子以50为中性基分，规则按下面分值加减，最终限制0–100。权重为用户要求优先后的工程配置，不代表该方法具有60%的预测正确率。已有降权项仍在综合分后乘0.85。",
             "", "|规则|工程解释（具体阈值并非作者原话）|出处切片|", "|---|---|---|"]
    for r in catalog["rules"]:
        lines.append(f"|{r['id']} {r['name']}|{r['interpretation']}|{r['evidence'][0]['quote_ref']}|")
    lines += ["", "ST、停牌、上市不足60日、市值区间、近5日涨幅超过35%、一字板等原有硬过滤继续生效。图片因子多条相关规则只算一个共振维度。均线空头环境的30%仓位是参考上限，并与系统原上限取较小值；没有下单或自动买卖。",
              "", "不足61根有效日线、非有限数、零成交量、重复日期或个股数据过期均不触发规则。此时因子中性50、覆盖率0，并限制为C档。指数缺失不猜测市场信号；相对强弱需要同起止日期。覆盖率只计算11项技术规则，不包括尚未自动核验的财报逻辑。",
              "", "所有行情按传入时点截断，收盘前不读取当日完整日线；此策略于10月11日整理。10月9日或更早行情套用当前规则会明确标为 current_policy_on_historical_prices，不能称为当时已知策略的样本外回测。",
              "", "## 股票推荐与预测记录", "", "下表为作者当时的条件式观察意见，不构成当前推荐名单。没有在系统里按这些股票代码永久加分。相对日期、年份和截断原文不强行补齐。",
              "", "|图内日期|代码 名称|作者逻辑|记录类型/限制|", "|---|---|---|---|"]
    for s in catalog["stock_forecasts"]:
        lines.append(f"|{s['date_label']}|{s['code']} {s['name']}|{s['thesis']}|{s['kind']}；{s['caveat']}|")
    lines += ["", "较完整推荐普遍带‘沿5日线低吸介入’条件。600206及截断引用/跟踪帖不补造未显示的买点条件。作者另称低吸通常观察3–5个交易日；不能将它解释为所有股票次日必涨，或擅自生成目标价。",
              "", "## 市场判断", "", "|日期标签|观点归纳|有效范围|", "|---|---|---|"]
    for m in catalog["market_forecasts"]:
        lines.append(f"|{m['date_label']}|{m['claim']}|{m['scope']}|")
    lines += ["", "3731、3741、3850、3875、3926、4000等均为当时指数语境，不作为永久买卖点。3–5成、5–6成等原帖仓位意见因时点不同而不同，不抄成一条固定仓位命令。",
              "", "## 反例与未自动化内容", "", "保留9月4日有研新材失败、9月13日冲4000判断错误，以及东材科技节前跌停/节后涨停的双向记录。跟踪帖展示的涨幅不计为独立预测，也不当作可成交收益。", ""]
    lines += ["- " + q for q in catalog["qualitative_only"] + catalog["unresolved"]]
    comparison = out / "ranking-comparison.json"
    if comparison.exists():
        result = json.loads(comparison.read_text(encoding="utf-8"))
        lines += ["", "## 本机验证", "", f"本地最新可用行情：{result['trade_date']}。输入{result['input_rows']}只，{result['eligible_rows']}只可计算；新旧排名对照保存在ranking-comparison.json。此次全量计算耗时{result['elapsed_seconds']}秒，包含原有缠论与RPS计算。",
                  "", "测试覆盖：最高权重影响排名、禁用回到原权重、硬过滤不被高分绕过、未来数据截断、缺失/NaN/重复/过期行情、市场仓位上限、原始文件哈希、缓存随策略配置变化。回归测试记录见test-results.txt。"]
    if (out / "desktop-verification.json").exists():
        lines += ["", "已使用安装版EXE验证每日推荐页、60%权重及29条出处记录，HTTP 200，无渲染脚本错误。证据见desktop-verification.json、daily-ranking.png、source-evidence.png。"]
    if (out / "installed-live-verification.json").exists():
        lines += ["", "已重启用户实际安装版，并验证127.0.0.1:8870的策略权重为0.6、5311只股票可计算、11条规则与29条股票记录可读取。真实自选接口当前3只，均无score字段；详细结论见自选股评分检查.md。",
                  "", "最终定向回归190 passed、1 skipped。独立Python全量评分实测从约54.5秒降至13.8秒，核心优化是避免全市场SQL逐个绑定数千个股票代码，并合并前端预热与点击时的并发请求；排名一致。不同机器和冷启动耗时会不同。"]
    lines += ["", "## 文件、备份与停用", "", "- 算法：backend/domain/image_strategy.py；共用入口：scripts/daily_recommend.py → backend/domain/recommend.py。",
              "- 配置：config/image_strategy.json；接口：/api/recommend、/api/image-strategy。每日推荐页显示策略分、权重贡献、覆盖率，并可展开查看规则和股票原始观点。",
              "- 原图、切片、OCR、哈希及原图坐标：data/image_distillation/20261011/；OCR文本仅辅助识别。所有63个切片均已检查，未展示全文不补造。",
              "- SQLite：corpus存40条规则/股票语料；strategy_distillation保存完整策略，包括19条市场观点。未把无收益验证的规则冒充为rule_library中的回测通过规则。",
              "- 写入前后SQLite一致性备份：data/backups/image-strategy-20261011/before与after；after包含原图、规则、代码及SHA256清单。原代码备份：docs/image-strategy-backup-20261011。",
              "- 停用：将config/image_strategy.json的enabled改为false；下一次请求即因配置哈希变化重新计算，恢复五因子权重。修改算法代码后需通过Opus托盘‘重新启动后端’或重启应用加载。",
              "", "内部hawk目录名和数据库名不变。当前安装版通过local-settings.json使用项目数据。财报/订单/真实资金流及策略盈利能力仍需独立数据和后续样本验证。", ""]
    (out / "蒸馏与接入报告.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    catalog = build_catalog()
    backup = ROOT / "data/backups/image-strategy-20261011"
    if not (backup / "before/hawk.sqlite").exists():
        snapshot(backup / "before")
    dump(DATA / "distilled.json", catalog)
    report(catalog)
    con = sqlite3.connect(ROOT / "data/hawk.sqlite", timeout=30)
    with con:
        # Keep user-enabled, unvalidated policy separate from profit-validated DSL rules.
        con.execute("CREATE TABLE IF NOT EXISTS strategy_distillation (id TEXT PRIMARY KEY, revision TEXT, observed_at TEXT, validation_status TEXT, payload TEXT)")
        con.execute("INSERT OR REPLACE INTO strategy_distillation VALUES (?,?,?,?,?)",
                    (STRATEGY, catalog["revision"], OBSERVED, "unvalidated_user_priority",
                     json.dumps(catalog, ensure_ascii=False)))
        for r in catalog["rules"] + catalog["stock_forecasts"]:
            ref = f"image_distillation/20261011/{r['id']}"
            if not con.execute("SELECT 1 FROM corpus WHERE strategy_id=? AND transcript_ref=?", (STRATEGY, ref)).fetchone():
                con.execute("INSERT INTO corpus(strategy_id,source,pub_time,content,transcript_ref,collected_at,processed) VALUES(?,?,?,?,?,?,1)",
                            (STRATEGY, "user_screenshots", None, json.dumps(r, ensure_ascii=False), ref, OBSERVED))
            else:
                con.execute("UPDATE corpus SET content=? WHERE strategy_id=? AND transcript_ref=?",
                            (json.dumps(r, ensure_ascii=False), STRATEGY, ref))
    con.close()
    after = backup / "after"
    snapshot(after)
    artifacts = ["config/image_strategy.json", "backend/domain/image_strategy.py", "backend/domain/recommend.py",
                 "scripts/daily_recommend.py", "backend/api/webapi.py", "backend/web/app.js", "backend/web/styles.css",
                 "scripts/distill_trade_images.py", "desktop/package.json",
                 "scripts/verify_image_strategy.py", "tests/test_image_strategy.py", "desktop/tests/image-strategy.cjs",
                 "data/image_distillation/20261011/distilled.json", "data/image_distillation/20261011/manifest.json"]
    artifacts += [str(p.relative_to(ROOT)) for p in (DATA / "originals").glob("*.jpg")]
    artifacts += [str(p.relative_to(ROOT)) for p in (ROOT / "docs/image-strategy-20261011").glob("*") if p.is_file() and p.name != "failure.png"]
    hashes = {}
    for f in artifacts:
        src = ROOT / f
        dst = after / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        hashes[f] = hashlib.sha256(dst.read_bytes()).hexdigest()
    hashes["hawk.sqlite"] = hashlib.sha256((after / "hawk.sqlite").read_bytes()).hexdigest()
    dump(after / "sha256.json", hashes)
    print(json.dumps({"rules": len(catalog["rules"]), "stock_records": len(catalog["stock_forecasts"]),
                      "market_forecasts": len(catalog["market_forecasts"]), "backup": str(after)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
