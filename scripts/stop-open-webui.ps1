[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = if ((Split-Path $PSScriptRoot -Leaf) -ieq 'scripts') {
    (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
    (Resolve-Path $PSScriptRoot).Path
}
$PidFile = Join-Path $Root '.open-webui.pid'
function Stop-OpenWebUiTree([int]$processId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (-not $process -or -not $process.CommandLine) { return $false }
    $commandLine = $process.CommandLine
    if ($commandLine -notmatch '(?i)\\\.open-webui-venv\\Scripts\\open-webui\.exe(?:\s|"|$)') { return $false }
    # open-webui.exe is a launcher; the listening server is commonly a
    # Python grandchild. Stop the validated process tree, not just the
    # launcher, otherwise port 3000 remains occupied after Stop.bat.
    & taskkill.exe /PID $processId /T /F *> $null
    if ($LASTEXITCODE -ne 0) {
        if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
            throw "Open WebUI-Prozessbaum konnte nicht beendet werden (taskkill $LASTEXITCODE)."
        }
    }
    Write-Host "Open WebUI-Prozessbaum beendet (PID $processId)."
    return $true
}

$stopped = $false
if (Test-Path -LiteralPath $PidFile) {
    try { $processId = [int](Get-Content -LiteralPath $PidFile -Raw).Trim() } catch { $processId = 0 }
    if ($processId -gt 0) { $stopped = Stop-OpenWebUiTree $processId }
    if (-not $stopped -and $processId -gt 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        Write-Warning "PID $processId gehört nicht eindeutig zu diesem Open-WebUI-Prozess; er bleibt unverändert."
    }
}

# Recover an orphaned Python child when the launcher/PID file disappeared.
if (-not $stopped) {
    $candidate = [IO.Path]::GetFullPath((Join-Path $Root '.open-webui-venv\Scripts\open-webui.exe'))
    $legacy = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '.open-webui-venv\Scripts\open-webui.exe'))
    $webUiProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and (($_.CommandLine.ToLowerInvariant().Contains($candidate.ToLowerInvariant())) -or
            ($_.CommandLine.ToLowerInvariant().Contains($legacy.ToLowerInvariant())))
    }
    foreach ($match in $webUiProcesses) { $stopped = (Stop-OpenWebUiTree ([int]$match.ProcessId)) -or $stopped }
}

if (-not $stopped) { Write-Host 'Open WebUI läuft nicht.' }
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
