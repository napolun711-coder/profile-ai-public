#Requires -Version 5.1
<#
  一键放行局域网访问（平板 / 手机打不开网页时用）

  为什么需要管理员：Windows 防火墙的入站规则只有管理员能改。
  为什么按**端口**放行而不是按 python.exe：本项目有时用 .venv 的 python，有时用 uv 的
  解释器（还可能是 junction 路径），按程序路径的白名单很容易"看着有规则、实际没匹配上"。
  TCP 8100 的入站规则与解释器路径无关，最稳。

  用法（任选一种）：
    · 双击项目根目录的 allow_lan.bat（会弹 UAC）
    · 管理员 PowerShell：
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\allow_lan.ps1

  只做一件事：新增 / 更新一条入站放行规则；不修改 .env，不启动/停止任何服务。
  退出码：0 = 已放行；2 = 没有管理员权限；3 = 放行了，但服务没在监听（需先启动）。
#>

[CmdletBinding()]
param(
    [int]$Port = 0,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$script:Root = Split-Path -Parent $PSScriptRoot

function Read-EnvValue {
    param([string]$Key, [string]$Default = '')
    $path = Join-Path $script:Root '.env'
    if (-not (Test-Path -LiteralPath $path)) { return $Default }
    foreach ($raw in (Get-Content -LiteralPath $path -Encoding UTF8)) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        $idx = $line.IndexOf('=')
        if ($idx -lt 1) { continue }
        if ($line.Substring(0, $idx).Trim() -ne $Key) { continue }
        $value = $line.Substring($idx + 1).Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1); $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        return $value
    }
    return $Default
}

function Show-LanUrls {
    param([int]$ListenPort)
    $ips = @(Get-NetIPConfiguration -ErrorAction SilentlyContinue |
        Where-Object { $_.IPv4DefaultGateway -ne $null } |
        ForEach-Object { $_.IPv4Address.IPAddress })
    if ($ips.Count -eq 0) {
        Write-Host '没有取到局域网 IPv4：请用 ipconfig 查看当前联网网卡，再用 http://电脑IP:端口/web 访问。'
        return
    }
    foreach ($ip in $ips) {
        Write-Host ('  平板 / 手机访问：http://' + $ip + ':' + $ListenPort + '/web')
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Port -le 0) { $Port = [int](Read-EnvValue 'APP_PORT' ([string]8100)) }
$appHost = Read-EnvValue 'APP_HOST' '127.0.0.1'

Write-Host ''
Write-Host '本地知识库助手 · 局域网访问放行'
Write-Host '------------------------------------------------------------'
Write-Host ('应用端口：' + $Port)
Write-Host ('APP_HOST：' + $appHost)
Write-Host ''
Write-Host '安全提醒：本应用没有登录认证。放行 8100 后，同一局域网内的设备都能打开它。' -ForegroundColor Yellow
Write-Host '只在你信任当前网络（家里 / 自己的路由器）时使用；不要在公司 / 公共 WiFi 上放行。'

if ($appHost -ne '0.0.0.0' -and $appHost -ne '') {
    Write-Host ''
    Write-Host ('注意：.env 里 APP_HOST=' + $appHost + '，服务只监听本机，平板一定连不上。') -ForegroundColor Yellow
    Write-Host '请先把 .env 的 APP_HOST 改成 0.0.0.0，保存后双击 restart.bat。'
}

if (-not $isAdmin) {
    Write-Host ''
    Write-Host '需要管理员权限才能添加入站规则。' -ForegroundColor Red
    Write-Host '请双击项目根目录的 allow_lan.bat（会弹 UAC），或在管理员 PowerShell 里运行：'
    Write-Host ('  powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    if (-not $NoPause) { Read-Host '按回车关闭' | Out-Null }
    exit 2
}

$ruleName = 'profile-ai 局域网网页 TCP ' + $Port
$existing = @(Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) | Select-Object -First 1
if ($existing) {
    Set-NetFirewallRule -Name $existing.Name -Enabled True -Action Allow -Profile Any -Direction Inbound
    $existing | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
    Write-Host ''
    Write-Host ('已更新现有规则：' + $ruleName)
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -Profile Any `
        -Description 'Profile AI Assistant: allow LAN clients to open the local web UI.' | Out-Null
    Write-Host ''
    Write-Host ('已新增规则：' + $ruleName)
}

$listen = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
Write-Host ''
if ($listen.Count -gt 0) {
    Write-Host ('服务正在监听 ' + $Port + ' 端口（' + $listen[0].LocalAddress + ':' + $listen[0].LocalPort + '）') -ForegroundColor Green
} else {
    Write-Host ('服务当前没有在监听 ' + $Port + '：先在电脑上启动「本地知识库助手」，平板才能打开。') -ForegroundColor Yellow
}
Write-Host ''
Write-Host '请让平板 / 手机连接与电脑相同的 WiFi（不能是访客网络 / 手机流量），然后访问：'
Show-LanUrls -ListenPort $Port
Write-Host ''
Write-Host '仍然打不开时依次检查：'
Write-Host '  1) 电脑上的聊天页面是否开着（关掉 25 秒后服务会自动停止）'
Write-Host '  2) 路由器是否开启了「AP 隔离 / 访客网络隔离」'
Write-Host '  3) 平板输入的是否是上面列出的那个 IP（不要用 127.0.0.1 或 192.168.56.x 这类虚拟网卡）'

if (-not $NoPause) { Read-Host '按回车关闭' | Out-Null }
if ($listen.Count -eq 0) { exit 3 }
exit 0
