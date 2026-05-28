@echo off
chcp 65001 >nul
title 工业SCADA系统 - 环境配置

echo ========================================
echo   工业数据采集与监控系统 - 环境配置
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo [OK] Python %PYVER%

:: 安装依赖
echo.
echo [1/3] 安装依赖...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [警告] 部分依赖安装失败，尝试继续...
)
echo [OK] 依赖安装完成

:: 创建目录
echo.
echo [2/3] 创建运行目录...
if not exist "data" mkdir data
if not exist "logs" mkdir logs
if not exist "exports" mkdir exports
echo [OK] 目录就绪

:: 创建 .env
echo.
echo [3/3] 初始化配置...
if not exist ".env" (
    copy .env.example .env >nul
    echo [OK] 已从 .env.example 创建 .env
) else (
    echo [OK] .env 已存在，跳过
)

echo.
echo ========================================
echo   配置完成！
echo.
echo   启动方式:
echo     模拟模式:  双击 start.bat
echo     真实模式:  双击 start_real.bat
echo     Docker:    docker compose up
echo ========================================
pause
