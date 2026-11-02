# Make the JARVIS control panel start by itself when you log in to this PC (Windows Task Scheduler), and start
# again a few seconds after it ever quits.
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1            install / update
#   powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove    uninstall
# Then bookmark http://127.0.0.1:8765
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$taskName = "JARVIS control panel"
$repo = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_panel.ps1"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Host "Removed. The panel will no longer start at login."
    exit 0
}

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Missing $py. Run install.ps1 first."; exit 1 }

$arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $repo
$me = "$env:USERDOMAIN\$env:USERNAME"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Starts the JARVIS control panel at login" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Installed. JARVIS panel: http://127.0.0.1:8765  (log: $repo\state\logs\panel.log)"
