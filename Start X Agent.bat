@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   X Agent - Signal CRM (API-first)
echo ========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    pause
    exit /b 1
)

if not exist "x_agent.db" (
    echo First run: database will be created by the app.
    echo Set your X credentials via 'Set X credentials' after it opens.
    echo.
)

echo.
echo Starting X Agent...
python x_gui.py
if errorlevel 1 (
    echo.
    echo The X Agent exited with an error.
    pause
)