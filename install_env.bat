@echo off
chcp 65001 >nul
call "%~dp0setup.cmd" %*
exit /b %errorlevel%
