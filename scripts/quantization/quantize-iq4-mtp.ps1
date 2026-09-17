[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
& python (Join-Path $PSScriptRoot 'quantize-iq4-mtp.py') @Arguments
exit $LASTEXITCODE
