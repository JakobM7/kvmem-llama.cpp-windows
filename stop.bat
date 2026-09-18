@echo off
setlocal
cd /d "%~dp0"
set "SILENT=0"
if /I "%~1"=="/silent" set "SILENT=1"

if "%SILENT%"=="1" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-iq4.ps1" >nul 2>&1
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-open-webui.ps1" >nul 2>&1
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-dashboard.ps1" >nul 2>&1
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-iq4.ps1"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-open-webui.ps1"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-dashboard.ps1"
  echo.
  echo KVMem, Open WebUI und Dashboard wurden beendet, soweit sie diesem Projekt gehoeren.
  pause
)
endlocal
