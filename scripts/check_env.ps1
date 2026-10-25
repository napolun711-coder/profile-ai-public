#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProfileHome = if ($env:PROFILE_AI_HOME) { $env:PROFILE_AI_HOME } else { Join-Path $env:LOCALAPPDATA 'ProfileAI' }
$ConfigDir = Join-Path $ProfileHome 'config'
$LogsDir = Join-Path $ProfileHome 'logs'
$DataDir = Join-Path $ProfileHome 'data'
$LightRagHome = Join-Path $ProfileHome 'backend\lightrag'
$FailCount = 0
$WarnCount = 0

function Write-Head([string]$Text) {
    Write-Host ''
    Write-Host ('  ' + $Text) -ForegroundColor Cyan
    Write-Host ('  ' + ('-' * 58)) -ForegroundColor DarkGray
}
function Check([string]$Level, [string]$Name, [string]$Detail = '', [string]$Hint = '') {
    if ($Level -eq 'PASS') { Write-Host '  [通过] ' -NoNewline -ForegroundColor Green }
    elseif ($Level -eq 'WARN') { Write-Host '  [注意] ' -NoNewline -ForegroundColor Yellow; $script:WarnCount++ }
    else { Write-Host '  [失败] ' -NoNewline -ForegroundColor Red; $script:FailCount++ }
    Write-Host $Name
    if ($Detail) { Write-Host ('         ' + $Detail) -ForegroundColor DarkGray }
    if ($Hint -and $Level -ne 'PASS') { Write-Host ('         建议：' + $Hint) -ForegroundColor Yellow }
}
function Test-Tcp([string]$HostName, [int]$Port, [int]$TimeoutMs = 1500) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch { return $false } finally { $client.Close() }
}
function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
}

Write-Host ''
Write-Host '  本地知识库助手 - 环境自检' -ForegroundColor White
Write-Host ('  时间：' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray
Write-Host ('  数据目录：' + $ProfileHome) -ForegroundColor DarkGray

Write-Head '1. Python 应用环境'
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $python) {
    $probe = & $python -c "import fastapi, uvicorn, requests, multipart, pypdf, docx, openpyxl; print('ok')" 2>&1
    if ($LASTEXITCODE -eq 0) { Check PASS '应用依赖可导入' } else { Check FAIL '应用依赖不完整' ($probe -join ' ') '运行 setup.cmd' }
} else {
    Check FAIL '项目 .venv 不存在' $python '运行 setup.cmd'
}

Write-Head '2. 用户数据目录'
foreach ($dir in @($ProfileHome, $ConfigDir, $DataDir, $LogsDir)) {
    if (Test-Path -LiteralPath $dir) {
        try {
            $probeFile = Join-Path $dir ('.write-test-' + [guid]::NewGuid().ToString('N'))
            Set-Content -LiteralPath $probeFile -Value 'ok' -Encoding ASCII -ErrorAction Stop
            Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
            Check PASS ('可写：' + $dir)
        } catch { Check FAIL ('不可写：' + $dir) $_.Exception.Message '检查目录权限' }
    } else { Check FAIL ('目录不存在：' + $dir) '' '运行 setup.cmd' }
}
$settingsFile = Join-Path $ConfigDir 'user-settings.json'
if (Test-Path -LiteralPath $settingsFile) { Check PASS '用户设置文件存在' } else { Check WARN '用户设置文件不存在' '首次启动会使用默认值' '打开设置页保存一次即可生成' }

Write-Head '3. LightRAG 服务'
$server = Join-Path $LightRagHome '.venv\Scripts\lightrag-server.exe'
$officialEnv = Join-Path $LightRagHome '.env'
if (Test-Path -LiteralPath $server) { Check PASS 'LightRAG 已安装' } else { Check FAIL 'LightRAG 未安装' $server '运行 setup.cmd' }
if (Test-Path -LiteralPath $officialEnv) { Check PASS 'LightRAG .env 存在' } else { Check FAIL 'LightRAG .env 缺失' $officialEnv '运行 setup.cmd' }

$runtime = Read-JsonFile (Join-Path $ConfigDir 'runtime.json')
$lightragUrl = if ($runtime -and $runtime.lightrag_url) { [string]$runtime.lightrag_url } else { 'http://127.0.0.1:9961' }
try { $uri = [Uri]$lightragUrl; $lrHost = $uri.Host; $lrPort = $uri.Port } catch { $lrHost = '127.0.0.1'; $lrPort = 9961 }
if (Test-Tcp $lrHost $lrPort) {
    try { $null = Invoke-WebRequest ($lightragUrl.TrimEnd('/') + '/health') -UseBasicParsing -TimeoutSec 3; Check PASS ('LightRAG 健康：' + $lightragUrl) }
    catch { Check WARN ('端口在监听但健康检查失败：' + $lightragUrl) '' '运行 appctl.bat restart' }
} else { Check WARN ('LightRAG 当前未运行：' + $lightragUrl) '启动器会尝试拉起' '若失败查看 ProfileAI\logs\lightrag_console.txt' }

$excluded = & netsh interface ipv4 show excludedportrange protocol=tcp 2>$null
$reserved = $false
foreach ($line in $excluded) {
    if ($line -match '^\s*(\d+)\s+(\d+)') {
        if ($lrPort -ge [int]$Matches[1] -and $lrPort -le [int]$Matches[2]) { $reserved = $true }
    }
}
if ($reserved) { Check WARN ('端口 ' + $lrPort + ' 位于 Windows 保留段') '启动器会自动切换到可用端口' '无需重启电脑' } else { Check PASS ('端口 ' + $lrPort + ' 不在 Windows 保留段') }

Write-Head '4. 模型与服务'
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
$ollamaUp = $false
try { $null = Invoke-WebRequest 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 3; $ollamaUp = $true } catch { }
if ($ollamaUp) {
    Check PASS 'Ollama 已运行'
    if ($ollama) {
        $models = (& $ollama.Source list 2>$null) -join ' '
        if ($models -match 'bge-m3') { Check PASS 'bge-m3 已安装' } else { Check WARN 'bge-m3 未安装' '' '运行 setup.cmd，或在设置页配置云端嵌入模型' }
    }
} else {
    Check WARN 'Ollama 未运行' '如使用本机嵌入，请安装并启动 Ollama' '或在设置页配置云端嵌入服务'
}

Write-Head '5. 磁盘空间'
$drive = (Get-Item -LiteralPath $ProfileHome).PSDrive.Name
$freeGB = [math]::Round((Get-PSDrive -Name $drive).Free / 1GB, 1)
if ($freeGB -lt 2) { Check WARN ('可用空间不足：' + $freeGB + ' GB') '知识库构建会产生较多文件' '清理磁盘后重试' } else { Check PASS ('可用空间：' + $freeGB + ' GB') }

Write-Host ''
Write-Host ('  ' + ('=' * 58)) -ForegroundColor DarkGray
if ($FailCount -gt 0) {
    Write-Host ('  自检结果：' + $FailCount + ' 项失败、' + $WarnCount + ' 项注意') -ForegroundColor Red
    exit 3
}
if ($WarnCount -gt 0) {
    Write-Host ('  自检结果：' + $WarnCount + ' 项注意，可以继续启动') -ForegroundColor Yellow
    exit 2
}
Write-Host '  自检结果：全部通过' -ForegroundColor Green
exit 0
