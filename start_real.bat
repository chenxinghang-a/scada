@echo off
cd /d "%~dp0"

echo ========================================
echo   Industrial SCADA System v2.1
echo   Real Device Mode
echo ========================================
echo.
echo   URL: http://localhost:5001
echo   Account: admin / admin123
echo   Press Ctrl+C to stop
echo.

python run.py --real
pause
