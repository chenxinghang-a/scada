@echo off
cd /d "%~dp0"
title SCADA 工业数据采集与监控系统
setlocal

rem ============ 读取版本号 ============
set "VER=unknown"
for /f "delims=" %%v in (VERSION) do set "VER=%%v"

rem ============ 定位 Python 解释器 ============
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    set "PY=python"
    echo [提示] 未找到 .venv，改用系统 Python
    echo        建议先运行 setup.bat 创建虚拟环境
    echo.
)

:MENU
cls
echo ============================================================
echo    工业数据采集与监控系统            v%VER%
echo ============================================================
echo.
echo    [1] 模拟模式启动       推荐，无需硬件，用仿真数据
echo    [2] 真实设备模式启动   接 Modbus TCP/RTU 真设备
echo    [3] 桌面客户端         先起后端，再开 Electron 界面
echo    [4] 环境自检           只检查，不启动
echo    [0] 退出
echo.
set "CH="
set /p "CH=请输入序号后回车: "
if "%CH%"=="1" goto SIM
if "%CH%"=="2" goto REAL
if "%CH%"=="3" goto DESKTOP
if "%CH%"=="4" goto CHECK
if "%CH%"=="0" goto QUIT
echo.
echo [错误] 无效输入
timeout /t 2 >nul
goto MENU


rem ==================== 模拟模式 ====================
:SIM
cls
call :BANNER
call :PORTWARN 5000
echo   模式: 模拟（仿真数据）
echo   地址: http://localhost:5000
echo   账号: admin / admin123
echo   停止: 在本窗口按 Ctrl+C
echo.
echo   后端启动约需 10-20 秒，就绪后自动打开浏览器...
echo.
start /b "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 14; Start-Process 'http://localhost:5000/login'"
"%PY%" run.py
goto DONE


rem ==================== 真实设备模式 ====================
:REAL
cls
call :BANNER
call :PORTWARN 5001
echo   模式: 真实设备（配置见 config\devices_real.yaml）
echo   地址: http://localhost:5001
echo   账号: admin / admin123
echo   停止: 在本窗口按 Ctrl+C
echo.
start /b "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 14; Start-Process 'http://localhost:5001/login'"
"%PY%" run.py --real
goto DONE


rem ==================== 桌面客户端 ====================
:DESKTOP
cls
call :BANNER
set "APP=%USERPROFILE%\scada-app\release\win-unpacked\SmartSCADA.exe"
if not exist "%APP%" (
    echo   [错误] 未找到桌面客户端：
    echo          %APP%
    echo.
    echo   请先到 scada-app 目录执行：npm run electron:build
    echo.
    pause
    goto MENU
)
call :PORTWARN 5000
echo   客户端: %APP%
echo   后端:   http://localhost:5000
echo   停止:   在本窗口按 Ctrl+C
echo.
echo   先起后端，14 秒后自动打开桌面客户端...
echo.
start /b "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 14; Start-Process '%APP%'"
"%PY%" run.py
goto DONE


rem ==================== 环境自检 ====================
:CHECK
cls
call :BANNER
echo [1/5] Python 解释器
echo        %PY%
"%PY%" -V
echo.
echo [2/5] 核心依赖
"%PY%" -c "import flask, flask_socketio, pymodbus, bcrypt, jwt, apscheduler; print('       核心依赖 OK')" 2>&1
echo.
echo [3/5] 项目配置
"%PY%" -c "import config; print('       config OK    模拟端口', config.WebConfig.PORT, ' 真实端口', config.WebConfig.REAL_PORT)" 2>&1
echo.
echo [4/5] 数据库文件
if exist "data\scada.db" (
    for %%A in ("data\scada.db") do echo        data\scada.db   %%~zA 字节   修改于 %%~tA
) else (
    echo        [警告] 未找到 data\scada.db（首次启动会自动创建）
)
echo.
echo [5/5] 端口占用
call :PORTWARN 5000
call :PORTWARN 5001
echo.
pause
goto MENU


rem ==================== 子程序 ====================
:BANNER
echo ============================================================
echo    工业数据采集与监控系统            v%VER%
echo ============================================================
echo.
exit /b 0

:PORTWARN
netstat -ano | findstr ":%~1 " | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
    echo   [警告] 端口 %~1 已被占用 —— 可能已有实例在运行
    echo          若界面打不开，请先结束占用该端口的进程
    echo.
)
exit /b 0

:DONE
echo.
echo ============================================================
echo   服务已退出
echo ============================================================
echo.
pause
goto MENU

:QUIT
endlocal
exit /b 0
