param([string]$TaskName = "profile-ai-M6-backup")
schtasks.exe /Delete /TN $TaskName /F
exit $LASTEXITCODE
