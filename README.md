# Opus 辅助交易

> A股个人辅助交易决策系统。**决策副驾，不接券商下单接口。**

行情采集 → 数据入库 → 因子/策略计算 → 信号输出 → 邮件报告 → 前端展示。

---

## ⚠️ 使用前必读

本仓库**只包含代码**，不含数据与密钥：

| 内容 | 为什么没有 |
|---|---|
| `data/*.duckdb` | 138 万行行情库，体积大且属个人数据 |
| `data/secrets.vault.json` | **含 SMTP 授权码（DPAPI 加密）** |
| `runtime/` | 打包用 Python 运行时，可由脚本重建 |
| `data/creator_corpus/` | 抖音/视频号语料的**原始音视频**（他人版权素材） |

首次运行需自行生成：

```bash
python scripts/build_runtime.py            # 生成 Python 运行时（打包用）
python scripts/sync_board_strength.py      # 采集板块行情（历史不可补造）
python scripts/capture_quotes.py           # 交易时段采集持仓与自选
```

---

## 三条铁律

改动前务必理解，它们不是风格偏好，违反了会**静默出错**：

| # | 铁律 | 违反后果 |
|---|---|---|
| 1 | **接口 + 注册表**，换实现不改核心代码 | 一改就到处是补丁 |
| 2 | **纯函数引擎** `f(输入, as_of, params)`，**禁 `datetime.now()`/`time.time()`** | 回测不可复现，且不报错 |
| 3 | **抓取与解析分离**，原始报文先落 `raw_news` | 上游改格式时静默产出空数据 |

**配色约定**：涨=红、跌=绿（**仅行情**）。服务状态用绿/黄，两套不可混。

**内部标识符**：`hawk.duckdb` / `hawk.sqlite` / 目录 `hawk/` —— 这些是**数据身份**，改名等于丢数据且不报错。品牌名是 Opus，代码标识符仍是 hawk。

---

## 快速开始

```bash
# 1. 环境
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 2. 初始化数据库
.venv/Scripts/python.exe -m backend.data.duck

# 3. 跑测试
.venv/Scripts/python.exe -m pytest tests -q

# 4. 启动 Web 端
PYTHONPATH=. .venv/Scripts/python.exe -m uvicorn backend.api.webapi:app --port 8900
```

浏览器打开 `http://127.0.0.1:8900`。

---

## 架构

```
backend/
├── domain/       纯函数引擎（铁律二）—— 不碰 IO，不取系统时间
│   ├── breadth.py      涨跌停 / 亏钱效应面积
│   ├── regime.py       市场状态机（带迟滞，避免每日翻转）
│   ├── strategy.py     StrategyContext
│   ├── dsl.py          规则 DSL（24 个谓词）
│   └── board_factors.py 板块八因子
├── data/         采集与存储
│   ├── duck.py         DuckDB（单写者：写 get_conn / 读 get_ro_conn）
│   └── adapters/       tencent / sina / eastmoney / ths_board
├── services/     两江算力平台适配层（只读）
├── scheduler.py  11 个调度任务 + cron 判定
└── web/          主界面（6 页签，零构建链）

desktop/          Electron 桌面端
scripts/          采集 / 同步 / 打包 / 备份脚本
tests/            1255 个测试
```

---

## 调度任务

```bash
python -m backend.scheduler --list           # 列出全部
python -m backend.scheduler --run news_247   # 手工执行
python -m backend.scheduler --catch-up       # 补跑错过的

python scripts/soak_runner.py --hours 26             # 长跑
python scripts/soak_runner.py --report --hours 24    # 长跑报告
```

长跑报告里的 **`max_gap_sec`** 才是「有没有静默中断」的直接证据
（轮询 20 秒，> 90 秒即说明进程被卡住或退出过）。

⚠ DuckDB **不支持多进程并发写**：行情写入与板块写入必须串行。

---

## 桌面端

```bash
cd desktop
npm install
unset ELECTRON_RUN_AS_NODE          # ★ 必须，否则 Electron 退化成纯 Node
npm start

# 打包
python ../scripts/build_runtime.py
npm run dist
```

⚠ `ELECTRON_RUN_AS_NODE=1` 会让 Electron 当成普通 Node 运行，报
`Cannot read properties of undefined (reading 'isPackaged')` ——
错误信息完全指错方向。

---

## 数据口径

已确认的口径见 `data/scope_answer_sheet.md`。几个容易搞错的：

| 项 | 口径 |
|---|---|
| 大盘 | **上证指数**（不是全市场等权均值 —— 实测两者符号经常相反） |
| 市值 | 流通市值，单位是**元**（用时 `/1e8`） |
| `pct_chg`（库内） | **百分数**，API 层 `_pct()` `/100` 归一 |
| 成交额 | ⚠ `kline_daily.amount` **已废弃不可用**，一律 `volume × close` 自己算 |
| 亏钱效应面积 | `跌停/(涨停+跌停)`（分母若用全市场家数，该规则 242 天永不触发） |
| 腾讯成交量单位 | `688`/`689` 返回**股**，其余返回**手** |

**引用行情前必须过 `data_gate`** —— 存在残缺交易日（如 2026-10-09 仅 108 只），
直接用会得到错误的横截面统计。

---

## 训练后备份（死纪律）

每次训练完成**必须**备份，备份失败则整单判失败：

```bash
python scripts/backup_after_train.py --date 20261011
python scripts/backup_after_train.py --verify 20261011
```

备份对象必须与训练对象**是同一份数据**（取 `build_trainset()` 的输出，
而不是按文件名猜 —— 累积样本从不落盘）。

---

## 测试

```bash
.venv/Scripts/python.exe -m pytest -q           # 1255 个
cd desktop && node tests/shell.test.js
cd desktop && node tests/updater.test.js
```

**写完判据必须做变异测试**：故意改坏源码，确认测试变红，再还原。

判据本身会骗你 —— 已踩过的坑写在各测试文件的 docstring 里：
注释里的关键词会污染子串判据、`if False:` 能伪装成 return、
Python 的 `ast` 解析不了 JS、Git 的否定规则无法穿透被排除的父目录。

---

## 许可

仅供个人研究使用。仓库含第三方数据源适配器，**不包含**任何行情数据。