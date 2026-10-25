@echo off
rem ============================================================
rem  Profile AI Assistant - 放行局域网访问（平板 / 手机打不开网页时用）
rem  会弹出 UAC；确认后以管理员身份运行 scripts\allow_lan.ps1。
rem  只新增/更新一条 TCP 8100 的入站防火墙规则，不改 .env、不启停服务。
rem ============================================================
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath powershell.exe -Verb RunAs -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0scripts\allow_lan.ps1')"
exit /b %errorlevel%
