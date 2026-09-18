[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = if ((Split-Path $PSScriptRoot -Leaf) -ieq 'scripts') {
    (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
    (Resolve-Path $PSScriptRoot).Path
}
$PidFile = Join-Path $Root '.open-webui.pid'
if (-not (Test-Path -LiteralPath $PidFile)) {
    Write-Host 'Open WebUI läuft nicht (keine lokale PID-Datei).'
    exit 0
}
$processId = [int](Get-Content -LiteralPath $PidFile -Raw).Trim()
$process = Get-Process -Id $processId -ErrorAction SilentlyContinue
if ($process) {
    $commandLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue).CommandLine
    if ($commandLine -and $commandLine -like '*\.open-webui-venv\Scripts\open-webui.exe*') {
        $process | Stop-Process -Force
        Write-Host "Open WebUI beendet (PID $processId)."
    } else {
        Write-Warning "PID $processId gehört nicht eindeutig zu diesem Open-WebUI-Prozess; er bleibt unverändert."
    }
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
