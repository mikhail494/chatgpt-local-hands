@echo off
setlocal
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
set "PIDFILE=%DIR%\bridge.pid"

rem --- detect an already running bridge, prevent double launch ---
if exist "%PIDFILE%" set /p PID=<"%PIDFILE%"
if defined PID (
  tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL
  if not errorlevel 1 (
    echo Bridge already running - PID %PID%.
    exit /b 0
  )
)

rem --- launch detached ---
:launch
start "LocalHandsBridge" /min cmd /c python "%DIR%\bridge.py" >> "%DIR%\bridge.out" 2>&1

rem --- wait for health ---
set /a N=0
:wait
set /a N+=1
if %N% gtr 40 (
  echo Bridge failed to start. Check %DIR%\bridge.log and %DIR%\bridge.out
  exit /b 1
)
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {} ; exit 1" >NUL 2>&1
if errorlevel 1 (
  timeout /t 1 /nobreak >NUL
  goto wait
)

echo Local Hands bridge is running on http://127.0.0.1:8787
exit /b 0
