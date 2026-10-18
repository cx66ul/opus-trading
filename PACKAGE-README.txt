Hawk Windows x64 便携交付包 - 2026-10-09

使用方式
1. 将整个 ZIP 解压到可写的本地文件夹。
2. 双击 Start-Hawk.cmd 打开菜单。
3. 选择“查看数据状态”，即可检查随包数据。

无需安装 Python，runtime 文件夹内已有 Python 3.12 与依赖。
也可在包内终端运行：
  runtime\python.exe -E -s scripts\verify_continuation.py
  runtime\python.exe -E -s scripts\daily_recommend.py --date 2026-10-09 --limit 60
  runtime\python.exe -E -s -m pytest tests -q

当前内容
- 源码、配置、测试、交付说明。
- 本地行情数据库、业务数据库、历史 Parquet 快照和研究结果。
- 独立 Python 运行时与依赖。
- 每个文件的 SHA-256 清单 manifest.json。

这是现有数据与研究引擎的便携包，尚无 Electron 桌面界面、安装向导或常驻服务。
日线、板块和市场状态有各自的更新时间，请先查看数据状态。
实时采集需要联网，免费源可能限流；行情写入操作须串行进行。
ZIP 必须完整解压后运行，不能直接在压缩文件预览中启动。

邮箱授权码与加密密钥库未包含；邮件功能需另行配置。
4.7 GB 的 qwen25-7b-q4km.gguf、本地推理工具和 Ollama 未包含。
在原电脑上需要本地模型时，继续使用原项目的模型与已配置推理服务。
不带模型不影响行情、板块、缠论、推荐、回测和数据核验功能。

详细功能状态：docs\continuation-2026-10-09.md
开发说明：README.md
