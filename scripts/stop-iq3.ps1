[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $env:BUILD_DIR) { $env:BUILD_DIR = Join-Path $Root 'build-windows' }
& python (Join-Path $PSScriptRoot 'start-server.py') --recipe iq3 `
    --default-model (Join-Path $Root 'models/unused.gguf') --default-mmproj (Join-Path $Root 'models/unused.gguf') `
    --default-vision-device cpu --kv q8_0 --budget 1 --reserve 1 --stop @Arguments
exit $LASTEXITCODE
