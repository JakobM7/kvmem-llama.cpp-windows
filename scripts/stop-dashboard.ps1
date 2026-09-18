[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dashboardPaths = @(
    [IO.Path]::GetFullPath((Join-Path $Root 'scripts\dashboard.py')),
    [IO.Path]::GetFullPath((Join-Path $Root 'dashboard.py'))
) | ForEach-Object { $_.ToLowerInvariant() }
$dashboardProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and ($command = $_.CommandLine.ToLowerInvariant()) -and (
        ($dashboardPaths | Where-Object { $command.Contains($_) })
    )
}

foreach ($match in $dashboardProcesses) {
    if (-not (Get-Process -Id ([int]$match.ProcessId) -ErrorAction SilentlyContinue)) { continue }
    & taskkill.exe /PID ([int]$match.ProcessId) /T /F *> $null
    if ($LASTEXITCODE -ne 0) {
        if (Get-Process -Id ([int]$match.ProcessId) -ErrorAction SilentlyContinue) {
            throw "Dashboard-Prozessbaum konnte nicht beendet werden (taskkill $LASTEXITCODE)."
        }
    }
    Write-Host "KVMem-Dashboard beendet (PID $($match.ProcessId))."
}
if (-not $dashboardProcesses) { Write-Host 'KVMem-Dashboard läuft nicht.' }
