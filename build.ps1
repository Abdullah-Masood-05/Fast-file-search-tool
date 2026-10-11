<#
.SYNOPSIS
  Local Windows build of Fast File Search Pro (wrapper around build.py).

.DESCRIPTION
  All Nuitka flags live in build.py (shared with CI and the other platforms); this script
  only adds a compiler fallback chain for local Windows machines (first success wins):
    1. MSVC    (Visual Studio / Build Tools with the C++ x64 toolset, --msvc=latest)
    2. MinGW64 (C:\msys64\mingw64\bin gcc, --mingw64; Nuitka 4.1.3 rejects MinGW on
       Python >= 3.13, so this stage runs in an isolated uv-managed Python 3.12 env)
    3. Docker  (LAST RESORT, only if 1 and 2 both failed; needs the Windows-containers
       engine, see Invoke-DockerBuild below). Container and image are removed afterwards.

  Output (default, standalone): dist\FastFileSearchPro\FastFileSearchPro.exe
  Output (-Mode onefile)      : dist\FastFileSearchPro.exe
  -Installer additionally builds dist\Fast-File-Search-Pro-v<version>-Windows-x64-Setup.exe
  with Inno Setup (packaging\windows\setup.iss) from the standalone folder.

.EXAMPLE
  .\build.ps1                      # standalone folder build, auto compiler
  .\build.ps1 -Mode onefile        # single-file exe
  .\build.ps1 -Compiler mingw      # force one compiler (no fallback)
  .\build.ps1 -Installer           # standalone build + Inno Setup installer
#>
[CmdletBinding()]
param(
    [ValidateSet('standalone', 'onefile')] [string]$Mode = 'standalone',
    [ValidateSet('auto', 'msvc', 'mingw', 'docker')] [string]$Compiler = 'auto',
    [string]$OutDir  = 'dist',
    [int]$Jobs = [Environment]::ProcessorCount,
    [string]$MingwBin = 'C:\msys64\mingw64\bin',
    [switch]$KeepBuildDir,
    [switch]$Installer
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$OutAbs  = if ([IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $Root $OutDir }
$Version = (& uv run --no-project python (Join-Path $Root 'build.py') --print-version | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) { throw 'Could not read the version via build.py (is uv installed?)' }

function Get-BuildPyArgs([string]$CompilerName, [string]$OutputDir = $OutAbs) {
    $a = @('build.py', '--mode', $Mode, '--compiler', $CompilerName, '--output-dir', $OutputDir, '--jobs', $Jobs)
    if ($KeepBuildDir) { $a += '--keep-build-dir' }
    return $a
}

function Invoke-BuildPy([string]$Label, [string]$CompilerName, [string[]]$Runner = @('run', 'python')) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "`n=== Nuitka build [$Label] mode=$Mode version=$Version ===" -ForegroundColor Cyan
    $buildArgs = Get-BuildPyArgs $CompilerName
    Write-Host ("uv " + ($Runner -join ' ') + " " + ($buildArgs -join ' '))
    & uv @Runner @buildArgs
    $code = $LASTEXITCODE
    Write-Host ("[{0}] exit code {1}, {2:n0}s" -f $Label, $code, $sw.Elapsed.TotalSeconds)
    return ($code -eq 0)
}

# ------------------------------------------------------------------ compilers
function Invoke-Msvc {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) { throw 'vswhere.exe not found' }
    $inst = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $inst) { throw 'No Visual Studio install with the C++ x64 toolset found' }
    Invoke-BuildPy 'MSVC' 'msvc'
}

function Invoke-Mingw {
    if (-not (Test-Path (Join-Path $MingwBin 'gcc.exe'))) { throw "gcc.exe not found in $MingwBin" }
    $env:PATH = "$MingwBin;$env:PATH"
    # Nuitka 4.1.3 refuses --mingw64 on Python >= 3.13, so run it from an isolated
    # Python 3.12 environment (uv downloads 3.12 + packages once, repo .venv untouched).
    $runner = @('run', '--no-project', '--isolated', '--python', '3.12',
                '--with', 'nuitka==4.1.3', '--with', 'zstandard', '--with', 'PyQt6>=6.0.0', '--with', 'watchdog>=3.0.0', 'python')
    Invoke-BuildPy 'MinGW64' 'mingw' $runner
}

<#
 Docker fallback - realistic notes:
  * Nuitka cannot cross-compile, so a Linux container would only yield a Linux binary.
    A Windows .exe can only come from a WINDOWS container (Docker Desktop switched to
    "Windows containers", base image must match/ be Hyper-V compatible with the host kernel).
  * On Windows 11 client, Windows containers run with Hyper-V isolation only.
  * The image is large (servercore ~ 5 GB + VS Build Tools ~ 3-6 GB) and slow to build.
  * Everything is created with a unique name and removed afterwards (container --rm, image rmi,
    temp context dir deleted), even on failure.
