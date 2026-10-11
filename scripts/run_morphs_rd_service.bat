@echo off
title Mantra MORPHS RD Service Bridge
color 0A
echo ======================================================================
echo    Starting Mantra MORPHS RD Service on http://127.0.0.1:11100
echo ======================================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python "%~dp0morphs_rd_service.py"
    goto end
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py "%~dp0morphs_rd_service.py"
    goto end
)

echo [ERROR] Python was not found in your PATH.
echo Please install Python 3 or add Python to your Windows PATH.
echo You can also run VDM directly and use the 'Import Slap' button on the website.
pause

:end
