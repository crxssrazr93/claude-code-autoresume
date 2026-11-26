# SessionStart hook for Windows: make sure the detector task exists and is enabled.
# Silent when all is well; prints one JSON systemMessage when something is wrong.
$task = "claude-autoresume-detect"
schtasks /Query /TN $task *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Output '{"systemMessage": "Auto-resume is OFF: the scheduled task is missing. Re-run the auto-resume install.ps1"}'
    exit 0
}
schtasks /Change /TN $task /ENABLE *> $null
exit 0
