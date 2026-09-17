[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot '../package-windows.ps1') @Arguments
exit $LASTEXITCODE
