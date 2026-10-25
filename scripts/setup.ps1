#Requires -Version 5.1
<#
  本地知识库助手 - 一键安装/修复
  只写用户数据目录与项目内 .venv，不触碰原项目。
#>
[CmdletBinding()]
param(
    [string]$ProfileHome = "",
    [string]$LightRagVersion = "1.5.6",
    [string]$UvVersion = "0.11.29",
    [switch]$SkipOllama,
    [switch]$ForceUvDownload,
    [switch]$BootstrapUvOnly,
    [switch]$SkipShortcuts,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host ('== ' + $Text + ' ==') -ForegroundColor Cyan
}
function Write-OK([string]$Text) { Write-Host ('[通过] ' + $Text) -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host ('[注意] ' + $Text) -ForegroundColor Yellow }
function Refresh-Path {
    $parts = @(
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama')
    )
    foreach ($part in $parts) {
        if ((Test-Path -LiteralPath $part) -and ($env:Path -notlike ('*' + $part + '*'))) {
            $env:Path = $part + ';' + $env:Path
        }
    }
}
function Ensure-Directory([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}
function Download-File([string]$Url, [string]$OutFile, [int]$MaxTimeSeconds = 300) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --fail --retry 3 --retry-delay 2 --connect-timeout 20 --max-time $MaxTimeSeconds --progress-bar -o $OutFile $Url
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $OutFile)) { throw ('下载失败：' + $Url) }
        return
    }
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec $MaxTimeSeconds
    if (-not (Test-Path -LiteralPath $OutFile)) { throw ('下载失败：' + $Url) }
}
function Find-Uv {
    Refresh-Path
    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($command -and (Test-Path -LiteralPath $command.Source)) { return $command.Source }
    $candidates = @(
        (Join-Path $ProfileHome 'tools\uv\uv.exe'),
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $wingetPackages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path -LiteralPath $wingetPackages) {
        $found = Get-ChildItem -LiteralPath $wingetPackages -Directory -Filter 'astral-sh.uv*' -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Recurse -File -Filter 'uv.exe' -ErrorAction SilentlyContinue } |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return ''
}
function Add-UserPath([string]$Directory) {
    if (-not $Directory) { return }
    if ($env:Path -notlike ('*' + $Directory + '*')) { $env:Path = $Directory + ';' + $env:Path }
    try {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $parts = @($userPath -split ';' | Where-Object { $_ })
        if ($parts -notcontains $Directory) {
            [Environment]::SetEnvironmentVariable('Path', (($parts + $Directory) -join ';'), 'User')
        }
    } catch { Write-Warn ('无法写入用户 PATH，但本次安装仍会使用绝对路径：' + $Directory) }
}
function Install-UvStandalone {
    $archRaw = (([string]$env:PROCESSOR_ARCHITECTURE) + ' ' + ([string]$env:PROCESSOR_ARCHITEW6432)).ToUpperInvariant()
    $target = if ($archRaw -match 'ARM64') { 'aarch64-pc-windows-msvc' } else { 'x86_64-pc-windows-msvc' }
    $name = 'uv-' + $target + '.zip'
    $base = 'https://github.com/astral-sh/uv/releases/download/' + $UvVersion + '/'
    $tools = Join-Path $ProfileHome 'tools\uv'
    Ensure-Directory $tools
    $zip = Join-Path $env:TEMP ('profile-ai-' + $name)
    $sumFile = $zip + '.sha256'
    try {
        Write-Host ('下载 uv ' + $UvVersion + '：' + ($base + $name))
        Download-File -Url ($base + $name) -OutFile $zip -MaxTimeSeconds 300
        try {
            Download-File -Url ($base + $name + '.sha256') -OutFile $sumFile -MaxTimeSeconds 60
            $raw = [IO.File]::ReadAllText($sumFile).Trim()
            $expected = (($raw -split '\s+')[0]).ToLowerInvariant()
            $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($expected -ne $actual) { throw 'uv 安装包 SHA256 校验失败' }
            Write-OK 'uv 安装包校验通过'
        } catch {
            if ($_.Exception.Message -like '*SHA256 校验失败*') { throw }
            Write-Warn '未能下载 uv 校验文件，继续使用 HTTPS 官方发布包'
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $tools -Force
        $uvExe = Get-ChildItem -LiteralPath $tools -Recurse -File -Filter 'uv.exe' -ErrorAction Stop | Select-Object -First 1
        if (-not $uvExe) { throw 'uv 安装包中没有 uv.exe' }
        Unblock-File -LiteralPath $uvExe.FullName -ErrorAction SilentlyContinue
        Add-UserPath $uvExe.DirectoryName
        return $uvExe.FullName
    } finally {
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $sumFile -Force -ErrorAction SilentlyContinue
    }
}
function Resolve-Uv {
    if (-not $ForceUvDownload) {
        $found = Find-Uv
        if ($found) { return $found }
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host '未找到 uv，正在通过 winget 安装...'
            try {
                & winget install --id astral-sh.uv --exact --silent --accept-package-agreements --accept-source-agreements | Out-Host
                Refresh-Path
                $found = Find-Uv
                if ($found) { return $found }
                Write-Warn 'winget 已完成但当前进程仍找不到 uv，改用官方 Windows 包'
            } catch {
                Write-Warn ('winget 安装 uv 失败：' + $_.Exception.Message + '；改用官方 Windows 包')
            }
        } else {
            Write-Warn '未检测到 winget，改用 uv 官方 Windows 包'
        }
    }
    return (Install-UvStandalone)
}

