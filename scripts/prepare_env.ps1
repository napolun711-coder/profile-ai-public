# Compatibility entry: install_env.bat now forwards to setup.ps1.
param([switch]$NoStart)
& (Join-Path $PSScriptRoot 'setup.ps1') -NoStart:$NoStart
exit $LASTEXITCODE
