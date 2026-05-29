@echo off
cd /d "%~dp0"

echo ========================================
echo   Industrial SCADA System v2.1
echo   Simulation Mode
echo ========================================
echo.
echo   URL: http://localhost:5000
echo   Account: admin / admin123
echo   Press Ctrl+C to stop
echo.

python run.py
pause