if (-not $ProfileHome) {
    $ProfileHome = if ($env:PROFILE_AI_HOME) { $env:PROFILE_AI_HOME } else { Join-Path $env:LOCALAPPDATA 'ProfileAI' }
}
$ProfileHome = [System.IO.Path]::GetFullPath($ProfileHome)
$env:PROFILE_AI_HOME = $ProfileHome
$configDir = Join-Path $ProfileHome 'config'
$dataDir = Join-Path $ProfileHome 'data'
$logsDir = Join-Path $ProfileHome 'logs'
$backendDir = Join-Path $ProfileHome 'backend'
$lightragHome = Join-Path $backendDir 'lightrag'
$lightragVenv = Join-Path $lightragHome '.venv'
$lightragPython = Join-Path $lightragVenv 'Scripts\python.exe'
$lightragServer = Join-Path $lightragVenv 'Scripts\lightrag-server.exe'

Write-Host ''
Write-Host '============================================================' -ForegroundColor DarkGray
Write-Host ' 本地知识库助手 - 一键安装' -ForegroundColor White
Write-Host (' 用户数据：' + $ProfileHome) -ForegroundColor DarkGray
Write-Host '============================================================' -ForegroundColor DarkGray

foreach ($dir in @(
    $ProfileHome, $configDir, $dataDir, (Join-Path $dataDir 'memory'),
    (Join-Path $dataDir 'uploads'), (Join-Path $dataDir 'parsed'),
    (Join-Path $dataDir 'inbox'), (Join-Path $dataDir 'originals'),
    (Join-Path $dataDir 'watch'), (Join-Path $dataDir 'lightrag_input'),
    (Join-Path $ProfileHome 'knowledge'), (Join-Path $ProfileHome 'output'),
    $logsDir, (Join-Path $ProfileHome 'models'),
    (Join-Path $ProfileHome 'rag_storage'), $backendDir, $lightragHome
)) { Ensure-Directory $dir }
Write-OK '用户数据目录已创建'

$settingsPath = Join-Path $configDir 'user-settings.json'
if (-not (Test-Path -LiteralPath $settingsPath)) {
    Copy-Item -LiteralPath (Join-Path $root 'config\user-settings.example.json') -Destination $settingsPath -Force
    Write-OK '已创建用户设置文件'
}

Write-Step '1/6 安装 uv'
$uvExe = Resolve-Uv
if (-not $uvExe -or -not (Test-Path -LiteralPath $uvExe)) {
    throw '未能准备 uv。请检查网络、代理或安全软件拦截。'
}
Write-OK ('uv：' + $uvExe)
$uvVersionText = & $uvExe --version
Write-OK ('版本：' + $uvVersionText)
if ($BootstrapUvOnly) { exit 0 }

Write-Step '2/6 同步应用依赖'
Push-Location $root
try {
    & $uvExe sync --frozen --no-dev
    if ($LASTEXITCODE -ne 0) { throw 'uv sync 失败，请检查网络/代理' }
} finally { Pop-Location }
Write-OK '应用依赖已就绪'

Write-Step ('3/6 安装 LightRAG ' + $LightRagVersion)
$versionFile = Join-Path $lightragHome 'installed-version.txt'
$installedVersion = if (Test-Path -LiteralPath $versionFile) { (Get-Content -LiteralPath $versionFile -Raw).Trim() } else { '' }
if ((-not (Test-Path -LiteralPath $lightragServer)) -or ($installedVersion -ne $LightRagVersion)) {
    if (-not [System.IO.Path]::IsPathRooted($lightragVenv)) { throw 'LightRAG venv path must be absolute' }
    Push-Location $lightragHome
    try {
        & $uvExe venv --python 3.12 --clear $lightragVenv
        if ($LASTEXITCODE -ne 0) { throw '创建 LightRAG Python 3.12 环境失败' }
        & $uvExe pip install --python $lightragPython ('lightrag-hku[api]==' + $LightRagVersion)
        if ($LASTEXITCODE -ne 0) { throw 'LightRAG 下载或安装失败' }
    } finally { Pop-Location }
    Set-Content -LiteralPath $versionFile -Value $LightRagVersion -Encoding ASCII
} else {
    Write-OK ('已安装 LightRAG ' + $installedVersion)
}

