@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "runtime\python.exe" (
  echo Python runtime missing. Extract the complete package first.
  pause
  exit /b 1
)
:menu
cls
echo Hawk - 数据与研究引擎
echo.
echo 1. 查看数据状态
echo 2. 生成今日盘前研究报告
echo 3. 采集当前板块数据（联网）
echo 4. 采集持仓与自选行情（联网，交易时段）
echo 5. 运行验收测试
echo 6. 打开使用说明
echo 0. 退出
echo.
set "hawk_choice="
set /p "hawk_choice=请选择: "
if "%hawk_choice%"=="0" exit /b 0
if "%hawk_choice%"=="1" "runtime\python.exe" -E -s scripts\verify_continuation.py
if "%hawk_choice%"=="2" "runtime\python.exe" -E -s scripts\daily_recommend.py
if "%hawk_choice%"=="3" "runtime\python.exe" -E -s scripts\sync_board_strength.py
if "%hawk_choice%"=="4" "runtime\python.exe" -E -s scripts\capture_quotes.py
if "%hawk_choice%"=="5" "runtime\python.exe" -E -s -m pytest tests -q
if "%hawk_choice%"=="6" start "" notepad.exe PACKAGE-README.txt
echo.
pause
goto menu
