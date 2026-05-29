@echo off
cd /d "%~dp0"

echo ========================================
echo   Modbus Simulator + SCADA System
echo   Real Modbus TCP Protocol
echo ========================================
echo.

echo [1/2] Starting Modbus Simulator (port 5020)...
start /b python tools\modbus_simulator.py --port 5020
timeout /t 2 >nul

echo [2/2] Starting SCADA System...
echo   URL: http://localhost:5000
echo   Account: admin / admin123
echo.
python run.py --simulator
pause
