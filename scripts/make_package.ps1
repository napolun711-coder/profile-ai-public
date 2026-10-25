# 打包为可迁移交付包（不含任何 .venv，体积小）
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format 'yyyyMMdd_HHmm'
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Path $dist -Force | Out-Null
$zip = Join-Path $dist ('profile-ai-交付包_' + $stamp + '.zip')
$stage = Join-Path $env:TEMP ('pkg_' + $stamp)
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage 'lightrag') -Force | Out-Null

Write-Host '== 1/4 复制项目文件 ==' -ForegroundColor Cyan
$exclude = @('.venv','logs','__pycache__','dist','.git','node_modules','backup')
Get-ChildItem -LiteralPath $root -Force | Where-Object { $exclude -notcontains $_.Name } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force
}
Get-ChildItem -LiteralPath $stage -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host '== 2/4 复制 LightRAG（源码 + .env，不含 .venv）==' -ForegroundColor Cyan
$lrHome = ''
$projectEnv = Join-Path $root '.env'
if (Test-Path $projectEnv) {
    $line = Get-Content $projectEnv -Encoding UTF8 | Where-Object { $_ -match '^LIGHTRAG_HOME=' } | Select-Object -First 1
    if ($line) { $lrHome = ($line -split '=',2)[1].Trim() }
if (-not $lrHome) { if (Test-Path (Join-Path $root 'lightrag')) { $lrHome = Join-Path $root 'lightrag' } else { $lrHome = (Join-Path $root 'lightrag') } }
if ($lrHome -and -not [System.IO.Path]::IsPathRooted($lrHome)) { $lrHome = Join-Path $root $lrHome }
}
if (Test-Path $lrHome) {
    $src = Get-ChildItem -LiteralPath $lrHome -Directory | Where-Object { $_.Name -like 'LightRAG-*' } | Select-Object -First 1
    if ($src) { Copy-Item -LiteralPath $src.FullName -Destination (Join-Path $stage 'lightrag') -Recurse -Force; Write-Host ('  源码：' + $src.Name) }
    $lrEnvFile = Join-Path $lrHome '.env'
    if (Test-Path $lrEnvFile) { Copy-Item -LiteralPath $lrEnvFile -Destination (Join-Path $stage 'lightrag\.env') -Force; Write-Host '  配置：lightrag\.env（含 API Key）' }
} else { Write-Host ('  [注意] 未找到 LightRAG 目录：' + $lrHome) -ForegroundColor Yellow }

Write-Host '== 3/4 生成包内配置（指向包内 lightrag，数据目录用相对路径）==' -ForegroundColor Cyan
$cfgPath = Join-Path $stage 'config\user-settings.json'
if (Test-Path $cfgPath) {
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg.paths.lightrag_home = 'lightrag'
    $cfg.graph.working_dir = './rag_storage'
    $cfg.graph.input_dir = './knowledge'
    ($cfg | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $cfgPath -Encoding UTF8
}

Write-Host '== 4/4 压缩 ==' -ForegroundColor Cyan
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
$size = [math]::Round((Get-Item $zip).Length / 1MB, 2)
Write-Host ('完成：' + $zip + '  （' + $size + ' MB）') -ForegroundColor Green
Write-Host '注意：包内含 LightRAG 的 .env 与 config\user-settings.json（可能有密钥），请勿外发。' -ForegroundColor Yellow