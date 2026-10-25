<#
  应用图标生成器（Phase 11）

  为什么自己画、而不是找个 .ico 放进仓库：
    1. 桌面快捷方式原来借用 .venv\Scripts\python.exe 的 Python 图标，一眼看不出这是什么应用；
    2. 本项目要求「离线可用、不引入新依赖」，所以用 Windows 自带的 System.Drawing 画图，
       再把各尺寸 PNG 打包成 ICO（Vista+ 支持 PNG 压缩条目），全程不联网、不加包。

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_icon.ps1
  参数（默认值已够用，一般不用传）：
    -Out     图标输出路径，默认 static\app.ico
    -Preview 预览图输出路径，默认 output\icon-preview.png

  设计（与网页 logo 同一套颜色）：
    底色  = 品牌渐变 135°  #3b82f6 → #8b5cf6
    主体  = 白色资料纸 + 文字行（个人资料）
    右下  = 放大镜（分析、检索）
    右上  = AI 星芒（AI 助手）
    小尺寸自动简化：≤24px 只留「渐变底 + 资料纸 + 两条粗线」，保证 16px 下依然认得出。

  产物：
    static\app.ico            16/20/24/32/40/48/64/128/256 九个尺寸
    output\icon-preview.png   各尺寸并排预览（人工确认外观用）
#>
[CmdletBinding()]
param(
    [string]$Out = '',
    [string]$Preview = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$root = Split-Path -Parent $PSScriptRoot
if (-not $Out) { $Out = Join-Path $root 'static\app.ico' }
if (-not $Preview) { $Preview = Join-Path $root 'output\icon-preview.png' }

$Sizes = @(256, 128, 64, 48, 40, 32, 24, 20, 16)
$C1 = [System.Drawing.Color]::FromArgb(255, 59, 130, 246)     # --brand 系：#3b82f6
$C2 = [System.Drawing.Color]::FromArgb(255, 139, 92, 246)     # 网页 logo 渐变尾部：#8b5cf6
$PaperColor = [System.Drawing.Color]::FromArgb(250, 255, 255, 255)
$LineColor = [System.Drawing.Color]::FromArgb(255, 175, 196, 246)
$GlassColor = [System.Drawing.Color]::FromArgb(70, 255, 255, 255)
$ShadeColor = [System.Drawing.Color]::FromArgb(120, 30, 41, 59)

function Get-RoundedPath {
    param([single]$X, [single]$Y, [single]$W, [single]$H, [single]$R)
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $d = $R * 2
    $path.AddArc($X, $Y, $d, $d, 180, 90)
    $path.AddArc($X + $W - $d, $Y, $d, $d, 270, 90)
    $path.AddArc($X + $W - $d, $Y + $H - $d, $d, $d, 0, 90)
    $path.AddArc($X, $Y + $H - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    return $path
}

function New-IconBitmap {
    param([int]$Size)

    $bmp = [System.Drawing.Bitmap]::new($Size, $Size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)
    # 全部按 256 的设计尺寸来画，最后整体缩放：同一套代码产出所有尺寸，形状比例永远一致
    $g.ScaleTransform([single]($Size / 256.0), [single]($Size / 256.0))

    # 1) 品牌渐变圆角方块底
    $bgPath = Get-RoundedPath 0 0 256 256 58
    $bgBrush = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.PointF]::new([single]0, [single]0),
        [System.Drawing.PointF]::new([single]256, [single]256), $C1, $C2)
    $g.FillPath($bgBrush, $bgPath)

    # 2) 白色资料纸
    $paperBrush = [System.Drawing.SolidBrush]::new($PaperColor)
    $paperPath = Get-RoundedPath 52 46 116 164 20
    $g.FillPath($paperBrush, $paperPath)

    $lineBrush = [System.Drawing.SolidBrush]::new($LineColor)
    if ($Size -ge 32) {
        # 3) 纸上的文字行（≥32px 才有意义，再小就糊成一团）
        foreach ($y in @(84, 116, 148)) {
            $path = Get-RoundedPath 76 $y 68 13 6
            $g.FillPath($lineBrush, $path)
            $path.Dispose()
        }
        $short = Get-RoundedPath 76 180 38 13 6
        $g.FillPath($lineBrush, $short)
        $short.Dispose()

        # 4) 放大镜（分析）：半透明玻璃 + 深色分离环 + 白色镜圈 + 手柄
        $glassBrush = [System.Drawing.SolidBrush]::new($GlassColor)
        $g.FillEllipse($glassBrush, 128, 124, 92, 92)
        $shadePen = [System.Drawing.Pen]::new($ShadeColor, [single]26)
        $g.DrawEllipse($shadePen, 137, 133, 74, 74)
        $ringPen = [System.Drawing.Pen]::new([System.Drawing.Color]::White, [single]18)
        $g.DrawEllipse($ringPen, 137, 133, 74, 74)
        $handlePen = [System.Drawing.Pen]::new([System.Drawing.Color]::White, [single]22)
        $handlePen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $handlePen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $g.DrawLine($handlePen, [single]212, [single]208, [single]242, [single]238)
        $glassBrush.Dispose(); $shadePen.Dispose(); $ringPen.Dispose(); $handlePen.Dispose()
    }
    else {
        # 小尺寸：只留两条粗线，够远看
        foreach ($y in @(104, 148)) {
            $path = Get-RoundedPath 80 $y 64 22 10
            $g.FillPath($lineBrush, $path)
            $path.Dispose()
        }
    }
    $lineBrush.Dispose()

    # 5) AI 星芒（≥48px 才画，小尺寸会变成一坨白点）
    if ($Size -ge 48) {
        $starBrush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
        $star = [System.Drawing.Drawing2D.GraphicsPath]::new()
        $star.AddPolygon(@(
            [System.Drawing.PointF]::new(198, 34), [System.Drawing.PointF]::new(205, 61),
            [System.Drawing.PointF]::new(232, 68), [System.Drawing.PointF]::new(205, 75),
            [System.Drawing.PointF]::new(198, 102), [System.Drawing.PointF]::new(191, 75),
            [System.Drawing.PointF]::new(164, 68), [System.Drawing.PointF]::new(191, 61)
        ))
        $g.FillPath($starBrush, $star)
        $star.Dispose(); $starBrush.Dispose()
    }


    $bgBrush.Dispose(); $paperBrush.Dispose(); $bgPath.Dispose(); $paperPath.Dispose()
    $g.Dispose()
    return $bmp
}

