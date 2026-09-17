[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'download-test-models.py') @Arguments
exit $LASTEXITCODE