$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $lightragPython -c 'import ollama' 2>$null | Out-Null
$ollamaProbeExit = $LASTEXITCODE
$ErrorActionPreference = $previousPreference
if ($ollamaProbeExit -ne 0) {
    Push-Location $lightragHome
    try {
        & $uvExe pip install --python $lightragPython 'ollama'
        if ($LASTEXITCODE -ne 0) { throw 'LightRAG 的 Ollama 客户端安装失败' }
    } finally { Pop-Location }
}

$envExample = Join-Path $lightragHome 'env.example'
$lightragEnv = Join-Path $lightragHome '.env'
if (-not (Test-Path -LiteralPath $envExample)) {
    $url = 'https://raw.githubusercontent.com/HKUDS/LightRAG/v' + $LightRagVersion + '/env.example'
    Write-Host ('下载 LightRAG 官方环境模板：' + $url)
    Download-File -Url $url -OutFile $envExample -MaxTimeSeconds 120
}
if (-not (Test-Path -LiteralPath $lightragEnv)) {
    Copy-Item -LiteralPath $envExample -Destination $lightragEnv -Force
    Write-OK '已使用 LightRAG 官方默认 .env 模板；敏感配置由应用设置页注入'
} else {
    Write-OK 'LightRAG .env 已存在，保持不动'
}

Write-Step '4/6 检查 Ollama 与嵌入模型'
if ($SkipOllama) {
    Write-Warn '已跳过 Ollama；请在设置页配置云端嵌入服务'
} else {
    Refresh-Path
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host '未找到 Ollama，正在通过 winget 安装...'
            & winget install --id Ollama.Ollama --exact --silent --accept-package-agreements --accept-source-agreements
            Refresh-Path
            $ollama = Get-Command ollama -ErrorAction SilentlyContinue
        }
    }
    if (-not $ollama) {
        Write-Warn 'Ollama 未安装。可稍后安装，或在设置页使用云端嵌入服务'
    } else {
        $apiUp = $false
        try {
            $null = Invoke-WebRequest 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 3
            $apiUp = $true
        } catch { $apiUp = $false }
        if (-not $apiUp) {
            Start-Process -FilePath $ollama.Source -ArgumentList 'serve' -WindowStyle Hidden -ErrorAction SilentlyContinue
            for ($i = 0; $i -lt 30; $i++) {
                Start-Sleep -Seconds 2
                try {
                    $null = Invoke-WebRequest 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 3
                    $apiUp = $true
                    break
                } catch { }
            }
        }
        if (-not $apiUp) {
            Write-Warn 'Ollama 已安装但未就绪；启动后可重新运行 setup.cmd 修复'
        } else {
            $models = (& $ollama.Source list 2>$null) -join ' '
            if ($models -notmatch 'bge-m3') {
                Write-Host '正在下载嵌入模型 bge-m3（首次约 1GB 以上，请耐心等待）...'
                & $ollama.Source pull bge-m3:latest
                if ($LASTEXITCODE -ne 0) { Write-Warn 'bge-m3 下载失败；可在设置页改用云端嵌入服务' }
            } else {
                Write-OK '嵌入模型 bge-m3 已就绪'
            }
        }
    }
}

Write-Step '5/6 创建快捷方式与运行目录'
if ($SkipShortcuts) {
    Write-Warn '已跳过快捷方式创建'
} else {
    $shortcutScript = Join-Path $PSScriptRoot 'install_shortcuts.ps1'
    if (Test-Path -LiteralPath $shortcutScript) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $shortcutScript
    }
}
Write-OK '安装步骤完成'

Write-Step '6/6 启动应用'
if ($NoStart) {
    Write-Host '已按 -NoStart 跳过启动。双击 appctl.vbs 可启动。'
} else {
    $launcher = Join-Path $root 'appctl.vbs'
    if (Test-Path -LiteralPath $launcher) {
        Start-Process -FilePath (Join-Path $env:WINDIR 'System32\wscript.exe') -ArgumentList @(('"' + $launcher + '"'), 'start') -WindowStyle Hidden
        Write-OK '已启动。首次使用请在设置页填写生成模型 API Key。'
    } else {
        Write-Warn '未找到 appctl.vbs，请手动启动'
    }
}
Write-Host ''
Write-Host ('配置与数据目录：' + $ProfileHome) -ForegroundColor Cyan
Write-Host '再次运行 setup.cmd 可做幂等修复。' -ForegroundColor DarkGray
exit 0
