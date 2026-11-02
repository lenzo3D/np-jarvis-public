@echo off
rem Double-click to open the JARVIS control panel. The first time, it installs everything for you.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set URL=http://127.0.0.1:8765
if not exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
  goto :done
)
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $a = $c.BeginConnect('127.0.0.1', 8765, $null, $null); if ($a.AsyncWaitHandle.WaitOne(1000)) { $c.EndConnect($a); exit 0 } else { exit 1 } } catch { exit 1 }"
if errorlevel 1 goto :start
start "" "%URL%"
goto :done
:start
echo Starting the JARVIS control panel. Keep this window open, or run scripts\install_startup.ps1 once
echo so it starts by itself at login.
set JARVIS_UI_OPEN=1
".venv\Scripts\python.exe" -m ui
:done
endlocal
