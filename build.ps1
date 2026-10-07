<#
.SYNOPSIS
  Build Fast File Search Pro into a Windows executable with Nuitka.

.DESCRIPTION
  Compiler fallback order (first success wins):
    1. MSVC   (VS Build Tools 2022, --msvc=latest, vcvars64 environment imported)
    2. MinGW64 (C:\msys64\mingw64\bin gcc, --mingw64; Nuitka 4.1.3 rejects MinGW on
       Python >= 3.13, so this stage runs in an isolated uv-managed Python 3.12 env)
    3. Docker (LAST RESORT, only if 1 and 2 both failed; needs the Windows-containers
       engine, see Invoke-DockerBuild below). Container and image are removed afterwards.

  Output (default, standalone): dist\FastFileSearchPro\FastFileSearchPro.exe
  Output (-Mode onefile)      : dist\FastFileSearchPro.exe

.EXAMPLE
  .\build.ps1                      # standalone folder build, auto compiler
  .\build.ps1 -Mode onefile        # single-file exe
  .\build.ps1 -Compiler mingw      # force one compiler (no fallback)
#>
[CmdletBinding()]
param(
    [ValidateSet('standalone', 'onefile')] [string]$Mode = 'standalone',
    [ValidateSet('auto', 'msvc', 'mingw', 'docker')] [string]$Compiler = 'auto',
    [string]$Entry   = 'main.py',
    [string]$Icon    = 'files.ico',
    [string]$Name    = 'FastFileSearchPro',
    [string]$OutDir  = 'dist',
    [string]$SrcPath = 'src',          # added to PYTHONPATH so Nuitka can find the package
    [string]$Package = 'fast_file_search',   # package to force-include ('' to skip)
    [int]$Jobs = [Environment]::ProcessorCount,
    [string]$MingwBin = 'C:\msys64\mingw64\bin',
    [switch]$KeepBuildDir
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Resolve-Rel([string]$p) { if ([IO.Path]::IsPathRooted($p)) { $p } else { Join-Path $Root $p } }
$EntryAbs  = Resolve-Rel $Entry
$IconAbs   = Resolve-Rel $Icon
$OutAbs    = Resolve-Rel $OutDir
$SrcAbs    = Resolve-Rel $SrcPath
$ExeName   = "$Name.exe"

# ---------------------------------------------------------------- nuitka args
function Get-NuitkaArgs([string]$CompilerFlag) {
    $a = @(
        '-m', 'nuitka',
        '--standalone',
        '--enable-plugin=pyqt6',
        '--windows-console-mode=disable',
        "--windows-icon-from-ico=$IconAbs",
        "--include-data-files=$IconAbs=files.ico",
        "--output-dir=$OutAbs",
        "--output-filename=$ExeName",
        "--company-name=Abdullah Masood",
        "--product-name=Fast File Search Pro",
        "--file-version=1.2.0.0",
        "--product-version=1.2.0.0",
        "--file-description=Fast File Search Pro",
        "--jobs=$Jobs",
        '--assume-yes-for-downloads',
        $CompilerFlag
    )
    if ($Package) { $a += "--include-package=$Package" }
    if ($Mode -eq 'onefile') { $a += '--onefile' }
    if (-not $KeepBuildDir) { $a += '--remove-output' }
    $a += $EntryAbs
    return $a
}

# --------------------------------------------------------- environment helpers
function Import-VcVars {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) { throw 'vswhere.exe not found' }
    $inst = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $inst) { throw 'No Visual Studio install with the C++ x64 toolset found' }
    $bat = Join-Path $inst 'VC\Auxiliary\Build\vcvars64.bat'
    if (-not (Test-Path $bat)) { throw "vcvars64.bat missing: $bat" }
    $dump = cmd /c "`"$bat`" >nul 2>&1 && set"
    foreach ($line in $dump) {
        if ($line -match '^([^=]+)=(.*)$') { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] }
    }
    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) { throw 'cl.exe not on PATH after vcvars64' }
}

function Invoke-Nuitka([string]$Label, [string]$CompilerFlag, [string[]]$Runner = @('run', 'python')) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "`n=== Nuitka build [$Label] mode=$Mode ===" -ForegroundColor Cyan
    $env:PYTHONPATH = $SrcAbs
    $nuitkaArgs = Get-NuitkaArgs $CompilerFlag
    Write-Host ("uv " + ($Runner -join ' ') + " " + ($nuitkaArgs -join ' '))
    & uv @Runner @nuitkaArgs
    $code = $LASTEXITCODE
    Write-Host ("[{0}] exit code {1}, {2:n0}s" -f $Label, $code, $sw.Elapsed.TotalSeconds)
    if ($code -ne 0) { return $false }
    return (Publish-Output)
}

