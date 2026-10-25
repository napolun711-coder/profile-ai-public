# 注册每日 M6 备份任务（默认 03:17，避开整点）。
param(
    [string]$Time = "03:17",
    [string]$TaskName = "profile-ai-M6-backup"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$wrapper = Join-Path $root "scripts\m6_backup_daily.ps1"
if (-not (Test-Path $wrapper)) { throw "找不到备份脚本：$wrapper" }
$action = 'powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "' + $wrapper + '"'
schtasks.exe /Create /TN $TaskName /SC DAILY /ST $Time /TR $action /F
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
schtasks.exe /Query /TN $TaskName /V /FO LIST
exit $LASTEXITCODE
