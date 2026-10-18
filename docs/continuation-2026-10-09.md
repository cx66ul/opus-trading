# 续开发交付说明

日期：2026-10-09。工作目录：`WorkBuddy/2026-10-08-19-58-20/hawk`。

## 范围与需求来源

用户要求读取指定文件夹，按开发提示词继续未完成工作。
指定文件夹没有该提示词；在相邻目录 `2026-10-04-20-53-08/A股辅助交易系统-开发提示词.md` 找到唯一匹配文件。
已核对该文件的 M5、M6、M7、M9~M11 和此目录最新工作日志。
保留原有决策记录，未调用券商接口，未执行邮件发送。

本轮推进最新日志中的板块强度、行情快照和推荐因子集成，未将全部项目标为完成。

## 接口与存储

- `strength.score_boards(rows, as_of)`：纯函数；八因子固定权重，行业/概念分别排名，平手用平均百分位。
- `tracking.capture_quotes(as_of, codes, dry_run)`：持仓优先、自选去重，上限 60；按缺失代码切腾讯/新浪；校验来源时间，分钟幂等落库。
- `market_context(conn, as_of)`、`board_context(conn, as_of)`：读取当时已可见快照，过期或缺失明确降级。
- `board_strength` 增量列：`pct_chg`、`amount`、`factor_coverage`、`score_method`、`factors_json`、`raw_json`、`source`、`source_complete`。
- `quote_snapshot` 增量列：`source`、`source_ts`、`captured_at`。
- 表仍保留原主键，迁移只新增列，旧记录的新增字段保持 NULL。

`partial_factor_v1` 按实际可得权重归一化，不能与完整八因子评分混同。
原始净流入缺少板块流通市值分母，未冒充 d5。
此前各日最后快照用于量比/排名斜率积累，不能用当天盘中多次采样代替多日数据。

## 修复与接入

1. 板块采集不再默认写入当天 15:00，不允许用当前接口补造历史快照。
2. 采集完整性独立保存；限流失败不再只存在于控制台日志。
3. 行情保存来源时间与捕获时间；过期、未来或非法报价拒绝入库。
4. 推荐的 K 线、消息、市场状态、板块快照均按 `as_of` 截断；08:30 不读取当天收盘。
5. 删除固定 `repair` 试跑状态，使用真实历史 `market_regime`。
6. 五日涨幅改为六个收盘价计算五段收益；RPS 改为 21 个收盘价计算 20 段收益，在全市场排名。
7. `--limit` 使用日期固定随机种子抽样，不再直接取代码排序前 N 只。
8. 注册 `realtime` 与 `board_scan` 任务；未启动常驻服务。
9. 新增独立 `.venv`、依赖清单、忽略规则、测试与只读验收脚本。

## 真实数据验证

- 原测试基线：769 passed。
- 新增 `tests/test_continuation.py`：14 项，覆盖因子缺失、平手、分类型排名、快照幂等、单股降级、来源时间、未来数据排除、消息时区、盘前截断和旧库迁移。
- 接口成功取得行业 90、概念 293；随后请求出现 403，最新批次仅取得行业，标记不完整。未声称全源长期稳定。
- 三只股票 `600519 / 000001 / 300750` 实时采集成功，写入 3 条分钟快照，未触发邮件。
- 60 只样本推荐试跑成功；真实状态读取自 2026-10-08 15:00，state=repair，score=43.15，cap=0.4。
- 只读验收：日线 1,375,427、分钟线 4,386,454、板块强度 856、行情快照 3、成分快照 20、市场状态 251。
- 最新行业因子权重覆盖为 28%，概念首批为 18%。该比例是数据覆盖，不是置信度或策略胜率。

运行方式见项目 `README.md`。最终回归结果追加在本文末尾。

## 剩余工作与限制

1. **板块数据**：全量成分映射不足，历史板块行情不可回填；先低频积累真实快照，再验证过滤增益。不得用当前成分反填历史。
2. **M10 语料蒸馏**：此目录未发现两位交易员的可审核语料，尚无蒸馏 CLI 和首批真实规则。需逐条保留原文出处与样本外报告。
3. **前端/API**：无 Electron/React/FastAPI 源码，K 线叠加、热力图、轮动图、业务面板与策略对比面板均未交付。
4. **常驻服务**：目前调度器为手工入口，缺 APScheduler 驱动、统一 DuckDB 写入所有者、SSE、托盘、自启和看门狗。
5. **M11**：打包、首次向导、自动更新、干净机器验收未完成。
6. **历史属性**：stock 主表市值/ST/停牌等仍为当前属性，尚未版本化；推荐结果明确记录此限制，不能称严格无前视的完整历史回测。
7. **阈值研究**：既有市场阈值由整段历史校准，不能据此宣称严格样本外优势；新增代码未重新优化或上线门控。
8. **日内刷新**：一分钟快照入口已实现，3 秒实时订阅与全市场 30~60 秒扫描尚未实现。

## 最终验收

- 全量测试：**783 passed in 15.67s**，无跳过。
- 本轮修改文件 Ruff 检查：**All checks passed**。
- 最终真实推荐试跑：60 只输入、55 只通过过滤，A=0、B=6、C=15。
- 生成结果：`data/recommend_continuation_2026-10-09.json`。
- 上述数字为实际运行结果，不是策略收益或实盘胜率承诺。

## 变更文件

- 新增：`backend/domain/strength.py`，`backend/services/__init__.py`、`tracking.py`、`market_context.py`。
- 新增：`scripts/capture_quotes.py`、`scripts/verify_continuation.py`，`tests/test_continuation.py`。
- 修改：`backend/data/duck.py`、`backend/data/adapters/ths_board.py`、`backend/scheduler.py`。
- 修改：`scripts/sync_board_strength.py`、`scripts/daily_recommend.py`、`config/fields/ths_board.yaml`。
- 新增：`requirements.txt`、`pyproject.toml`、`.gitignore`、`README.md`、本交付说明。
