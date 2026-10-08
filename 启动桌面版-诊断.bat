@echo off
cd /d "%~dp0"
title SCADA 桌面版 - 启动 / 诊断
setlocal

set "APP=%LOCALAPPDATA%\Programs\SmartSCADA\SmartSCADA.exe"
if not exist "%APP%" set "APP=%USERPROFILE%\scada-app\release\win-unpacked\SmartSCADA.exe"

echo ============================================================
echo    SCADA 桌面版 - 启动 / 诊断
echo ============================================================
echo.
echo   程序: %APP%
if not exist "%APP%" (
    echo.
    echo   [错误] 找不到桌面客户端
    echo.
    pause
    exit /b 1
)
echo.

rem 清掉可能干扰 Electron 的环境变量（本来没有也无害）
set "ELECTRON_RUN_AS_NODE="
set "NODE_OPTIONS="

echo   [第 1 次] 正常启动...
start "" "%APP%"
ping -n 16 127.0.0.1 >nul

tasklist /FI "IMAGENAME eq SmartSCADA.exe" | findstr /I "SmartSCADA.exe" >nul
if not errorlevel 1 (
    echo   [OK] 已启动，程序正在运行。
    goto REPORT
)

echo.
echo   [第 1 次] 失败：进程没起来。
echo          常见原因是 GPU 进程起不来 —— Electron 会直接 FATAL 退出，
echo          表现就是「双击完全没反应」。
echo.
echo   [第 2 次] 改用**软件渲染**重试（SCADA_DISABLE_GPU=1）...
set "SCADA_DISABLE_GPU=1"
start "" "%APP%"
ping -n 16 127.0.0.1 >nul

tasklist /FI "IMAGENAME eq SmartSCADA.exe" | findstr /I "SmartSCADA.exe" >nul
if not errorlevel 1 (
    echo.
    echo   [OK] 软件渲染下起来了！
    echo        ^>^> 把这个结论告诉助手：**是 GPU 问题**，需要固定用软渲染。
    goto REPORT
)

echo.
echo   [第 2 次] 也失败。

:REPORT
echo.
echo ============================================================
echo    检查结果
echo ============================================================
echo.
tasklist /FI "IMAGENAME eq SmartSCADA.exe" | findstr /I "SmartSCADA.exe" >nul
if errorlevel 1 (
    echo   [X] SmartSCADA.exe 没在运行
) else (
    echo   [OK] SmartSCADA.exe 正在运行
)
tasklist /FI "IMAGENAME eq scada-backend.exe" | findstr /I "scada-backend.exe" >nul
if errorlevel 1 (
    echo   [X] 内置后端没在运行
) else (
    echo   [OK] 内置后端正在运行
)
netstat -ano | findstr /C:":5000" >nul
if errorlevel 1 (
    echo   [X] 端口 5000 没在监听
) else (
    echo   [OK] 端口 5000 正在监听
)
echo.
echo   ------------- 应用日志（最后几行）-------------
if exist "%APPDATA%\SmartSCADA\update.log" (
    echo   [有 update.log]
) else (
    echo   [没有 update.log —— 说明程序没跑到初始化那步]
)
echo   -----------------------------------------------
echo.
echo   把这一屏内容发给我就能定位。
echo.
pause
