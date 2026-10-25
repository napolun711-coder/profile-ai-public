# 备份知识库 / 索引 / 配置 / 用户数据 到 backup\日期时间
# 用法：
#   backup.bat          前台运行，能看到每一步（维护用）
#   backup_silent.vbs   静默运行：全程无窗口，结束后弹结果框，日志 logs\backup.log
param([switch]$Silent)

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$logFile = Join-Path $root 'logs\backup.log'
$status = 0
$failed = @()

function Say([string]$text, [string]$color = 'Gray') {
    # 前台：彩色打印；静默：逐行追加到 logs\backup.log
    if (-not $Silent) { Write-Host $text -ForegroundColor $color; return }
    Add-Content -LiteralPath $logFile -Value $text -Encoding UTF8
}

function ShowBox([string]$text, [uint32]$icon = 0x40) {
    # 静默模式结束时弹结果框：用户看不到窗口，必须给回执（成功也给，因为结果在框里）
    # 用 WScript.Shell 的内置 Popup（不加 Add-Type，避免编译/依赖问题；阻塞直到点掉）
    if (-not $Silent) { return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shell.Popup($text, 0, '本地知识库助手 - 备份', $icon) | Out-Null
    } catch {
        # 弹框失败也不能让用户一无所知：把回执文字留在日志里（静默时日志是唯一入口）
        Add-Content -LiteralPath $logFile -Value ('  [提示] 结果框弹出失败：' + $_.Exception.Message) -Encoding UTF8
    }
}

if ($Silent) {
    New-Item -ItemType Directory -Path (Join-Path $root 'logs') -Force | Out-Null
    Say ''
    Say ('===== ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' backup =====')
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$dst = Join-Path $root ('backup\' + $stamp)
Say ''
Say '===== 本地知识库助手 - 备份 ====='
$total = 0
foreach ($i in @('knowledge', 'rag_storage', 'config', 'data')) {
    $src = Join-Path $root $i
    if (Test-Path $src) {
        try {
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
            Copy-Item -LiteralPath $src -Destination (Join-Path $dst $i) -Recurse -Force -ErrorAction Stop
            $f = Get-ChildItem (Join-Path $dst $i) -Recurse -File -ErrorAction SilentlyContinue
            $mb = [math]::Round(($f | Measure-Object -Property Length -Sum).Sum / 1MB, 2)
            $total = $total + $mb
            Say ('  [OK] ' + $i + '  ' + $mb + ' MB') 'Green'
        } catch {
            $failed = $failed + $i
            Say ('  [失败] ' + $i + '  ' + $_.Exception.Message) 'Red'
        }
    } else {
        Say ('  [--] ' + $i + ' 不存在，已跳过') 'DarkGray'
    }
}
Say ''
if ($failed.Count -gt 0) {
    # 失败时只记日志、置退出码；弹框交给 backup_silent.vbs，避免出现两个框
    $status = 3
    Say ('备份未完成，失败项：' + ($failed -join ' / ')) 'Red'
    Say ('已保留部分结果：' + $dst) 'Yellow'
    Say ('可能原因：文件被占用（先关闭正在打开的文档），或磁盘空间不足。') 'Yellow'
    Say ('详细日志：' + $logFile) 'Yellow'
} else {
    Say ('备份完成：' + $dst + '  合计约 ' + $total + ' MB') 'Green'
    Say '还原方法：关闭服务后，把备份目录里的同名文件夹复制回项目根目录覆盖即可。' 'Cyan'
    Say '提示：服务运行中备份可能含正在写入的文件；要求高一致性时请先运行 stop.bat。' 'Yellow'
    Say '清理：直接删除 backup 下的旧日期文件夹。' 'Gray'
    Say ('详细日志：' + $logFile) 'Gray'
    ShowBox ("备份完成。`r`n`r`n备份目录：$dst`r`n合计约 $total MB`r`n`r`n还原方法：先停止服务（stop_silent.vbs），再把备份目录里的同名文件夹复制回项目根目录覆盖。`r`n详细日志：$logFile")
}
exit $status

