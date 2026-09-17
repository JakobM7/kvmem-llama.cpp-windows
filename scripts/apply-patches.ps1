[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
& $Python (Join-Path $PSScriptRoot 'apply-patches.py')
if ($LASTEXITCODE) { exit $LASTEXITCODE }
