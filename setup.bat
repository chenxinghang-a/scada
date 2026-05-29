@echo off
cd /d "%~dp0"

echo ========================================
echo   Industrial SCADA System - Setup
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo [OK] Python %PYVER%

echo.
echo [1/3] Installing dependencies...
pip install -r requirements.txt -q
echo [OK] Dependencies installed

echo.
echo [2/3] Creating directories...
if not exist "data" mkdir data
if not exist "logs" mkdir logs
if not exist "exports" mkdir exports
echo [OK] Directories ready

echo.
echo [3/3] Initializing config...
if not exist ".env" (
    copy .env.example .env >nul
    echo [OK] Created .env from .env.example
) else (
    echo [OK] .env already exists
)

echo.
echo ========================================
echo   Setup complete!
echo.
echo   Start: double-click start.bat
echo   Docker: docker compose up
echo ========================================
pause