#>
function Invoke-DockerBuild {
    $docker = (Get-Command docker -ErrorAction SilentlyContinue)
    if (-not $docker) { throw 'docker CLI not found' }
    $dockerCli = Join-Path $env:ProgramFiles 'Docker\Docker\DockerCli.exe'
    $os = (& docker info --format '{{.OSType}}' 2>$null)
    if ($os -ne 'windows') {
        if (Test-Path $dockerCli) {
            Write-Host 'Switching Docker Desktop to Windows containers...'
            & $dockerCli -SwitchWindowsEngine | Out-Null
            for ($i = 0; $i -lt 30 -and (& docker info --format '{{.OSType}}' 2>$null) -ne 'windows'; $i++) { Start-Sleep 5 }
        }
        $os = (& docker info --format '{{.OSType}}' 2>$null)
        if ($os -ne 'windows') { throw 'Docker daemon is not available in Windows-container mode; cannot produce a Windows exe' }
    }

    $tag  = "ffs-nuitka-build:" + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $cont = "ffs-nuitka-" + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $ctx  = Join-Path $env:TEMP ("ffs_docker_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $ctx | Out-Null
    $ok = $false
    try {
        $pyVer = '3.12'   # Python version installed in the image (via uv)
        @"
# escape=``
FROM mcr.microsoft.com/windows/servercore:ltsc2025
SHELL ["powershell","-NoProfile","-Command","`$ErrorActionPreference='Stop';"]
RUN Invoke-WebRequest https://aka.ms/vs/17/release/vs_buildtools.exe -OutFile C:\vs_bt.exe; ``
    Start-Process C:\vs_bt.exe -Wait -ArgumentList '--quiet','--wait','--norestart','--nocache','--add','Microsoft.VisualStudio.Workload.VCTools','--includeRecommended'; ``
    Remove-Item C:\vs_bt.exe
RUN Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
ENV PATH="C:\Users\ContainerAdministrator\.local\bin;C:\Windows\system32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0"
RUN uv python install $pyVer
WORKDIR C:\src
"@ | Set-Content (Join-Path $ctx 'Dockerfile') -Encoding ascii

        & docker build -t $tag $ctx
        if ($LASTEXITCODE -ne 0) { throw 'docker build failed' }

        # Keep the container's venv out of the bind-mounted tree (the host .venv stays untouched).
        $inner = "`$env:UV_PROJECT_ENVIRONMENT='C:\venv'; uv sync --python $pyVer; uv run python " + ((Get-BuildPyArgs 'msvc' 'C:\out') -join ' ')
        $cmd = "cmd /c `"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat`" && powershell -NoProfile -Command `"$inner`""
        New-Item -ItemType Directory -Force -Path $OutAbs | Out-Null
        & docker run --name $cont --isolation=hyperv -v "${Root}:C:\src" -v "${OutAbs}:C:\out" $tag cmd /c $cmd
        if ($LASTEXITCODE -ne 0) { throw 'docker run (nuitka build) failed' }
        $ok = $true
    }
    finally {
        & docker rm -f $cont 2>$null | Out-Null
        & docker rmi -f $tag 2>$null | Out-Null
        Remove-Item $ctx -Recurse -Force -ErrorAction SilentlyContinue
    }
    return $ok
}

function Invoke-Installer {
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe", "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw 'Inno Setup (ISCC.exe) not found; install it from https://jrsoftware.org/isdl.php' }
    $src = Join-Path $OutAbs 'FastFileSearchPro'
    if (-not (Test-Path (Join-Path $src 'FastFileSearchPro.exe'))) { throw "Standalone build not found in $src (the installer needs -Mode standalone)" }
    & $iscc /Q "/DAppVersion=$Version" "/DSourceDir=$src" "/DOutputDir=$OutAbs" (Join-Path $Root 'packaging\windows\setup.iss')
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }
    Write-Host "OK: $OutAbs\Fast-File-Search-Pro-v$Version-Windows-x64-Setup.exe" -ForegroundColor Green
}

# ----------------------------------------------------------------------- main
$order = switch ($Compiler) {
    'auto'   { @('msvc', 'mingw', 'docker') }
    default  { @($Compiler) }
}

$success = $false
foreach ($c in $order) {
    try {
        $r = switch ($c) {
            'msvc'   { Invoke-Msvc }
            'mingw'  { Invoke-Mingw }
            'docker' { Invoke-DockerBuild }
        }
        if ($r -is [array]) { $r = $r[-1] }
        if ($r) { $success = $true; break }
        Write-Warning "$c build failed; trying next option (if any)."
    } catch {
        Write-Warning "$c unavailable/failed: $($_.Exception.Message)"
    }
}

if (-not $success) { Write-Error 'All build methods failed.'; exit 1 }
if ($Installer) { Invoke-Installer }
exit 0
