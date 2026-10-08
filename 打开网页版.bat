@echo off

cd /d "%~dp0"

title SCADA - 启动网页版

echo ============================================================

echo    工业数据采集与监控系统 - 网页版

echo ============================================================

echo.

echo   地址: http://localhost:5000

echo   账号: admin / admin123

echo.

echo   后端会在【另一个窗口】里运行（标题：SCADA 后端）

echo   要停止后端，关掉那个窗口即可。

echo.

echo   正在启动后端，就绪后会自动打开浏览器（约 10-20 秒）...

echo.



set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" set "PY=python"



start "SCADA 后端" "%PY%" run.py



set /a N=0

:waitloop

ping -n 3 127.0.0.1 >nul

set /a N+=1

netstat -ano | findstr /C:":5000" >nul

if errorlevel 1 (

    if %N% LSS 20 goto waitloop

    echo.

    echo [警告] 探测 20 次仍未看到 5000 端口。

    echo        请把【SCADA 后端】那个窗口里的内容发给我。

    echo.

    pause

    exit /b 1

)



echo.

echo   [OK] 后端已就绪，正在打开浏览器...

start "" "http://localhost:5000/login"

echo.

echo   ==============================================

echo    已启动。浏览器里用 admin / admin123 登录。

echo    停止后端：关掉【SCADA 后端】那个窗口。

echo   ==============================================

echo.

pause

