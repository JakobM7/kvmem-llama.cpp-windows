[CmdletBinding()]
param(
    [switch]$NoOpen,
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$Root = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'docker-compose.openwebui.yml')) {
    (Resolve-Path $PSScriptRoot).Path
} else {
    (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
$Compose = Join-Path $Root 'docker-compose.openwebui.yml'
$docker = (Get-Command docker.exe -ErrorAction SilentlyContinue).Source
if (-not $docker) { throw 'Docker Desktop is required for Open WebUI.' }
if (-not (Test-Path -LiteralPath $Compose)) { throw "Missing compose file: $Compose" }

function Test-DockerEngine {
    $probe = Start-Process -FilePath $docker -ArgumentList @('info', '--format', '{{.ServerVersion}}') -WindowStyle Hidden -PassThru
    if (-not $probe.WaitForExit(3000)) {
        try { $probe.Kill() } catch { }
        return $false
    }
    return $probe.ExitCode -eq 0
}

if (-not (Test-DockerEngine)) {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (Test-Path -LiteralPath $desktop) {
        Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
    } else {
        throw 'Docker Desktop is required for Open WebUI.'
    }
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(30, $TimeoutSeconds))
    while (-not (Test-DockerEngine) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds 2
    }
}
if (-not (Test-DockerEngine)) {
    throw 'Docker Desktop did not become ready before the timeout.'
}

& $docker compose -f $Compose up -d
if ($LASTEXITCODE) { throw 'Open WebUI container could not be started.' }

$port = 3000
if ($env:OPEN_WEBUI_PORT) { $port = [int]$env:OPEN_WEBUI_PORT }
$url = "http://127.0.0.1:$port/"
$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(30, $TimeoutSeconds))
$ready = $false
while (-not $ready -and [DateTime]::UtcNow -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        $ready = $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ready) { throw "Open WebUI did not answer at $url before the timeout." }
if (-not $NoOpen) { Start-Process $url }
Write-Host "Open WebUI is ready at $url"
