@echo off
chcp 65001 >nul
call "%~dp0appctl.bat" stop %*
exit /b %errorlevel%
