#Requires -Version 5.1
<#
  本地知识库助手 - 环境自检（Phase 4.1）

  作用：检查运行本系统所需的软件与配置是否就绪。
  只做检查：不修改任何文件、不启动任何服务。

  用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_env.ps1
  退出码：0 = 全部通过；2 = 有告警可继续；3 = 有致命问题，请先处理
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:FailCount = 0
$script:WarnCount = 0
$script:EnvValues = @{}

function Read-EnvFile {
    param([string]$Path)
    $result = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $result }
    foreach ($raw in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $line = $raw.Trim()
        if ($line.Length -eq 0) { continue }
        if ($line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $val = $line.Substring($idx + 1).Trim()
        if ($val.Length -ge 2) {
            $first = $val.Substring(0, 1)
            $last = $val.Substring($val.Length - 1, 1)
            if (($first -eq [char]34 -and $last -eq [char]34) -or ($first -eq [char]39 -and $last -eq [char]39)) {
                $val = $val.Substring(1, $val.Length - 2)
            }
        }
        $result[$key] = $val
    }
    return $result
}

function Get-EnvValue {
    param([string]$Key, [string]$Default = '', [hashtable]$Table)
    $fromOs = [System.Environment]::GetEnvironmentVariable($Key)
    if ($fromOs) { return $fromOs }
    if ($Table -and $Table.ContainsKey($Key)) { return $Table[$Key] }
    return $Default
}

