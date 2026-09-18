[CmdletBinding()]
param(
    [switch]$NoOpen,
    [int]$TimeoutSeconds = 180,
    [int]$Port = $(if ($env:OPEN_WEBUI_PORT) { [int]$env:OPEN_WEBUI_PORT } else { 3000 })
)

$ErrorActionPreference = 'Stop'
$Root = if ((Split-Path $PSScriptRoot -Leaf) -ieq 'scripts') {
    (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
    (Resolve-Path $PSScriptRoot).Path
}
$Venv = Join-Path $Root '.open-webui-venv'
$LegacyVenv = Join-Path $PSScriptRoot '.open-webui-venv'
if ((-not (Test-Path -LiteralPath $Venv)) -and (Test-Path -LiteralPath $LegacyVenv)) { $Venv = $LegacyVenv }
$Data = Join-Path $Root '.open-webui-data'
$LogDir = Join-Path $Root 'logs'
$PidFile = Join-Path $Root '.open-webui.pid'
$WebUi = Join-Path $Venv 'Scripts/open-webui.exe'
$VenvPython = Join-Path $Venv 'Scripts/python.exe'
$Url = "http://127.0.0.1:$Port/"

if (-not (1 -le $Port -and $Port -le 65535)) { throw 'OPEN_WEBUI_PORT must be between 1 and 65535.' }
New-Item -ItemType Directory -Force $Data, $LogDir | Out-Null

function Test-Ready {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch { return $false }
}

function Get-UserCount([string]$Database) {
    if (-not (Test-Path -LiteralPath $Database)) { return 0 }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        throw "Open WebUI Python fehlt: $VenvPython"
    }
    $code = @'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
try:
    try:
        rows = db.execute('select email from [user]').fetchall()
        value = sum(1 for (email,) in rows if str(email or '').lower() != 'admin@localhost')
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):
            raise
        value = 0
    print(value)
finally:
    db.close()
'@
    $output = $code | & $VenvPython - $Database
    if ($LASTEXITCODE -ne 0) { throw "Open WebUI-Datenbank konnte nicht geprüft werden: $($output -join ' ')" }
    $value = ($output | Select-Object -Last 1).ToString().Trim()
    if ($value -notmatch '^\d+$') { throw "Ungültige Benutzeranzahl in Open WebUI-Datenbank: $value" }
    return [int]$value
}

function Backup-ExistingUsers {
    $database = Join-Path $Data 'webui.db'
    if ((Get-UserCount $database) -eq 0) { return }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = Join-Path $Root ".open-webui-data.login-backup-$stamp"
    $suffix = 1
    while (Test-Path -LiteralPath $backup) {
        $backup = Join-Path $Root ".open-webui-data.login-backup-$stamp-$suffix"
        $suffix++
    }
    Move-Item -LiteralPath $Data -Destination $backup
    New-Item -ItemType Directory -Force $Data | Out-Null
    Write-Warning "Open WebUI-Benutzerdaten wurden reversibel verschoben nach $backup"
}

function Invoke-Python([string[]]$Arguments) {
    & $script:PythonExe @script:PythonPrefix @Arguments
    if ($LASTEXITCODE) { throw "Python command failed ($LASTEXITCODE): $($Arguments -join ' ')" }
}

if (-not (Test-Path -LiteralPath $WebUi)) {
    $script:PythonExe = $null
    $script:PythonPrefix = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @('3.11', '3.12')) {
            try { $probe = & $py.Source "-$version" --version 2>&1 } catch { $probe = $null }
            if ($LASTEXITCODE -eq 0) {
                $script:PythonExe = $py.Source
                $script:PythonPrefix = @("-$version")
                break
            }
        }
    }
    if (-not $script:PythonExe) {
        foreach ($candidate in @('python.exe', 'python3.exe')) {
            $command = Get-Command $candidate -ErrorAction SilentlyContinue
            if (-not $command) { continue }
            $probe = & $command.Source --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $probe -match 'Python 3\.(11|12)\.') {
                $script:PythonExe = $command.Source
                break
            }
        }
    }
    if (-not $script:PythonExe) {
        $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source venv --python 3.11 $Venv
            if ($LASTEXITCODE -eq 0) { $script:PythonExe = $uv.Source; $script:PythonPrefix = @('run', '--python', $VenvPython, 'python') }
        }
    }
    if (-not $script:PythonExe) {
        throw 'Open WebUI braucht Python 3.11 oder 3.12. Installiere eine dieser Versionen und starte erneut.'
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) { Invoke-Python @('-m', 'venv', $Venv) }
    $script:PythonExe = $VenvPython
    $script:PythonPrefix = @()
    Invoke-Python @('-m', 'pip', 'install', '--upgrade', 'pip')
    Invoke-Python @('-m', 'pip', 'install', 'open-webui')
}

if (-not (Test-Path -LiteralPath $WebUi)) {
    throw "Open WebUI konnte nicht installiert werden: $WebUi fehlt."
}
if (Test-Ready) {
    if (-not $NoOpen) { Start-Process $Url }
    Write-Host "Open WebUI läuft bereits unter $Url"
    exit 0
}

