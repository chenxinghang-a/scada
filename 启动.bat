@echo off
chcp 65001 >nul
title 工业数据采集与监控系统
color 0A

echo.
echo   ========================================
echo     工业数据采集与监控系统 SCADA v1.0
echo   ========================================
echo.

cd /d "%~dp0"

set "PYTHON=C:\Users\cxx\AppData\Local\Programs\Python\Python312\python.exe"

if not exist "%PYTHON%" (
    set "PYTHON=C:\Users\cxx\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
)

if not exist "%PYTHON%" (
    echo [X] 未找到Python!
    echo     请安装 Python 3.8+ : https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [OK] Python: %PYTHON%

:: 检查依赖
echo [1/3] 检查依赖...
"%PYTHON%" -c "import flask" >nul 2>&1
if %errorlevel% neq 0 (
    echo [安装] 首次运行，安装依赖...
    "%PYTHON%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
    if %errorlevel% neq 0 (
        echo [X] 依赖安装失败
        pause
        exit /b 1
    )
    echo [OK] 依赖安装完成
) else (
    echo [OK] 依赖已就绪
)

:: 创建日志目录
if not exist "logs" mkdir logs

:: 延迟3秒后打开浏览器
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5000"

echo.
echo [2/3] 启动系统 (模拟模式)...
echo.
echo   访问地址: http://localhost:5000
echo   默认账号: admin / admin123
echo   按 Ctrl+C 停止
echo.
echo [3/3] 服务启动中...
echo.

"%PYTHON%" run.py

pause
