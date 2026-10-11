@echo off
title Slap Biometrics Studio - Local Workstation
color 0B

echo ======================================================================
echo           SLAP BIOMETRICS STUDIO - LOCAL WINDOWS LAUNCHER
echo ======================================================================
echo.

:: 1. Check Python
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: 2. Check / Create Virtual Environment
if not exist ".venv" (
    echo [*] Creating virtual environment (.venv)...
    python -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: 3. Activate Virtual Environment
call .venv\Scripts\activate.bat

:: 4. Install / Update Requirements
echo [*] Checking dependencies...
pip install -r requirements.txt --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Some dependencies failed to install. Continuing...
)

:: 5. Create .env if missing
if not exist ".env" (
    if exist ".env.example" (
        echo [*] Initializing .env configuration...
        copy .env.example .env >nul
    )
)

:: 6. Launch Studio
echo.
echo ======================================================================
echo    Studio is starting on: http://localhost:8001
echo    Opening browser...
echo ======================================================================
echo.

start http://localhost:8001
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001

pause
