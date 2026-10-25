@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_release.ps1" %*
if errorlevel 1 pause
exit /b %errorlevel%
