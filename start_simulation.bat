@echo off
cd /d "%~dp0"
title SCADA 模拟模式

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo ============================================================
echo    工业数据采集与监控系统 —— 模拟模式
echo ============================================================
echo.
echo   地址: http://localhost:5000
echo   账号: admin / admin123
echo   配置: 配置\devices_simulated.yaml
echo   停止: 在本窗口按 Ctrl+C
echo.
echo   提示: 日常使用请直接双击 启动.bat（菜单式入口）
echo.
"%PY%" run.py
pause
