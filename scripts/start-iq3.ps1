[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $env:BUILD_DIR) { $env:BUILD_DIR = Join-Path $Root 'build-windows' }
& python (Join-Path $PSScriptRoot 'start-server.py') `
    --recipe iq3 `
    --default-model (Join-Path $Root 'models/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF/Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf') `
    --default-mmproj (Join-Path $Root 'models/unsloth/Qwen3.8-27B-GGUF/mmproj-Q8_0.gguf') `
    --default-vision-device cpu --kv q8_0 --budget 36864 --reserve 16384 @Arguments
exit $LASTEXITCODE
