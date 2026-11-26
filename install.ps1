# Install the auto-resume detector on Windows (beta) as a Task Scheduler task.
#
#   .\install.ps1 [-Dir DIR] [-SkipTests]
#
# The scripts are copied to DIR (default $env:AUTORESUME_DIR, then
# %USERPROFILE%\.claude\auto-resume) and a task named claude-autoresume-detect runs
# the detector every 2 minutes as you, hidden, without admin rights. Your Claude Code
# settings.json is never edited. Needs Python 3.11 or newer on PATH.
param(
    [string]$Dir = $(if ($env:AUTORESUME_DIR) { $env:AUTORESUME_DIR } else { Join-Path $env:USERPROFILE ".claude\auto-resume" }),
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Task = "claude-autoresume-detect"
$Files = @("detect.py", "fire.py", "lib.py", "peer.py", "reach.py", "platform_base.py",
           "platform_linux.py", "platform_macos.py", "platform_windows.py",
           "ensure.ps1", "uninstall.ps1")

$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    Write-Error "python not found on PATH. Install Python 3.11 or newer from python.org or the Microsoft Store."
}
$version = ((& $Python --version) -replace "Python ", "").Trim()
$parts = $version.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
    Write-Error "Python $version is too old. This tool needs 3.11 or newer."
}

# psutil lists processes. tzdata gives Python the time zone database that Windows lacks.
& $Python -m pip install --user --quiet psutil tzdata
if ($LASTEXITCODE -ne 0) { Write-Error "could not install psutil and tzdata with pip" }

if (-not $SkipTests) {
    & $Python -m unittest discover -s (Join-Path $Src "tests") -p "test_*.py" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "tests failed, not installing (run: python -m unittest discover -s tests)"
    }
}

if ([System.IO.Path]::GetFullPath($Dir).TrimEnd("\") -ne [System.IO.Path]::GetFullPath($Src).TrimEnd("\")) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    foreach ($f in $Files) { Copy-Item (Join-Path $Src $f) $Dir -Force }
}
New-Item -ItemType File -Force -Path (Join-Path $Dir ".autoresume-install") | Out-Null
$Data = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "auto-resume" } else { Join-Path $env:USERPROFILE "AppData\Local\auto-resume" }
New-Item -ItemType Directory -Force -Path $Data | Out-Null

$xml = Join-Path ([System.IO.Path]::GetTempPath()) "claude-autoresume-detect.xml"
& $Python (Join-Path $Dir "platform_windows.py") detect-task-xml --python $Python --dir $Dir --out $xml
if ($LASTEXITCODE -ne 0) { Write-Error "could not build the task definition" }
schtasks /Create /TN $Task /XML $xml /F
$created = $LASTEXITCODE
Remove-Item $xml -Force -ErrorAction SilentlyContinue
if ($created -ne 0) { Write-Error "schtasks could not create the task" }

Write-Host "Installed to $Dir (Windows, beta). Task: $Task"
Write-Host @"

Optional, merge these into %USERPROFILE%\.claude\settings.json yourself (nothing was changed):

1. Let Claude Code continue by itself at the limit reset (it pairs with this tool):

  "autoContinueAtUsageLimit": true

To change the delay or the message, set user environment variables, then sign out and in:

  setx AUTORESUME_DELAY 300
  setx AUTORESUME_MESSAGE "your text"

Verify with:
  python -I $Dir\reach.py
  python $Dir\detect.py status
  python $Dir\detect.py scan --dry-run
"@
