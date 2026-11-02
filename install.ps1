# One-step installer for JARVIS on Windows 10/11. Safe to run again: it only fills in what is missing.
#   Right-click > Run with PowerShell, or from a terminal:  powershell -ExecutionPolicy Bypass -File .\install.ps1
#   Add -NoAutostart to skip the "start at login" question.
param([switch]$NoAutostart)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"

function Say($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "`nProblem: $msg" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }
function Run-Checked([string]$what, [scriptblock]$cmd) {
    & $cmd
    if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE). Scroll up to see why." }
}

# Find Python 3.12 or newer. "py -3.12" is the Python launcher; plain "python" must not be the Microsoft Store stub.
$candidates = @("py -3.13", "py -3.12", "python", "python3")
$pyExe = $null; $pyArgs = @()
foreach ($cand in $candidates) {
    $parts = @($cand -split ' ')
    $exe = $parts[0]
    $extra = @($parts | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        & $exe @extra -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $pyExe = $exe; $pyArgs = $extra; break }
    } catch { }
}
if (-not $pyExe) {
    Fail "Python 3.12 or newer is needed. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this installer again."
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
Say "Creating the private Python environment (.venv)"
if (-not (Test-Path $venvPy)) { Run-Checked "Creating the environment" { & $pyExe @pyArgs -m venv .venv } }
Run-Checked "Updating pip" { & $venvPy -m pip install --quiet --upgrade pip }
# Passwords and cookies live in state\ : make that folder readable by this Windows user only.
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "state") | Out-Null
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls (Join-Path $PSScriptRoot "state") /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Warning: could not restrict the state folder to your user." -ForegroundColor Yellow }
Say "Installing JARVIS and its dependencies (a minute or two)"
Run-Checked "Installing dependencies" { & $venvPy -m pip install --quiet -e . }
Say "Installing the browser JARVIS uses to sign in to POLITEMall and Teams"
Run-Checked "Installing the browser" { & $venvPy -m playwright install chromium }

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "`nNote: the Claude Code command (`"claude`") was not found. JARVIS needs it for chat, briefings and summaries." -ForegroundColor Yellow
    Write-Host "Install it from https://claude.com/claude-code, then run:  claude   (and sign in once)."
}

Say "Checking the install"
& $venvPy scripts\doctor.py
# (doctor reports problems itself; do not stop the installer on them)

if (-not $NoAutostart) {
    $ans = Read-Host "`nStart JARVIS automatically when you log in to this PC? [Y/n]"
    if ($ans -notmatch '^[Nn]') { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "scripts\install_startup.ps1") }
}

Say "Opening the control panel. The setup wizard will guide you from here."
$port = 8765
$up = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $async = $client.BeginConnect("127.0.0.1", $port, $null, $null)
    if ($async.AsyncWaitHandle.WaitOne(1000)) { $client.EndConnect($async); $up = $true }
    $client.Close()
} catch { }
if ($up) {
    Start-Process "http://127.0.0.1:$port"
} else {
    Write-Host "Keep this window open while you use JARVIS (or install auto-start). Close it to stop the panel."
    $env:JARVIS_UI_OPEN = "1"
    & $venvPy -m ui
}
