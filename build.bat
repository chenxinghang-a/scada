@echo off
cd /d "%~dp0"

echo ========================================
echo   SCADA System - Build EXE
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found
    pause
    exit /b 1
)

:: Install PyInstaller
echo [1/3] Checking PyInstaller...
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller -q
)

:: Build
echo [2/3] Building EXE...
python build.py

echo.
echo [3/3] Done!
echo.
pause
