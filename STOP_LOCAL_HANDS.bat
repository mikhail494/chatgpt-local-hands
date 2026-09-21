@echo off
setlocal
set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
set "PIDFILE=%DIR%\bridge.pid"

if not exist "%PIDFILE%" (
  echo No bridge.pid found. Bridge is not running.
  exit /b 0
)

set /p PID=<"%PIDFILE%"

tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL
if errorlevel 1 (
  echo Stale PID file - PID %PID% not running. Removing it.
  del "%PIDFILE%" >NUL 2>&1
  exit /b 0
)

taskkill /PID %PID% /T /F >NUL 2>&1
if errorlevel 1 (
  echo Failed to stop PID %PID%.
  exit /b 1
)

echo Stopped bridge PID %PID%.
del "%PIDFILE%" >NUL 2>&1
exit /b 0