function Test-TcpPort {
    param([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 1500)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-UrlHostPort {
    param([string]$Url, [int]$DefaultPort)
    try {
        $uri = [System.Uri]$Url
        $targetHost = $uri.Host
        if (-not $targetHost) { $targetHost = '127.0.0.1' }
        $port = $uri.Port
        if ($port -le 0) { $port = $DefaultPort }
        return @($targetHost, $port)
    } catch {
        return @('127.0.0.1', $DefaultPort)
    }
}

function Write-Section {
    param([string]$Text)
    Write-Host ''
    Write-Host ('  ' + $Text) -ForegroundColor Cyan
    Write-Host ('  ' + ('-' * 58)) -ForegroundColor DarkGray
}

function Write-Check {
    param([string]$Level, [string]$Name, [string]$Detail = '', [string]$Hint = '')
    if ($Level -eq 'PASS') {
        Write-Host '  [通过] ' -NoNewline -ForegroundColor Green
    } elseif ($Level -eq 'WARN') {
        Write-Host '  [注意] ' -NoNewline -ForegroundColor Yellow
        $script:WarnCount++
    } else {
        Write-Host '  [失败] ' -NoNewline -ForegroundColor Red
        $script:FailCount++
    }
    Write-Host $Name
    if ($Detail) { Write-Host ('         ' + $Detail) -ForegroundColor DarkGray }
    if ($Hint -and $Level -ne 'PASS') { Write-Host ('         建议：' + $Hint) -ForegroundColor Yellow }
}

# ============================================================
# 配置读取
# ============================================================

Write-Host ''
Write-Host '  本地知识库助手 - 环境自检' -ForegroundColor White
Write-Host ('  时间：' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray

$script:EnvValues = Read-EnvFile (Join-Path $script:ProjectRoot '.env')

$appHost = Get-EnvValue 'APP_HOST' '127.0.0.1' $script:EnvValues
$appPort = [int](Get-EnvValue 'APP_PORT' '8100' $script:EnvValues)
$lightragUrl = Get-EnvValue 'LIGHTRAG_URL' 'http://127.0.0.1:9621' $script:EnvValues
$lightragHome = Get-EnvValue 'LIGHTRAG_HOME' (Join-Path $script:ProjectRoot 'lightrag') $script:EnvValues
if (-not $lightragHome) { if (Test-Path (Join-Path $script:ProjectRoot 'lightrag')) { $lightragHome = Join-Path $script:ProjectRoot 'lightrag' } else { $lightragHome = (Join-Path $script:ProjectRoot 'lightrag') } }
if (-not [System.IO.Path]::IsPathRooted($lightragHome)) { $lightragHome = Join-Path $script:ProjectRoot $lightragHome }
$lightragExeDefault = Join-Path $lightragHome '.venv\Scripts\lightrag-server.exe'
$lightragExe = Get-EnvValue 'LIGHTRAG_SERVER_EXE' $lightragExeDefault $script:EnvValues
$ollamaUrl = Get-EnvValue 'OLLAMA_URL' 'http://localhost:11434' $script:EnvValues
$qdrantUrl = Get-EnvValue 'QDRANT_URL' 'http://localhost:6333' $script:EnvValues
$qdrantEnabled = (Get-EnvValue 'QDRANT_ENABLED' 'auto' $script:EnvValues).ToLower()

# 读取网页设置页保存的用户配置（Phase 4.2/4.3）：显示“实际生效”的取值
$userConfPath = Join-Path $script:ProjectRoot 'config\user-settings.json'
$userVectorType = ''
$userQdrantUrl = ''
$userLightragHome = ''
if (Test-Path -LiteralPath $userConfPath) {
    try {
        $userConf = Get-Content -LiteralPath $userConfPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($userConf.vector_store) {
            $userVectorType = [string]$userConf.vector_store.type
            $userQdrantUrl = [string]$userConf.vector_store.qdrant_url
        }
        if ($userConf.paths) { $userLightragHome = [string]$userConf.paths.lightrag_home }
    } catch { }
}
if ($userLightragHome) { $lightragHome = $userLightragHome }
if ($userQdrantUrl) { $qdrantUrl = $userQdrantUrl }

$python = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
$ragEnvFile = Join-Path $lightragHome '.env'
$ragEnv = Read-EnvFile $ragEnvFile

$lrUrl = Get-UrlHostPort $lightragUrl 9621
$olUrl = Get-UrlHostPort $ollamaUrl 11434
$qdUrl = Get-UrlHostPort $qdrantUrl 6333

# ============================================================
# 1. 本项目
# ============================================================

Write-Section '1. 本项目'

if (Test-Path -LiteralPath (Join-Path $script:ProjectRoot 'main.py')) {
    Write-Check 'PASS' ('项目目录：' + $script:ProjectRoot)
} else {
    Write-Check 'FAIL' '项目目录' '未找到 main.py' '请在项目根目录运行本脚本'
}

if (Test-Path -LiteralPath (Join-Path $script:ProjectRoot '.env')) {
    Write-Check 'PASS' '配置文件 .env 已存在'
} else {
    Write-Check 'WARN' '配置文件 .env 不存在' '将全部使用默认值（与开发环境默认一致）' '如需修改配置，可复制 .env.example 为 .env'
}

if (Test-Path -LiteralPath $python) {
    $ver = & $python -c 'import sys; print(sys.version.split()[0])' 2>$null
    Write-Check 'PASS' ('Python 运行环境就绪（' + $ver + '）')
} else {
    Write-Check 'FAIL' 'Python 运行环境 .venv 缺失' '未找到 ' + $python '在项目目录执行：uv sync' 
}

$userInput = ''
if ($userConf -and $userConf.graph) { $userInput = [string]$userConf.graph.input_dir }
if (-not $userInput) { $userInput = 'knowledge' }
if (-not [System.IO.Path]::IsPathRooted($userInput)) { $userInput = Join-Path $script:ProjectRoot $userInput }
$userInput = [System.IO.Path]::GetFullPath($userInput)
if (Test-Path -LiteralPath $userInput) {
    Write-Check 'PASS' ('文档输入目录：' + $userInput)
} else {
    Write-Check 'WARN' '文档输入目录不存在（不影响问答）' $userInput '可在设置页修正“文档输入目录”'
}
$userWorking = ""
if ($userConf -and $userConf.graph) { $userWorking = [string]$userConf.graph.working_dir }
if (-not $userWorking) { $userWorking = "rag_storage" }
if (-not [System.IO.Path]::IsPathRooted($userWorking)) { $userWorking = Join-Path $script:ProjectRoot $userWorking }
$userWorking = [System.IO.Path]::GetFullPath($userWorking)
if (Test-Path -LiteralPath $userWorking) {
    Write-Check "PASS" ("知识库数据目录：" + $userWorking)
} else {
    Write-Check "FAIL" "知识库数据目录不存在" $userWorking "请在设置页修正“知识库数据目录”，或先运行「迁移数据.bat」"
}

# ============================================================
# 2. 本项目依赖
# ============================================================

Write-Section '2. 本项目依赖（缺一不可）'

$sitePackages = Join-Path $script:ProjectRoot '.venv\Lib\site-packages'

if (Test-Path -LiteralPath $python) {
    $probe = & $python -c 'import fastapi, uvicorn, requests' 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Check 'PASS' '核心依赖可导入（fastapi / uvicorn / requests）'
    } else {
        Write-Check 'FAIL' '核心依赖不完整' ($probe -join ' ') '在项目目录执行：uv sync'
    }
} else {
    Write-Check 'FAIL' '核心依赖检查被跳过' 'Python 运行环境不存在'
}

if (Test-Path -LiteralPath (Join-Path $sitePackages 'python_multipart')) {
    Write-Check 'PASS' '上传功能依赖 python-multipart 已安装'
} else {
    Write-Check 'FAIL' '上传功能依赖 python-multipart 缺失' '缺少它会导致程序启动即失败（历史上出现过）' '在项目目录执行：uv sync（或 uv add python-multipart）'
}

# Phase 8：本地解析依赖（pypdf / python-docx / openpyxl）
if (Test-Path -LiteralPath $python) {
    $parseProbe = & $python -c 'import pypdf, docx, openpyxl' 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Check 'PASS' '文档解析依赖可导入（pypdf / python-docx / openpyxl）'
    } else {
        Write-Check 'FAIL' '文档解析依赖不完整' ($parseProbe -join ' ') '在项目目录执行：uv sync'
    }
}

$logsDir = Join-Path $script:ProjectRoot 'logs'
if (-not (Test-Path -LiteralPath $logsDir)) {
    try { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null } catch { }
}
if (Test-Path -LiteralPath $logsDir) {
    Write-Check 'PASS' '日志目录可写（logs）'
} else {
    Write-Check 'WARN' '日志目录创建失败' '将只输出到窗口' '检查项目目录是否有写入权限'
}

# ============================================================
# 3. 端口占用
# ============================================================

Write-Section '3. 端口'

$appUp = Test-TcpPort '127.0.0.1' $appPort
if ($appUp) {
    Write-Check 'PASS' ('本应用端口 ' + $appPort + ' 已在监听（服务正在运行）')
} else {
    Write-Check 'PASS' ('本应用端口 ' + $appPort + ' 空闲，可以启动')
}

# ============================================================
# 4. LightRAG（知识库服务）
# ============================================================

# ============================================================
# 3.5 手机/局域网访问（Phase 7.1）
# ============================================================

Write-Section '3.5 手机/局域网访问'

if ($appHost -eq '0.0.0.0' -or $appHost -eq '') {
    $lanIps = @(Get-NetIPConfiguration -ErrorAction SilentlyContinue | Where-Object { $_.IPv4DefaultGateway -ne $null } | ForEach-Object { $_.IPv4Address.IPAddress })
    if ($lanIps.Count -gt 0) {
        Write-Check 'PASS' ('已开放局域网访问（APP_HOST=' + $appHost + '）  手机访问：' + (($lanIps | ForEach-Object { 'http://' + $_ + ':' + $appPort + '/web' }) -join '   '))
        Write-Check 'WARN' '手机访问失败时优先检查防火墙' '需允许 Python 通过 Windows 防火墙的“专用”网络' '控制面板 → Windows Defender 防火墙 → 允许应用通过防火墙 → 勾选 Python 的“专用”'
    } else {
        Write-Check 'WARN' 'APP_HOST=0.0.0.0，但未取到局域网 IPv4' '请用 ipconfig 查看当前网卡 IPv4' '手机访问 http://电脑IP:' + $appPort + '/web'
    }
} else {
    Write-Check 'PASS' ('当前仅本机访问（APP_HOST=' + $appHost + '）') '如需手机访问：把 .env 的 APP_HOST 改为 0.0.0.0 后运行 restart.bat' '详见 docs\手机局域网访问说明.md'
}

Write-Section '4. LightRAG 知识库服务'

if (Test-Path -LiteralPath $lightragHome) {
    Write-Check 'PASS' ('LightRAG 目录：' + $lightragHome)
} else {
    Write-Check 'FAIL' 'LightRAG 目录不存在' $lightragHome '确认 LIGHTRAG_HOME 配置，或重新安装 LightRAG'
}

if (Test-Path -LiteralPath $ragEnvFile) {
    Write-Check 'PASS' 'LightRAG 配置文件 .env 已存在（模型与密钥来源）'
} else {
    Write-Check 'FAIL' 'LightRAG 配置文件 .env 缺失' $ragEnvFile '缺少它会导致模型/向量库配置丢失，知识库不可用'
}

if (Test-Path -LiteralPath $lightragExe) {
    Write-Check 'PASS' 'lightrag-server 可执行文件就绪'
} else {
    Write-Check 'FAIL' 'lightrag-server 不存在' $lightragExe '确认 LightRAG 的 .venv 是否完整'
}

if (Test-TcpPort $lrUrl[0] $lrUrl[1]) {
    Write-Check 'PASS' ('LightRAG 已在下运行（' + $lrUrl[0] + ':' + $lrUrl[1] + '）')
} else {
    Write-Check 'WARN' ('LightRAG 未运行（' + $lrUrl[0] + ':' + $lrUrl[1] + '）') '启动脚本会自动拉起它' '若启动失败，查看 LightRAG 目录下的 lightrag.log'
}

# ============================================================
# 5. 模型与密钥
# ============================================================

Write-Section '5. 模型与密钥（用于知识库构建与问答）'

$llmBinding = $ragEnv['LLM_BINDING']
$llmModel = $ragEnv['LLM_MODEL']
$llmKey = $ragEnv['LLM_BINDING_API_KEY']
$embBinding = $ragEnv['EMBEDDING_BINDING']
$embModel = $ragEnv['EMBEDDING_MODEL']
$rerankBinding = $ragEnv['RERANK_BINDING']
$rerankKey = $ragEnv['RERANK_BINDING_API_KEY']

if ($llmBinding) {
    $detail = '绑定=' + $llmBinding + '，模型=' + $llmModel
    if (-not $llmKey) {
        Write-Check 'FAIL' '问答大模型未配置 API Key' $detail '在 LightRAG 的 .env 中填写 LLM_BINDING_API_KEY'
    } else {
        Write-Check 'PASS' '问答大模型配置正常（启动时强制 DeepSeek openai + deepseek-chat）' $detail
    }
} else {
    Write-Check 'WARN' '未读取到问答大模型配置' 'LightRAG .env 中无 LLM_BINDING' '确认 LightRAG 配置文件'
}

if ($embBinding) {
    $detail = '绑定=' + $embBinding + '，模型=' + $embModel
    Write-Check 'PASS' '向量化配置正常（启动时强制 Ollama + bge-m3:latest）' $detail
} else {
    Write-Check 'WARN' '未读取到向量化配置' 'LightRAG .env 中无 EMBEDDING_BINDING'
}

if ($rerankBinding -and $rerankBinding -ne 'null') {
    if ($rerankKey) {
        Write-Check 'PASS' ('重排序（Rerank）已启用：' + $rerankBinding)
    } else {
        Write-Check 'WARN' ('重排序已启用但缺少 Key：' + $rerankBinding) '问答仍可用，但可能报错' '填写 RERANK_BINDING_API_KEY，或把 RERANK_BINDING 设为 null 关闭'
    }
} else {
    Write-Check 'PASS' '重排序未启用（Rerank 关闭）'
}

$vectorStorage = $ragEnv['LIGHTRAG_VECTOR_STORAGE']
if ($userVectorType -eq 'qdrant') { $vectorStorage = 'QdrantVectorDBStorage' }
elseif ($userVectorType -eq 'local') { $vectorStorage = 'NanoVectorDBStorage' }
if ($vectorStorage) {
    $detail = '向量存储=' + $vectorStorage
    if ($vectorStorage -like '*Qdrant*') {
        Write-Check 'PASS' '向量存储：Qdrant（需要 Qdrant 服务）' $detail
    } else {
        Write-Check 'PASS' '向量存储：本地文件（无需 Qdrant）' $detail
    }
} else {
    Write-Check 'PASS' '向量存储：使用 LightRAG 默认（本地文件）'
}

# ============================================================
# 6. Ollama（本地向量化模型）
# ============================================================

Write-Section '6. Ollama 本地模型'

$needOllama = ($embBinding -eq 'ollama') -or ($llmBinding -eq 'ollama')
if (-not $needOllama) {
    Write-Check 'PASS' '当前配置未使用 Ollama（可跳过）'
} elseif (Test-TcpPort $olUrl[0] $olUrl[1]) {
    Write-Check 'PASS' ('Ollama 已在下运行（' + $olUrl[0] + ':' + $olUrl[1] + '）')
    if ($embModel) {
        $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
        if ($ollamaCmd) {
            $models = & ollama list 2>$null
            $hit = $false
            foreach ($row in $models) { if ($row -like ($embModel + '*')) { $hit = $true } }
            if ($hit) {
                Write-Check 'PASS' ('向量化模型已拉取：' + $embModel)
            } else {
                Write-Check 'WARN' ('未找到向量化模型：' + $embModel) '知识库构建会失败' ('在命令行执行：ollama pull ' + $embModel)
            }
        }
    }
} else {
    Write-Check 'WARN' 'Ollama 未运行' '当前配置需要它做本地向量化' '启动 Ollama 桌面程序后重试'
}

# ============================================================
# 7. Qdrant（仅当向量存储为 Qdrant 时才需要）
# ============================================================

Write-Section '7. Qdrant 向量数据库'

if ($qdrantEnabled -eq 'false') {
    Write-Check 'PASS' '已配置为不使用 Qdrant（QDRANT_ENABLED=false）'
} elseif ($vectorStorage -and -not ($vectorStorage -like '*Qdrant*') -and $qdrantEnabled -eq 'auto') {
    Write-Check 'PASS' '当前向量存储不是 Qdrant，无需 Qdrant 服务'
} elseif (Test-TcpPort $qdUrl[0] $qdUrl[1]) {
    Write-Check 'PASS' ('Qdrant 已在下运行（' + $qdUrl[0] + ':' + $qdUrl[1] + '）')
} else {
    Write-Check 'WARN' ('Qdrant 未运行（' + $qdUrl[0] + ':' + $qdUrl[1] + '）') '当前知识库使用 Qdrant 存储，未启动则问答会失败' '启动 Docker Desktop（容器名 qdrant），或稍后在设置页把向量存储切换为本地文件'
}

# ============================================================
# 8. 文档解析与质量门（Phase 8）
# ============================================================

Write-Section '8. 文档解析与质量门'

$qualityEnabled = (Get-EnvValue 'QUALITY_ENABLED' 'true' $script:EnvValues).ToLower()
if ($qualityEnabled -eq 'false') {
    # Phase 18：质检是硬门，这个开关已失效 —— 不能装作没看见，也不能只说"已关闭"（那是错的）
    Write-Check 'PASS' '质量门恒定开启（Phase 18 起关不掉）' '.env 里设了 QUALITY_ENABLED=false，但该开关已失效：不合格的文件仍不会进入知识库' '想放行某一份文件，请在上传卡片上点「仍要入库」（档案里会记下人工放行）'
}
$minChars = Get-EnvValue 'QUALITY_MIN_CHARS' '50' $script:EnvValues
$minReadable = Get-EnvValue 'QUALITY_MIN_READABLE_RATIO' '0.60' $script:EnvValues
$minPerPage = Get-EnvValue 'QUALITY_MIN_CHARS_PER_PAGE' '100' $script:EnvValues
Write-Check 'PASS' '质量门已开启（不合格的文件不会进入知识库）' ('阈值：字符≥' + $minChars + '、可读率≥' + $minReadable + '、每页字符≥' + $minPerPage)

$profile = (Get-EnvValue 'QUALITY_PROFILE' 'standard' $script:EnvValues).ToLower()
$maxUnocr = Get-EnvValue 'QUALITY_MAX_UNOCR_IMAGES' '0' $script:EnvValues
$imageOcrSwitch = (Get-EnvValue 'OCR_IMAGE_OCR' 'true' $script:EnvValues).ToLower()

if ($profile -eq 'standard' -or $profile -eq 'strict' -or $profile -eq 'loose') {
    Write-Check 'PASS' ('质量门预设：' + $profile) '预设模式下列出的自定义阈值会被预设值覆盖（如需逐项调参请改为 custom）'
} elseif ($profile -eq 'custom') {
    Write-Check 'PASS' '质量门预设：custom（使用 .env 中的逐项阈值）'
} else {
    Write-Check 'WARN' ('QUALITY_PROFILE 取值无法识别：' + $profile) '只支持 standard / strict / loose / custom，其他值按 standard 处理' '改回 standard 或 custom'
}

if ($maxUnocr -eq '0') {
    Write-Check 'PASS' '内嵌图片策略：一张未识别图片即停下等你确认（最保守）' '网页上会显示「识别图片（N 张）」与「仍要入库」两个按钮'
} else {
    Write-Check 'WARN' ('内嵌图片策略：容忍 ' + $maxUnocr + ' 张未识别图片') '未识别的图片不会阻断入库（可能漏掉图片里的关键信息）' '若希望逐张确认，把 QUALITY_MAX_UNOCR_IMAGES 改回 0'
}

if ($imageOcrSwitch -eq 'false') {
    Write-Check 'WARN' '内嵌图片不纳入 OCR（OCR_IMAGE_OCR=false）' 'Word/PPT/PDF 里的图片会被忽略，只入库正文' '如需识别图片文字，把 OCR_IMAGE_OCR 改为 true'
} else {
    Write-Check 'PASS' '内嵌图片纳入 OCR：识别前会先停下等你确认（不会自动外发）'
}

$ocrMode = (Get-EnvValue 'OCR_MODE' 'off' $script:EnvValues).ToLower()
$ocrProvider = (Get-EnvValue 'OCR_PROVIDER' 'paddle' $script:EnvValues).ToLower()
$ocrToken = Get-EnvValue 'OCR_TOKEN' '' $script:EnvValues
$ocrBaseUrl = Get-EnvValue 'OCR_BASE_URL' 'https://paddleocr.aistudio-app.com/api/v2/ocr/jobs' $script:EnvValues
$ocrModel = Get-EnvValue 'OCR_MODEL' 'PaddleOCR-VL-1.6' $script:EnvValues

if ($ocrMode -eq 'off') {
    Write-Check 'PASS' 'OCR 未启用（OCR_MODE=off）：不会向任何外部服务发送文件' '扫描件与内嵌图片会被拦下并提示，需要时再开启'
} elseif ($ocrMode -eq 'manual' -or $ocrMode -eq 'auto') {
    $modeText = 'OCR_MODE=' + $ocrMode
    if ($ocrMode -eq 'manual') { $modeText = 'OCR_MODE=manual（网页上点按钮才识别）' }
    if ($ocrMode -eq 'auto') { $modeText = 'OCR_MODE=auto（检测到需要即自动识别，会消耗配额）' }

    if ($ocrProvider -eq 'local') {
        Write-Check 'PASS' ('本机 OCR 已选择：' + $modeText) '提供方 local：在本机识别、不向外部发送文件（需已安装 paddleocr；仅支持图片格式）'
    } elseif ($ocrProvider -eq 'none') {
        Write-Check 'PASS' ('OCR 已明确禁用（OCR_PROVIDER=none）' + '；' + $modeText) '即使填写了 Token 也不会外发文件'
    } elseif ($ocrProvider -eq 'stub') {
        Write-Check 'WARN' ('OCR 使用离线替身（OCR_PROVIDER=stub）' + '；' + $modeText) '替身返回的是构造文字，不能用于真实资料' '正式使用时把 OCR_PROVIDER 改为 paddle 或 local'
    } elseif ($ocrProvider -eq 'paddle') {
        if ($ocrToken) {
            $level = 'PASS'
            if ($ocrMode -eq 'auto') { $level = 'WARN' }
            Write-Check $level ('云端 OCR 已配置（会外发文件）：' + $modeText) ('地址：' + $ocrBaseUrl + '；模型：' + $ocrModel)
        } else {
            Write-Check 'WARN' ('云端 OCR 缺少令牌（' + $modeText + '）') '未填写 OCR_TOKEN，点「OCR 解析」会提示未启用' '在 .env 的 OCR_TOKEN 填入访问令牌后重启（或改回 OCR_MODE=off）'
        }
    } else {
        Write-Check 'WARN' ('OCR_PROVIDER 取值无法识别：' + $ocrProvider) '只支持 paddle / local / stub / none，其他值按禁用处理' '改回 paddle 或 local'
    }
} else {
    Write-Check 'WARN' ('OCR_MODE 取值无法识别：' + $ocrMode) '只支持 off / manual / auto，其他值会按 off 处理' '改回 off 或 manual'
}

$ingestAsync = (Get-EnvValue 'INGEST_ASYNC' 'true' $script:EnvValues).ToLower()
if ($ingestAsync -eq 'false') {
    Write-Check 'WARN' '上传为同步处理（INGEST_ASYNC=false）' '上传接口会等解析与质检结束才返回（排障用）' '日常使用建议改回 true'
} else {
    Write-Check 'PASS' '上传为后台处理（上传后立即返回，网页显示解析进度）'
}

# ============================================================
# 9. 磁盘空间
# ============================================================

Write-Section '9. 磁盘空间'

$drive = (Get-Item -LiteralPath $script:ProjectRoot).PSDrive.Name
$free = (Get-PSDrive -Name $drive).Free
$freeGB = [math]::Round($free / 1GB, 1)
if ($freeGB -lt 2) {
    Write-Check 'WARN' ('可用空间不足：' + $freeGB + ' GB') '知识库构建会产生较多文件' '清理磁盘后重试'
} else {
    Write-Check 'PASS' ('可用空间：' + $freeGB + ' GB（' + $drive + ' 盘）')
}

# ============================================================
# 汇总
# ============================================================

Write-Host ''
Write-Host ('  ' + ('=' * 58)) -ForegroundColor DarkGray

if ($script:FailCount -gt 0) {
    Write-Host ('  自检结果：' + $script:FailCount + ' 项失败、' + $script:WarnCount + ' 项注意 —— 请先处理失败项' ) -ForegroundColor Red
    exit 3
} elseif ($script:WarnCount -gt 0) {
    Write-Host ('  自检结果：' + $script:WarnCount + ' 项注意 —— 可以继续启动，但请留意上面的建议' ) -ForegroundColor Yellow
    exit 2
} else {
    Write-Host '  自检结果：全部通过' -ForegroundColor Green
    exit 0
}
