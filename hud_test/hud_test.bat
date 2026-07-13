@echo off
chcp 65001 >nul 2>&1
title HUD Teszt

:: A projekt gyökeréből indítjuk (a .bat a hud_test\ mappában van)
cd /d "%~dp0.."

:: Check venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo [HIBA] Virtualis kornyezet nem talalhato!
    echo        Futtasd eloszor: setup_windows.bat
    pause
    exit /b 1
)

:: Activate venv and run
call .venv\Scripts\activate.bat
python hud_test\run_hud_test.py %*
if %errorlevel% neq 0 pause
