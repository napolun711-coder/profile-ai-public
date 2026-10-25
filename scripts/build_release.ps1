#Requires -Version 5.1
param([string]$Version = "")
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Version) {
    $match = Select-String -LiteralPath (Join-Path $root 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $match) { throw 'Cannot read version from pyproject.toml' }
    $Version = $match.Matches[0].Groups[1].Value
}
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Path $dist -Force | Out-Null
$name = 'profile-ai-' + $Version + '-source.zip'
$zip = Join-Path $dist $name
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
$dirty = git -C $root status --porcelain
if ($dirty) { Write-Host '[WARN] Working tree is not clean; archive is built from HEAD only.' -ForegroundColor Yellow }
git -C $root archive --format=zip --prefix=('profile-ai-' + $Version + '/') -o $zip HEAD
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
$sum = Join-Path $dist 'SHA256SUMS.txt'
Set-Content -LiteralPath $sum -Value ($hash + '  ' + $name) -Encoding ASCII
Write-Host ('Release: ' + $zip) -ForegroundColor Green
Write-Host ('SHA256:  ' + $hash)
exit 0
