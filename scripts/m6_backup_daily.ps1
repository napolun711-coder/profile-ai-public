# M6 每日备份包装：供 Windows 任务计划程序调用。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $root "logs"
$log = Join-Path $logDir "m6_backup.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Encoding utf8 -Value ("[" + $stamp + "] 开始 M6 备份")
$python = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "scripts\m6_backup.py"
$output = & $python -X utf8 $script 2>&1
$code = $LASTEXITCODE
$output | Out-File -FilePath $log -Append -Encoding utf8
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Encoding utf8 -Value ("[" + $stamp + "] 结束，exit=" + $code)
exit $code
