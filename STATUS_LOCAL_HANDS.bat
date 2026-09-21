@echo off
setlocal
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
set "PIDFILE=%DIR%\bridge.pid"

if exist "%PIDFILE%" set /p PID=<"%PIDFILE%"

if not defined PID goto stopped

tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL
if not errorlevel 1 goto health

:stopped
echo STOPPED
echo No running bridge process found.
exit /b 1

:health
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 3; if ($r.StatusCode -eq 200) { exit 0 } } catch {} ; exit 1" >NUL 2>&1
if errorlevel 1 (
  echo DEGRADED
  echo PID %PID% is alive but http://127.0.0.1:8787/health does not answer.
  exit /b 2
)

echo RUNNING
echo Bridge PID %PID% is alive and http://127.0.0.1:8787/health answers OK.
exit /b 0
