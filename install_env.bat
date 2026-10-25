@echo off
rem ============================================================
rem  Profile AI Assistant - install / prepare environment
rem  Run this ONCE on a new machine (checks uv + Ollama,
rem  creates folders, installs dependencies, self-checks).
rem  Daily use since Phase 10: double-click the desktop shortcut,
rem  or appctl.vbs / appctl.bat in this folder.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\prepare_env.ps1"
echo.
echo [shortcut] creating desktop / start menu shortcuts ...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\install_shortcuts.ps1"
echo.
pause