# Normalise output to dist\FastFileSearchPro\ (standalone) or dist\FastFileSearchPro.exe (onefile)
function Publish-Output {
    $stem = [IO.Path]::GetFileNameWithoutExtension($EntryAbs)
    if ($Mode -eq 'onefile') {
        $exe = Join-Path $OutAbs $ExeName
        if (-not (Test-Path $exe)) { Write-Warning "Expected $exe not found"; return $false }
        Write-Host "OK: $exe" -ForegroundColor Green
        return $true
    }
    $dist = Join-Path $OutAbs "$stem.dist"
    $final = Join-Path $OutAbs $Name
    if (-not (Test-Path (Join-Path $dist $ExeName))) { Write-Warning "Expected $dist\$ExeName not found"; return $false }
    if (Test-Path $final) { Remove-Item $final -Recurse -Force }
    Move-Item $dist $final
    Write-Host "OK: $final\$ExeName" -ForegroundColor Green
    return $true
}

# ------------------------------------------------------------------ compilers
function Invoke-Msvc {
    Import-VcVars
    Invoke-Nuitka 'MSVC' '--msvc=latest'
}

function Invoke-Mingw {
    if (-not (Test-Path (Join-Path $MingwBin 'gcc.exe'))) { throw "gcc.exe not found in $MingwBin" }
    $env:PATH = "$MingwBin;$env:PATH"
    # Nuitka 4.1.3 refuses --mingw64 on Python >= 3.13, so run it from an isolated
    # Python 3.12 environment (uv downloads 3.12 + packages once, repo .venv untouched).
    $runner = @('run', '--no-project', '--isolated', '--python', '3.12',
                '--with', 'nuitka==4.1.3', '--with', 'PyQt6>=6.0.0', '--with', 'watchdog>=3.0.0', 'python')
    Invoke-Nuitka 'MinGW64' '--mingw64' $runner
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

        $inner = "uv sync --python $pyVer; `$env:PYTHONPATH='C:\src\$SrcPath'; " +
                 "uv run python -m nuitka --standalone --enable-plugin=pyqt6 --windows-console-mode=disable " +
                 "--windows-icon-from-ico=$Icon --include-data-files=$Icon=files.ico --output-dir=C:\out " +
                 "--output-filename=$ExeName --include-package=$Package --msvc=latest --assume-yes-for-downloads $Entry"
        $cmd = "cmd /c `"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat`" && powershell -NoProfile -Command `"$inner`""
        New-Item -ItemType Directory -Force -Path $OutAbs | Out-Null
        & docker run --name $cont --isolation=hyperv -v "${Root}:C:\src" -v "${OutAbs}:C:\out" $tag cmd /c $cmd
        if ($LASTEXITCODE -ne 0) { throw 'docker run (nuitka build) failed' }
        $ok = Publish-Output
    }
    finally {
        & docker rm -f $cont 2>$null | Out-Null
        & docker rmi -f $tag 2>$null | Out-Null
        Remove-Item $ctx -Recurse -Force -ErrorAction SilentlyContinue
    }
    return $ok
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
exit 0
