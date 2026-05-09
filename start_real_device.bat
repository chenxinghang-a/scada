@echo off
chcp 65001 >nul
title 工业数据采集与监控系统 - 真实设备模式
color 0B

echo.
echo   ========================================
echo     工业数据采集与监控系统 SCADA v2.1
echo     *** 真实设备模式 ***
echo   ========================================
echo.

cd /d "%~dp0"

:: 优先使用 WorkBuddy 内置 Python
set "PYTHON=C:\Users\cxx\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

:: 如果 WorkBuddy Python 不存在，尝试系统 Python
if not exist "%PYTHON%" (
    set "PYTHON=C:\Users\cxx\AppData\Local\Programs\Python\Python312\python.exe"
)

:: 如果都不存在，提示用户安装
if not exist "%PYTHON%" (
    echo [X] 未找到 Python！
    echo     请安装 Python 3.8+ : https://www.python.org/downloads/
    echo     或确认 WorkBuddy 已正确安装。
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
        echo [X] 依赖安装失败，请检查网络连接
        pause
        exit /b 1
    )
    echo [OK] 依赖安装完成
) else (
    echo [OK] 依赖已就绪
)

:: 创建必要目录
if not exist "logs" mkdir logs
if not exist "data" mkdir data

:: 延迟3秒后打开浏览器
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5000"

echo.
echo [2/3] 启动系统 (真实设备模式)...
echo.
echo   访问地址: http://localhost:5000
echo   默认账号: admin / admin123
echo   设备配置: 配置/devices_real.yaml
echo   数据库  : data/scada_real.db
echo   按 Ctrl+C 停止
echo.
echo [3/3] 服务启动中...
echo.

"%PYTHON%" run.py --real

pause
