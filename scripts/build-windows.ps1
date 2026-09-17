[CmdletBinding()]
param(
    [ValidateSet('Release','RelWithDebInfo','Debug')][string]$Configuration = $(if ($env:CMAKE_BUILD_TYPE) { $env:CMAKE_BUILD_TYPE } else { 'Release' }),
    [string]$BuildDir = $(if ($env:BUILD_DIR) { $env:BUILD_DIR } else { Join-Path (Split-Path $PSScriptRoot -Parent) 'build-windows' }),
    [string]$CudaArch = $(if ($env:CMAKE_CUDA_ARCHITECTURES) { $env:CMAKE_CUDA_ARCHITECTURES } else { '120a-real' }),
    [int]$Jobs = $(if ($env:CMAKE_BUILD_PARALLEL_LEVEL) { [int]$env:CMAKE_BUILD_PARALLEL_LEVEL } else { 4 }),
    [switch]$HostOnly,
    [switch]$SkipPatch
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$CMake = if ($env:CMAKE) { $env:CMAKE } else { 'cmake' }
$Ninja = Get-Command ninja -ErrorAction SilentlyContinue
if (-not $SkipPatch) { & (Join-Path $PSScriptRoot 'apply-patches.ps1') }
$generator = if ($env:CMAKE_GENERATOR) {
    $env:CMAKE_GENERATOR
} elseif ($Ninja) {
    'Ninja'
} else {
    # This is the installed VS 2026 generator on the target machine.
    'Visual Studio 18 2026'
}
$llama = if ($HostOnly) { 'OFF' } else { 'ON' }
$configure = @('-S', $Root, '-B', $BuildDir, '-G', $generator, "-DCMAKE_BUILD_TYPE=$Configuration", "-DKVMEM_BUILD_LLAMA=$llama", '-DBUILD_SHARED_LIBS=OFF')
if (-not $HostOnly) {
    # Respect an explicit toolkit/compiler. Otherwise find the newest CUDA
    # installation in NVIDIA's standard Windows directory.
    $cudaCompiler = $null
    if ($env:CUDACXX) {
        $cudaCompiler = $env:CUDACXX
    } elseif (Get-Command nvcc -ErrorAction SilentlyContinue) {
        $cudaCompiler = (Get-Command nvcc).Source
    } elseif ($env:CUDA_PATH) {
        $candidate = Join-Path $env:CUDA_PATH 'bin/nvcc.exe'
        if (Test-Path $candidate) { $cudaCompiler = $candidate }
    } else {
        $cudaRoot = Get-ChildItem 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA' -Directory -Filter 'v*' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            Where-Object { Test-Path (Join-Path $_.FullName 'bin/nvcc.exe') } |
            Select-Object -First 1
        if ($cudaRoot) {
            $env:CUDA_PATH = $cudaRoot.FullName
            $env:CUDACXX = Join-Path $cudaRoot.FullName 'bin/nvcc.exe'
            $cudaCompiler = $env:CUDACXX
        }
    }
    if ($env:CUDA_PATH) {
        $cudaLeaf = Split-Path -Leaf $env:CUDA_PATH.TrimEnd('\', '/')
        if ($cudaLeaf -match '^v([0-9]+)\.([0-9]+)$') {
            $versionedName = "CUDA_PATH_V$($Matches[1])_$($Matches[2])"
            if (-not (Get-Item "Env:$versionedName" -ErrorAction SilentlyContinue)) {
                Set-Item "Env:$versionedName" $env:CUDA_PATH
            }
        }
    }
    if ($cudaCompiler) {
        $configure += @("-DCMAKE_CUDA_COMPILER=$cudaCompiler")
    }
}
if ($generator -like 'Visual Studio*') {
    $configure += @('-A', 'x64')
    $buildRoot = [IO.Path]::GetFullPath($BuildDir)
    $binRoot = Join-Path $buildRoot 'bin'
    $configName = $Configuration.ToUpperInvariant()
    # VS is multi-config; keep the launcher/package layout identical to Ninja.
    $configure += @(
        "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_$configName=$binRoot",
        "-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_$configName=$binRoot",
        "-DCMAKE_ARCHIVE_OUTPUT_DIRECTORY_$configName=$binRoot"
    )
}
if (-not $HostOnly) {
    $configure += @("-DCMAKE_CUDA_ARCHITECTURES=$CudaArch")
    $configure += @('-DGGML_CUDA=ON', '-DGGML_CUDA_FA_ALL_QUANTS=ON', '-DLLAMA_KVMEM=ON', "-DLLAMA_KVMEM_ROOT=$Root")
}
& $CMake @configure
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& $CMake '--build' $BuildDir '--config' $Configuration '--parallel' $Jobs
exit $LASTEXITCODE
