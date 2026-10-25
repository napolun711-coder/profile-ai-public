#Requires -Version 5.1
param([switch]$Silent)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$profileHome = if ($env:PROFILE_AI_HOME) { $env:PROFILE_AI_HOME } else { Join-Path $env:LOCALAPPDATA 'ProfileAI' }
$logDir = Join-Path $profileHome 'logs'
$backupRoot = Join-Path $profileHome 'backups'
$logFile = Join-Path $logDir 'backup.log'
$status = 0
$failed = @()

function Say([string]$text, [string]$color = 'Gray') {
    if (-not $Silent) { Write-Host $text -ForegroundColor $color; return }
    Add-Content -LiteralPath $logFile -Value $text -Encoding UTF8
}
function ShowBox([string]$text, [uint32]$icon = 0x40) {
    if (-not $Silent) { return }
    try { (New-Object -ComObject WScript.Shell).Popup($text, 0, '本地知识库助手 - 备份', $icon) | Out-Null } catch { }
}
foreach ($dir in @($logDir, $backupRoot)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
if ($Silent) { Say ''; Say ('===== ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' backup =====') }
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$dst = Join-Path $backupRoot $stamp
Say ''; Say '===== 本地知识库助手 - 备份 ====='
$total = 0
foreach ($name in @('knowledge', 'rag_storage', 'config', 'data')) {
    $src = Join-Path $profileHome $name
    if (Test-Path -LiteralPath $src) {
        try {
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
            Copy-Item -LiteralPath $src -Destination (Join-Path $dst $name) -Recurse -Force -ErrorAction Stop
            $files = Get-ChildItem (Join-Path $dst $name) -Recurse -File -ErrorAction SilentlyContinue
            $mb = [math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1MB, 2)
            $total += $mb
            Say ('  [OK] ' + $name + '  ' + $mb + ' MB') 'Green'
        } catch {
            $failed += $name
            Say ('  [失败] ' + $name + '  ' + $_.Exception.Message) 'Red'
        }
    } else { Say ('  [--] ' + $name + ' 不存在，已跳过') 'DarkGray' }
}
Say ''
if ($failed.Count -gt 0) {
    $status = 3
    Say ('备份未完成，失败项：' + ($failed -join ' / ')) 'Red'
    Say ('已保留部分结果：' + $dst) 'Yellow'
} else {
    Say ('备份完成：' + $dst + '  合计约 ' + $total + ' MB') 'Green'
    Say ('详细日志：' + $logFile) 'Gray'
    ShowBox ("备份完成。`r`n`r`n备份目录：$dst`r`n合计约 $total MB")
}
exit $status
