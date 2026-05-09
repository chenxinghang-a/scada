@echo off
chcp 65001 >nul
title 工业数据采集与监控系统

:: 设置Python路径（WorkBuddy内置Python）
set "PYTHON=C:\Users\cxx\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "PIP=C:\Users\cxx\.workbuddy\binaries\python\envs\default\Scripts\pip.exe"

:: 检查Python是否存在
if not exist "%PYTHON%" (
    echo [错误] 未找到Python: %PYTHON%
    pause
    exit /b 1
)

echo ========================================
echo   工业数据采集与监控系统 v2.1
echo   Industrial SCADA System
echo ========================================
echo.

:: 检查依赖
echo [1/3] 检查依赖包...
"%PIP%" install -q -r requirements.txt 2>nul

:: 创建必要目录
echo [2/3] 初始化目录...
if not exist "data" mkdir data
if not exist "logs" mkdir logs
if not exist "exports" mkdir exports

:: 启动系统
echo [3/3] 启动系统...
echo.
echo ========================================
echo   系统启动中...
echo   访问地址: http://localhost:5000
echo   默认账户: admin / admin123
echo   按 Ctrl+C 停止系统
echo ========================================
echo.

"%PYTHON%" run.py

pause
