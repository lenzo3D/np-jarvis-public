# Runs the JARVIS control panel and starts it again a few seconds after it quits (used by the login task that
# scripts\install_startup.ps1 creates). Output goes to state\logs\panel.out.log and panel.log.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repo
$py = Join-Path $repo ".venv\Scripts\python.exe"
$logs = Join-Path $repo "state\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$env:PYTHONUTF8 = "1"
$env:JARVIS_RESTART_BY_PARENT = "1"   # tells the panel to just exit after an update; this loop starts the new one
while ($true) {
    foreach ($n in "panel.out.log", "panel.log") {
        $f = Join-Path $logs $n
        if (Test-Path $f) { Move-Item -Force -Path $f -Destination "$f.1" }
    }
    Start-Process -FilePath $py -ArgumentList @("-m", "ui") -WorkingDirectory $repo -WindowStyle Hidden -Wait `
        -RedirectStandardOutput (Join-Path $logs "panel.out.log") -RedirectStandardError (Join-Path $logs "panel.log")
    Start-Sleep -Seconds 5
}
