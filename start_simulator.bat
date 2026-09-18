@echo off
cd /d "%~dp0"
title SCADA + Modbus 模拟器

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo ============================================================
echo    Modbus TCP 模拟器 + SCADA（真实协议）
echo ============================================================
echo.
echo   [1/2] 启动 Modbus 模拟器（127.0.0.1:5020）...
start /b "" "%PY%" tools\modbus_simulator.py --port 5020
timeout /t 3 >nul

echo   [2/2] 启动 SCADA 系统...
echo.
echo   地址: http://localhost:5001
echo   账号: admin / admin123
echo   配置: 配置\devices_modbus_sim.yaml
echo   停止: 在本窗口按 Ctrl+C（Modbus 模拟器需另行结束）
echo.
echo   说明: --simulator 模式走真实 Modbus TCP 协议连本机 5020 端口，
echo         与「模拟模式」的纯内存仿真数据不是一回事。
echo.
"%PY%" run.py --simulator
pause
