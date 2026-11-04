@echo off
rem ============================================================
rem  SCADA 统一入口（round 369）
rem
rem  历史问题：这一行原来是 `call 启动.bat`，且文件按 GBK 保存。
rem  在启用了 UTF-8 代码页（chcp 65001 / Windows“UTF-8 全球语言支持”）
rem  的机器上，cmd.exe 会按 UTF-8 解析脚本字节，中文文件名变成乱码，
rem  直接报 “'.bat' 不是内部或外部命令”，双击入口毫无反应。
rem
rem  现在：入口脚本**全部用 ASCII**，直接内联启动逻辑
rem  （等价于 启动.bat 的核心动作），任何代码页下都能执行。
rem ============================================================
cd /d "%~dp0"

title SCADA Industrial Data Acquisition System

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo ============================================================
echo   Starting SCADA ...
echo ============================================================

"%PY%" run.py %*

if errorlevel 1 (
    echo.
    echo [ERROR] SCADA exited with an error. Check the logs above.
    pause
)
