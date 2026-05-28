@echo off
chcp 65001 >nul
title 工业SCADA系统 - Modbus模拟器模式
echo ========================================
echo   启动 Modbus 模拟器 + SCADA 系统
echo   采集层走真实 Modbus TCP 协议
echo ========================================
echo.

:: 启动模拟器（后台）
echo [1/2] 启动 Modbus TCP 模拟器 (端口 5020)...
start /b python tools\modbus_simulator.py --port 5020
timeout /t 2 >nul

:: 启动 SCADA
echo [2/2] 启动 SCADA 系统...
echo 访问地址: http://localhost:5000
echo 账号: admin  密码: admin123
echo.
python run.py --simulator
pause