function Get-IconPng {
    param([int]$Size)
    $bmp = New-IconBitmap $Size
    $stream = [System.IO.MemoryStream]::new()
    $bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $stream.ToArray()
    $stream.Dispose(); $bmp.Dispose()
    return , $bytes          # 逗号：别让 PowerShell 把 byte[] 展开成 object[]，否则后面写 ICO 会退化成逐字节写
}

# ---------------- 生成各尺寸 PNG ----------------
Write-Host ''
Write-Host '绘制图标...'
$payloads = @()
foreach ($size in $Sizes) {
    $png = Get-IconPng $size
    $payloads += , @{ Size = $size; Bytes = $png }
    Write-Host ('  ' + $size.ToString().PadLeft(3) + 'px  ' + $png.Length.ToString().PadLeft(6) + ' bytes')
}

# ---------------- 打包 ICO（ICONDIR + 目录项 + PNG 数据）----------------
$stream = [System.IO.MemoryStream]::new()
$writer = [System.IO.BinaryWriter]::new($stream)
$writer.Write([uint16]0)                       # reserved
$writer.Write([uint16]1)                       # type = icon
$writer.Write([uint16]$payloads.Count)         # count

$offset = 6 + 16 * $payloads.Count
foreach ($item in $payloads) {
    $size = [int]$item.Size
    $len = ([byte[]]$item.Bytes).Length
    $dim = if ($size -ge 256) { 0 } else { $size }   # 256 在目录里用 0 表示
    $writer.Write([byte]$dim)
    $writer.Write([byte]$dim)
    $writer.Write([byte]0)                     # 调色板数
    $writer.Write([byte]0)                     # 保留
    $writer.Write([uint16]1)                   # 色彩平面
    $writer.Write([uint16]32)                  # 位深
    $writer.Write([uint32]$len)
    $writer.Write([uint32]$offset)
    $offset += $len
}
foreach ($item in $payloads) {
    $bytes = [byte[]]$item.Bytes
    $stream.Write($bytes, 0, $bytes.Length)
}
$writer.Flush()
$outDir = Split-Path -Parent $Out
if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
[System.IO.File]::WriteAllBytes($Out, $stream.ToArray())
$writer.Dispose(); $stream.Dispose()

# ---------------- 预览图（各尺寸并排，方便肉眼确认）----------------
$previewSizes = @(128, 64, 48, 32, 24, 16)
$gap = 18
$pad = 20
$sum = 0
foreach ($s in $previewSizes) { $sum += $s }
$canvasW = $sum + $gap * ($previewSizes.Count - 1) + $pad * 2
$canvasH = 128 + $pad * 2
$canvas = [System.Drawing.Bitmap]::new([int]$canvasW, [int]$canvasH, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$graphics = [System.Drawing.Graphics]::FromImage($canvas)
$graphics.Clear([System.Drawing.Color]::FromArgb(255, 244, 246, 251))
$checker = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(255, 228, 232, 240))
for ($y = 0; $y -lt $canvasH; $y += 8) {
    for ($x = 0; $x -lt $canvasW; $x += 8) {
        if (((($x / 8) + ($y / 8)) % 2) -eq 0) { $graphics.FillRectangle($checker, $x, $y, 8, 8) }
    }
}
$cursor = $pad
foreach ($s in $previewSizes) {
    $bmp = New-IconBitmap $s
    $graphics.DrawImage($bmp, [System.Drawing.Rectangle]::new([int]$cursor, [int]$pad, [int]$s, [int]$s))
    $bmp.Dispose()
    $cursor += $s + $gap
}
$graphics.Dispose()
$checker.Dispose()
$previewDir = Split-Path -Parent $Preview
if ($previewDir -and -not (Test-Path $previewDir)) { New-Item -ItemType Directory -Path $previewDir | Out-Null }
$canvas.Save($Preview, [System.Drawing.Imaging.ImageFormat]::Png)
$canvas.Dispose()

Write-Host ''
Write-Host '图标已生成：'
Write-Host ('  ' + $Out + '  (' + (Get-Item $Out).Length + ' bytes)')
Write-Host ('  ' + $Preview + '  (预览图，可直接打开看)')
Write-Host ''
Write-Host '让桌面 / 开始菜单快捷方式用上新图标：'
Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_shortcuts.ps1'
Write-Host '（或双击 appctl.vbs shortcut；若资源管理器仍显示旧图标，执行 ie4uinit.exe -show 刷新缓存）'
Write-Host ''
exit 0
