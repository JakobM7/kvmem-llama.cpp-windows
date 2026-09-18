[CmdletBinding()]
param(
    [string]$BuildDir = $(if ($env:BUILD_DIR) { $env:BUILD_DIR } else { Join-Path (Split-Path $PSScriptRoot -Parent) 'build-windows' }),
    [string]$Output = $(Join-Path (Split-Path $PSScriptRoot -Parent) 'dist/kvmem-llama-windows.zip'),
    [ValidateSet('Release','RelWithDebInfo','Debug')][string]$Configuration = $(if ($env:CMAKE_BUILD_TYPE) { $env:CMAKE_BUILD_TYPE } else { 'Release' }),
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($Build) { & (Join-Path $PSScriptRoot 'build-windows.ps1') -BuildDir $BuildDir -Configuration $Configuration; if ($LASTEXITCODE) { exit $LASTEXITCODE } }
$cudaRoot = $env:CUDA_PATH
if (-not $cudaRoot) {
    $cudaInstallation = Get-ChildItem 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA' -Directory -Filter 'v*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Where-Object { Test-Path (Join-Path $_.FullName 'bin/nvcc.exe') } |
        Select-Object -First 1
    if ($cudaInstallation) { $cudaRoot = $cudaInstallation.FullName }
}
if (-not $cudaRoot) { Write-Warning 'CUDA_PATH not set and no standard CUDA installation was found; CUDA runtime DLLs will not be bundled.' }
$bin = Join-Path $BuildDir 'bin'
$binSource = Join-Path $bin $Configuration
if (-not (Test-Path $binSource)) { $binSource = $bin }
if (-not (Test-Path $binSource)) { throw "missing build output: $bin" }
$stage = Join-Path ([IO.Path]::GetTempPath()) ('kvmem-windows-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    New-Item -ItemType Directory -Force (Join-Path $stage 'bin') | Out-Null
    New-Item -ItemType Directory -Force (Join-Path $stage 'scripts') | Out-Null
    Get-ChildItem $binSource -File | Where-Object { $_.Extension -in '.exe', '.dll' } |
        Copy-Item -Destination (Join-Path $stage 'bin') -Force
    Copy-Item (Join-Path $Root 'scripts/start-server.py') (Join-Path $stage 'start-server.py')
    Copy-Item (Join-Path $Root 'scripts/start-iq3.ps1') (Join-Path $stage 'start-iq3.ps1')
    Copy-Item (Join-Path $Root 'scripts/start-iq4.ps1') (Join-Path $stage 'start-iq4.ps1')
    Copy-Item (Join-Path $Root 'scripts/stop-iq3.ps1') (Join-Path $stage 'stop-iq3.ps1')
    Copy-Item (Join-Path $Root 'scripts/stop-iq4.ps1') (Join-Path $stage 'stop-iq4.ps1')
    Copy-Item (Join-Path $Root 'scripts/start-open-webui.ps1') (Join-Path $stage 'start-open-webui.ps1')
    Copy-Item (Join-Path $Root 'scripts/stop-open-webui.ps1') (Join-Path $stage 'stop-open-webui.ps1')
    Copy-Item (Join-Path $Root 'docker-compose.openwebui.yml') (Join-Path $stage 'docker-compose.openwebui.yml')
    Copy-Item (Join-Path $Root 'scripts/dashboard.py') (Join-Path $stage 'dashboard.py')
    Copy-Item (Join-Path $Root 'scripts/dashboard.py') (Join-Path $stage 'scripts/dashboard.py')
    Copy-Item (Join-Path $Root 'scripts/start-server.py') (Join-Path $stage 'scripts/start-server.py')
    Copy-Item (Join-Path $Root 'Start.bat') (Join-Path $stage 'Start.bat')
    $uiCandidates = @()
    if ($env:KVMEM_UI_DIR) { $uiCandidates += $env:KVMEM_UI_DIR }
    $uiCandidates += (Join-Path $Root 'build/share/kvmem/ui'), (Join-Path $BuildDir 'share/kvmem/ui')
    $uiSource = $uiCandidates | Where-Object { Test-Path (Join-Path $_ 'index.html') } | Select-Object -First 1
    if ($uiSource) {
        $uiTarget = Join-Path $stage 'share/kvmem/ui'
        New-Item -ItemType Directory -Force $uiTarget | Out-Null
        Copy-Item (Join-Path $uiSource '*') $uiTarget -Recurse -Force
    }
    Copy-Item (Join-Path $Root 'VERSION') (Join-Path $stage 'VERSION')
    New-Item -ItemType Directory -Force (Join-Path $stage 'licenses') | Out-Null
    Copy-Item (Join-Path $Root 'llama.cpp/LICENSE') (Join-Path $stage 'licenses/llama.cpp-MIT.txt')
    if (Test-Path (Join-Path $Root 'README.md')) { Copy-Item (Join-Path $Root 'README.md') (Join-Path $stage 'README.md') }
    if ($cudaRoot -and (Test-Path (Join-Path $cudaRoot 'EULA.txt'))) {
        Copy-Item (Join-Path $cudaRoot 'EULA.txt') (Join-Path $stage 'licenses/NVIDIA-CUDA-EULA.txt')
    }
if ($cudaRoot) {
        foreach ($pattern in @('cudart64*.dll', 'cublas64*.dll', 'cublasLt64*.dll', 'nvrtc64*.dll', 'nvJitLink_*.dll')) {
            foreach ($cudaBin in @((Join-Path $cudaRoot 'bin'), (Join-Path $cudaRoot 'bin/x64'))) {
                Get-ChildItem $cudaBin -Filter $pattern -ErrorAction SilentlyContinue |
                    Copy-Item -Destination (Join-Path $stage 'bin') -Force
            }
        }
    }
    function Read-CMakeVersion([string]$FileName, [string]$Variable) {
        $file = Get-ChildItem -Path $BuildDir -Recurse -Filter $FileName -File -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $file) { return $null }
        $pattern = '(?m)^\s*set\(' + [regex]::Escape($Variable) + '\s+"?([^"\s)]+)'
        $content = Get-Content $file.FullName -Raw
        if ($content -match $pattern) { return $Matches[1] }
        return $null
    }
    $cudaVersion = Read-CMakeVersion 'CMakeCUDACompiler.cmake' 'CMAKE_CUDA_COMPILER_VERSION'
    if (-not $cudaVersion -and $cudaRoot -and (Test-Path (Join-Path $cudaRoot 'bin/nvcc.exe'))) {
        $nvcc = (& (Join-Path $cudaRoot 'bin/nvcc.exe') '--version' 2>&1 | Out-String)
        if ($nvcc -match 'V([0-9]+(?:\.[0-9]+)+)') { $cudaVersion = $Matches[1] }
        elseif ($nvcc -match 'release\s+([0-9]+(?:\.[0-9]+)+)') { $cudaVersion = $Matches[1] }
    }
    if (-not $cudaVersion) { $cudaVersion = 'unknown' }
    $msvcVersion = Read-CMakeVersion 'CMakeCCompiler.cmake' 'CMAKE_C_COMPILER_VERSION'
    if (-not $msvcVersion) { $msvcVersion = 'unknown' }
    $manifest = [ordered]@{
        version = (Get-Content (Join-Path $Root 'VERSION') -Raw).Trim()
        platform = 'windows-x64'
        build = 'local'
        configuration = $Configuration
        cuda_arch = $(if ($env:CMAKE_CUDA_ARCHITECTURES) { $env:CMAKE_CUDA_ARCHITECTURES } else { '120a-real' })
        cuda_toolkit_version = $cudaVersion
        msvc_version = $msvcVersion
        generated_utc = [DateTime]::UtcNow.ToString('o')
    }
    $manifest | ConvertTo-Json | Set-Content (Join-Path $stage 'build-manifest.json') -Encoding utf8
    $checksums = foreach ($file in Get-ChildItem $stage -File -Recurse) {
        $relative = $file.FullName.Substring($stage.Length + 1).Replace('\', '/')
        "$(Get-FileHash $file.FullName -Algorithm SHA256 | Select-Object -ExpandProperty Hash)  $relative"
    }
    $checksums | Set-Content (Join-Path $stage 'SHA256SUMS') -Encoding ascii
    New-Item -ItemType Directory -Force (Split-Path $Output -Parent) | Out-Null
    if (Test-Path $Output) {
        if (Test-Path $Output -PathType Container) { throw "output path is a directory: $Output" }
        Remove-Item -LiteralPath $Output -Force
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $stage, $Output, [System.IO.Compression.CompressionLevel]::Optimal, $false)
    Write-Host "created $Output"
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
