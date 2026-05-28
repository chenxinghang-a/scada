@echo off
chcp 65001 >nul
title 工业SCADA系统 - 模拟模式

cd /d "%~dp0"

echo ========================================
echo   工业数据采集与监控系统 v2.1
echo   模拟模式（仿真数据）
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

:: 检查依赖
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖...
    pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
)

:: 创建目录
if not exist "data" mkdir data
if not exist "logs" mkdir logs

:: 打开浏览器
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5000"

echo   访问地址: http://localhost:5000
echo   默认账号: admin / admin123
echo   按 Ctrl+C 停止
echo.

python run.py
pause
