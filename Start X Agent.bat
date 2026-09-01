@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   X Agent - Signal CRM (browser-based)
echo ========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    pause
    exit /b 1
)

python -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo Installing required Python packages: playwright, requests...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install required packages.
        pause
        exit /b 1
    )
)

echo.
echo The agent Chrome (its own profile) opens automatically on
echo port 9223. Log in to X in it once when it appears.
echo.
echo Starting X Agent...
python x_gui.py
if errorlevel 1 (
    echo.
    echo The X Agent exited with an error.
    pause
)