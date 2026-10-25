@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\check_env.ps1"
pause
exit /b %errorlevel%
