@echo off
chcp 65001 >nul
echo ========================================
echo 工业数据采集与监控系统 - 真实设备模式
echo ========================================
echo.
echo 启动中...
echo.

cd /d "%~dp0"
python run.py --real

pause
