@echo off
chcp 65001 >nul
title 工业SCADA系统 - 模拟模式

cd /d "%~dp0"

echo ========================================
echo   工业数据采集与监控系统 v2.1
echo   模拟模式（仿真数据）
echo ========================================
echo.
echo   访问地址: http://localhost:5000
echo   默认账户: admin / admin123
echo   按 Ctrl+C 停止系统
echo.

python run.py
pause
