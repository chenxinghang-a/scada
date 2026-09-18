@echo off
cd /d "%~dp0"
title SCADA 真实设备模式

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY set "PY=python"

echo ============================================================
echo    工业数据采集与监控系统 —— 真实设备模式
echo ============================================================
echo.
echo   地址: http://localhost:5001
echo   账号: admin / admin123
echo   配置: 配置\devices_real.yaml
echo   停止: 在本窗口按 Ctrl+C
echo.
echo   注意: 配置\devices_real.yaml 目前是空壳（13 字节），
echo         接真设备前需先按实际从站填写 host / port / slave_id
echo         和寄存器表，否则起来后一个设备都没有。
echo.
"%PY%" run.py --real
pause
