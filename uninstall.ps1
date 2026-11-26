# Remove the detector task and cancel every pending auto-resume task on Windows.
#
#   .\uninstall.ps1 [-Dir DIR] [-Purge]
#
# DIR is the install directory (default $env:AUTORESUME_DIR, then
# %USERPROFILE%\.claude\auto-resume). It is deleted only when install.ps1 marked it.
# With -Purge the logs and state in %LOCALAPPDATA%\auto-resume are deleted too.
param(
    [string]$Dir = $(if ($env:AUTORESUME_DIR) { $env:AUTORESUME_DIR } else { Join-Path $env:USERPROFILE ".claude\auto-resume" }),
    [switch]$Purge
)
$ErrorActionPreference = "Continue"

$names = @("claude-autoresume-detect")
$rows = schtasks /Query /FO CSV /NH 2>$null | ConvertFrom-Csv -Header Name, Next, Status
foreach ($r in $rows) {
    $n = $r.Name.TrimStart("\")
    if ($n -like "claude-autoresume-*" -and $names -notcontains $n) { $names += $n }
}
foreach ($n in $names) {
    schtasks /End /TN $n 2>$null | Out-Null
    schtasks /Delete /TN $n /F 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "cancelled $n" }
}

if ($Purge) {
    $Data = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "auto-resume" } else { Join-Path $env:USERPROFILE "AppData\Local\auto-resume" }
    Remove-Item -Recurse -Force $Data -ErrorAction SilentlyContinue
    Write-Host "purged $Data"
}
if (Test-Path (Join-Path $Dir ".autoresume-install")) {
    Remove-Item -Recurse -Force $Dir
    Write-Host "removed $Dir"
}
Write-Host "uninstalled"
