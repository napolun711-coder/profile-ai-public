<#
  桌面 / 开始菜单快捷方式（Phase 10）

  为什么用 .lnk 而不是打包成 exe：
    1. 不需要管理员权限（只写当前用户的桌面与开始菜单）；
    2. Windows 原生支持，安装步骤就是这份可读的脚本，出问题好排查；
    3. 不引入打包工具链（PyInstaller 类方案会带来杀软误报与体积膨胀）。
  为什么指向 wscript + appctl.vbs：双击等于「无窗口执行一条 appctl 命令」，
  与 install_env.bat 装好之后的第一次启动体验完全一致。

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_shortcuts.ps1
  参数：
    -NoStop       不创建「停止」快捷方式（默认创建，方便随时手动停服）
    -NoStartMenu  不写开始菜单（默认写）
  本脚本可重复运行：每次都覆盖修复，不会产生重复项。
#>
[CmdletBinding()]
param(
    [switch]$NoStop,
    [switch]$NoStartMenu
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot                        # 项目根目录（scripts 的上一级）
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$launcher = Join-Path $root 'appctl.vbs'
$python = Join-Path $root '.venv\Scripts\python.exe'
$appIcon = Join-Path $root 'static\app.ico'

if (-not (Test-Path $launcher)) {
    throw "找不到启动器：$launcher"
}

# 图标（Phase 11）：用应用自己的 static\app.ico（scripts\make_icon.ps1 生成）。
# 以前借用 .venv 里 Python 的图标，混在任务栏/桌面上根本认不出这是什么应用。
# 图标不存在时先自动生成一次；实在生成不出来才回落到 Python 图标 → 系统图标。
if (-not (Test-Path $appIcon)) {
    $maker = Join-Path $PSScriptRoot 'make_icon.ps1'
    if (Test-Path $maker) {
        try {
            & $maker | Out-Null
        }
        catch {
            Write-Host ('图标生成失败，改用其它图标：' + $_.Exception.Message)
        }
    }
}
if (Test-Path $appIcon) {
    $icon = "$appIcon,0"
} elseif (Test-Path $python) {
    $icon = "$python,0"
} else {
    $icon = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',13'
}

function New-AppShortcut {
    param(
        [string]$Path,
        [string]$Arguments,
        [string]$Description
    )
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $wscript
    $link.Arguments = '"' + $launcher + '" ' + $Arguments
    $link.WorkingDirectory = $root
    $link.Description = $Description
    $link.IconLocation = $icon
    $link.Save()
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}

$created = New-Object System.Collections.ArrayList

$desktop = [Environment]::GetFolderPath('Desktop')
$desktopStart = Join-Path $desktop '本地知识库助手.lnk'
New-AppShortcut -Path $desktopStart -Arguments 'start' -Description '启动本地知识库助手（无窗口；失败时才弹提示）'
[void]$created.Add($desktopStart)

if (-not $NoStop) {
    $desktopStop = Join-Path $desktop '停止本地知识库助手.lnk'
    New-AppShortcut -Path $desktopStop -Arguments 'stop' -Description '停止本地知识库助手拉起的全部服务'
    [void]$created.Add($desktopStop)
}

if (-not $NoStartMenu) {
    $menuDir = Join-Path ([Environment]::GetFolderPath('Programs')) '本地知识库助手'
    if (-not (Test-Path $menuDir)) {
        New-Item -ItemType Directory -Path $menuDir | Out-Null
    }
    $menuStart = Join-Path $menuDir '本地知识库助手.lnk'
    New-AppShortcut -Path $menuStart -Arguments 'start' -Description '启动本地知识库助手'
    [void]$created.Add($menuStart)
    if (-not $NoStop) {
        $menuStop = Join-Path $menuDir '停止本地知识库助手.lnk'
        New-AppShortcut -Path $menuStop -Arguments 'stop' -Description '停止本地知识库助手拉起的全部服务'
        [void]$created.Add($menuStop)
    }
}

Write-Host ''
Write-Host '快捷方式已创建 / 已修复：'
foreach ($item in $created) {
    Write-Host ('  ' + $item)
}
Write-Host ''
Write-Host '用法：双击「本地知识库助手」= 启动（会自动打开网页）；'
Write-Host '      关闭聊天窗口 25 秒后，本应用拉起的服务会自动停止。'
Write-Host ''
exit 0
