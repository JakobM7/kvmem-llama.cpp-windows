[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $env:BUILD_DIR) { $env:BUILD_DIR = Join-Path $Root 'build-windows' }
& python (Join-Path $PSScriptRoot 'start-server.py') `
    --recipe iq4 `
    --default-model (Join-Path $Root 'models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ4_XS-mtp-q4_0.gguf') `
    --default-mmproj (Join-Path $Root 'models/unsloth/Qwen3.8-27B-GGUF/mmproj-BF16.gguf') `
    --default-vision-device cpu --kv q5_0 --budget 32768 --reserve 12288 @Arguments
exit $LASTEXITCODE
