@echo off
chcp 65001 >nul
echo ========================================
echo 工业数据采集与监控系统 - 模拟模式
echo ========================================
echo.
echo 启动中（使用仿真数据）...
echo.

cd /d "%~dp0"
python run.py

pause
