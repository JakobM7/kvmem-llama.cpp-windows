[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'docker-compose.openwebui.yml')) {
    (Resolve-Path $PSScriptRoot).Path
} else {
    (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}
$Compose = Join-Path $Root 'docker-compose.openwebui.yml'
$docker = (Get-Command docker.exe -ErrorAction SilentlyContinue).Source
if (-not $docker) { throw 'Docker Desktop is required for Open WebUI.' }
& $docker compose -f $Compose down
exit $LASTEXITCODE
