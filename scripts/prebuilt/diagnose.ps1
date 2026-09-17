[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'diagnose.py') @Arguments
exit $LASTEXITCODE