$activeProcess = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath -ieq $WebUi) -or
        ($_.CommandLine -and $_.CommandLine -like "*$WebUi*")
    } |
    Select-Object -First 1
if ($activeProcess) {
    throw "Open WebUI läuft bereits (PID $($activeProcess.ProcessId)); Datenbank bleibt unverändert."
}
Backup-ExistingUsers

$env:DATA_DIR = $Data
$env:OPENAI_API_BASE_URL = if ($env:KVMEM_OPENAI_API_BASE_URL) { $env:KVMEM_OPENAI_API_BASE_URL } else { 'http://127.0.0.1:18200/v1' }
$env:OPENAI_API_BASE_URLS = $env:OPENAI_API_BASE_URL
$env:OPENAI_API_KEYS = if ($env:KVMEM_OPENAI_API_KEY) { $env:KVMEM_OPENAI_API_KEY } else { 'sk-kvmem-local' }
$env:OPENAI_API_KEY = $env:OPENAI_API_KEYS
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:WEBUI_AUTH = 'false'
$env:ENABLE_OLLAMA_API = 'false'
$env:ENABLE_PERSISTENT_CONFIG = 'false'
# A single 27B local slot should spend its time on the user's chat.  Disable
# automatic title/tag/follow-up/query jobs that otherwise enqueue extra model
# requests with large tool prompts behind the visible answer.
$env:ENABLE_TITLE_GENERATION = 'false'
$env:ENABLE_TAGS_GENERATION = 'false'
$env:ENABLE_FOLLOW_UP_GENERATION = 'false'
$env:ENABLE_AUTOCOMPLETE_GENERATION = 'false'
$env:ENABLE_SEARCH_QUERY_GENERATION = 'false'
$env:ENABLE_RETRIEVAL_QUERY_GENERATION = 'false'
# Open WebUI forwards these model defaults to llama.cpp.  Only an explicit
# dashboard reasoning profile enables them; the normal "none" profile stays
# unchanged and does not add reasoning work to chat requests.
$reasoningEffort = $env:KVMEM_OPENWEBUI_THINKING
if ($reasoningEffort -in @('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')) {
    $env:DEFAULT_MODEL_PARAMS = '{"chat_template_kwargs":{"enable_thinking":true,"reasoning_effort":"' + $reasoningEffort + '"},"reasoning_budget_tokens":-1}'
} elseif ($reasoningEffort -eq 'default') {
    $env:DEFAULT_MODEL_PARAMS = '{"chat_template_kwargs":{"enable_thinking":true},"reasoning_budget_tokens":-1}'
} elseif ($reasoningEffort -in @('256', '1024', '4096')) {
    $env:DEFAULT_MODEL_PARAMS = '{"chat_template_kwargs":{"enable_thinking":true},"reasoning_budget_tokens":' + $reasoningEffort + '}'
} else {
    Remove-Item Env:DEFAULT_MODEL_PARAMS -ErrorAction SilentlyContinue
}
$defaultModel = if ([string]::IsNullOrWhiteSpace($env:KVMEM_OPENWEBUI_DEFAULT_MODEL)) {
    $preferred = Get-ChildItem -Path @((Join-Path $Root 'models'), 'I:\models\LLM_Collection\qwen3.8') -Recurse -Filter '*.gguf' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '(?i)qwen3\.8.*ud-iq4.*mtp|ud-iq4.*mtp.*qwen3\.8' } |
        Sort-Object FullName |
        Select-Object -First 1
    if ($preferred) { $preferred.Name } else { 'Qwen3.8-27B-Uncensored-IQ4_XS.gguf' }
} else {
    $env:KVMEM_OPENWEBUI_DEFAULT_MODEL.Trim()
}
$env:DEFAULT_MODELS = $defaultModel
$env:DEFAULT_PINNED_MODELS = $defaultModel
$env:WEBUI_URL = $Url.TrimEnd('/')
$stdout = Join-Path $LogDir 'open-webui.stdout.log'
$stderr = Join-Path $LogDir 'open-webui.stderr.log'
$process = Start-Process -FilePath $WebUi -ArgumentList @('serve', '--host', '127.0.0.1', '--port', $Port) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$process.Id | Set-Content -LiteralPath $PidFile -Encoding ascii

$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(30, $TimeoutSeconds))
while (-not (Test-Ready) -and [DateTime]::UtcNow -lt $deadline) {
    if ($process.HasExited) {
        $errorText = if (Test-Path $stderr) { Get-Content $stderr -Raw } else { '' }
        throw "Open WebUI wurde beendet (Exit $($process.ExitCode)). $errorText"
    }
    Start-Sleep -Seconds 2
}
if (-not (Test-Ready)) { throw "Open WebUI antwortet nicht unter $Url. Details: $stderr" }
if (-not $NoOpen) { Start-Process $Url }
Write-Host "Open WebUI ist bereit unter $Url"
