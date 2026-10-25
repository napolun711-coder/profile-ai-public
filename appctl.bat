@echo off
rem ============================================================
rem  Profile AI Assistant - single entry point (VISIBLE console)
rem  daily use : double-click appctl.vbs   (no window at all)
rem  this file : use it when you want to READ what is going on
rem  usage     : appctl.bat [start^|stop^|restart^|status^|open^|
rem              backup^|migrate^|switch-vdb^|shortcut]
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [ERROR] Python environment not found: .venv\Scripts\python.exe
    echo         Run install_env.bat first.
    echo.
    pause
    exit /b 3
)

".venv\Scripts\python.exe" "scripts\appctl.py" %*
set "RC=%errorlevel%"

set "CMD=%~1"
if "%CMD%"=="" set "CMD=start"

echo.
if "%RC%"=="0" if /i not "%CMD%"=="start" pause
if not "%RC%"=="0" pause

exit /b %RC%
