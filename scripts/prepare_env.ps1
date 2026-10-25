# 新机器环境准备（检查 uv / Ollama / LightRAG，并同步本项目依赖）
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Write-Host ''
Write-Host '===== 本地知识库助手 - 环境准备 =====' -ForegroundColor Cyan
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCmd) { Write-Host ('[通过] uv：' + $uvCmd.Source) -ForegroundColor Green }
else { Write-Host '[失败] 未安装 uv，请先安装（任选其一）：winget install astral-sh.uv / 访问 https://docs.astral.sh/uv/' -ForegroundColor Red }

if ($uvCmd) {
    Push-Location $root
    Write-Host '正在同步本项目依赖（uv sync）...' -ForegroundColor DarkGray
    & uv sync
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -eq 0) { Write-Host '[通过] 本项目依赖已就绪' -ForegroundColor Green } else { Write-Host '[失败] uv sync 未成功，请检查网络/代理后重试' -ForegroundColor Red }
}

try {
    $null = Invoke-WebRequest 'http://localhost:11434/api/tags' -UseBasicParsing -TimeoutSec 5
    Write-Host '[通过] Ollama 已运行' -ForegroundColor Green
    $models = (& ollama list 2>$null) -join ' '
    if ($models -match 'bge-m3') { Write-Host '[通过] 嵌入模型 bge-m3 已就绪' -ForegroundColor Green } else { Write-Host '[注意] 缺少嵌入模型，请执行：ollama pull bge-m3' -ForegroundColor Yellow }
} catch { Write-Host '[注意] Ollama 未运行，请安装并启动：https://ollama.com' -ForegroundColor Yellow }

$lrHome = ''
$projectEnv = Join-Path $root '.env'
if (Test-Path $projectEnv) {
    $line = Get-Content $projectEnv -Encoding UTF8 | Where-Object { $_ -match '^LIGHTRAG_HOME=' } | Select-Object -First 1
    if ($line) { $lrHome = ($line -split '=',2)[1].Trim() }
}
$cfgPath = Join-Path $root 'config\user-settings.json'
if (-not $lrHome -and (Test-Path $cfgPath)) {
    try { $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json; if ($cfg.paths.lightrag_home) { $lrHome = [string]$cfg.paths.lightrag_home } } catch { }
}
if (-not $lrHome) { if (Test-Path (Join-Path $root 'lightrag')) { $lrHome = Join-Path $root 'lightrag' } else { $lrHome = (Join-Path $root 'lightrag') } }
if (-not [System.IO.Path]::IsPathRooted($lrHome)) { $lrHome = Join-Path $root $lrHome }
if (Test-Path $lrHome) {
    Write-Host ('[通过] LightRAG 目录：' + $lrHome) -ForegroundColor Green
    if (-not (Test-Path (Join-Path $lrHome '.env'))) { Write-Host '[失败] 缺少 LightRAG 的 .env（模型/密钥配置）' -ForegroundColor Red }
    $exe = Join-Path $lrHome '.venv\Scripts\lightrag-server.exe'
    if (Test-Path $exe) { Write-Host '[通过] LightRAG 运行环境已就绪' -ForegroundColor Green }
    else {
        Write-Host '[注意] LightRAG 运行环境缺失（.venv 不能跨机复制，需重建）：' -ForegroundColor Yellow
        $pkg = Get-ChildItem -LiteralPath $lrHome -Directory | Where-Object { $_.Name -like 'LightRAG-*' } | Select-Object -First 1
        if ($pkg) {
            Write-Host ' 请在该目录执行以下三条命令：' -ForegroundColor Yellow
            Write-Host ('  cd ' + $pkg.FullName) -ForegroundColor White
            Write-Host '  uv venv .venv' -ForegroundColor White
            Write-Host '  uv pip install "lightrag-hku[api]==1.5.6"' -ForegroundColor White
        }
    }
} else { Write-Host ('[失败] 未找到 LightRAG 目录：' + $lrHome + '（可在设置页修正）') -ForegroundColor Red }

$chk = Join-Path $root 'scripts\check_env.ps1'
if (Test-Path $chk) { Write-Host ''; & powershell -NoProfile -ExecutionPolicy Bypass -File $chk }
Write-Host ''
Write-Host '准备完成后，双击「启动.bat」即可。' -ForegroundColor Cyan