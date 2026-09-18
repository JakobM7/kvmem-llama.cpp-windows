@echo off
setlocal
cd /d "%~dp0"
title KVMem Dashboard
set "KVMEM_TERMINAL_OWNER=1"
where python >nul 2>&1
if errorlevel 1 (
  where pythonw >nul 2>&1
  if not errorlevel 1 echo python.exe was not found. Install Python 3 with python.exe; pythonw.exe cannot keep a controllable terminal open.
  if errorlevel 1 echo Python 3 is required. Install Python and run Start.bat again.
  pause
  exit /b 1
)
if exist "%~dp0scripts\dashboard.py" (
  python "%~dp0scripts\dashboard.py"
) else (
  python "%~dp0dashboard.py"
)
set "DASHBOARD_EXIT=%ERRORLEVEL%"
call "%~dp0stop.bat" /silent
echo.
echo KVMem Dashboard beendet. Dieses Fenster kann jetzt geschlossen werden.
pause
endlocal & exit /b %DASHBOARD_EXIT%
