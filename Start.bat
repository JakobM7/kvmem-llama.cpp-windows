@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>&1
if not errorlevel 1 (
  if exist "%~dp0scripts\dashboard.py" (start "KVMem Dashboard" /b pythonw "%~dp0scripts\dashboard.py") else (start "KVMem Dashboard" /b pythonw "%~dp0dashboard.py")
  exit /b 0
)
where python >nul 2>&1
if errorlevel 1 (
  echo Python 3 is required. Install Python and run Start.bat again.
  pause
  exit /b 1
)
if exist "%~dp0scripts\dashboard.py" (python "%~dp0scripts\dashboard.py") else (python "%~dp0dashboard.py")
endlocal
