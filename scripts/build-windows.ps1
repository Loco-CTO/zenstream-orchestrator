param(
    [string]$PythonExecutable = "python",
    [switch]$SkipFfmpegDownload,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$mediaRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "assets\ffmpeg\windows"))
$backendDist = [IO.Path]::GetFullPath((Join-Path $projectRoot "dist\backend"))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot ".build"))
$venvRoot = Join-Path $buildRoot "windows-venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

function Copy-ReleaseTree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    & robocopy.exe $Source $Destination /E /NFL /NDL /NJH /NJS /NP /XD "__pycache__" ".pytest_cache" /XF "*.pyc" "*.pyo" | Out-Host
    if ($LASTEXITCODE -gt 7) {
        throw "Could not stage release files from $Source (robocopy exit code $LASTEXITCODE)."
    }
}

$ffmpegVersion = "8.1"
$ffmpegArchiveName = "ffmpeg-8.1-full_build.zip"
$ffmpegUrl = "https://github.com/GyanD/codexffmpeg/releases/download/8.1/$ffmpegArchiveName"
$ffmpegSha256 = "587B1C37DE29C5003D01CF65DA10001BAC43A58B88E61AF0FC77C61DAFF04761"

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "The ZenStream Windows launcher supports x64 Windows only."
}

New-Item -ItemType Directory -Force -Path $mediaRoot, $buildRoot | Out-Null
$ffmpegPath = Join-Path $mediaRoot "ffmpeg.exe"
$ffprobePath = Join-Path $mediaRoot "ffprobe.exe"

if (-not $SkipFfmpegDownload -and (-not (Test-Path -LiteralPath $ffmpegPath) -or -not (Test-Path -LiteralPath $ffprobePath))) {
    $temporaryRoot = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) "zenstream-ffmpeg-$ffmpegVersion-$PID"))
    $systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $temporaryRoot.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to use a temporary path outside the system temporary directory."
    }
    New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
    try {
        $archivePath = Join-Path $temporaryRoot $ffmpegArchiveName
        Write-Host "Downloading pinned FFmpeg $ffmpegVersion build..."
        Invoke-WebRequest -UseBasicParsing -Uri $ffmpegUrl -OutFile $archivePath
        $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
        if ($actualHash -ne $ffmpegSha256) {
            throw "FFmpeg archive checksum mismatch. Expected $ffmpegSha256, received $actualHash."
        }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $temporaryRoot -Force
        $binDirectory = Get-ChildItem -LiteralPath $temporaryRoot -Directory |
            Where-Object { $_.Name -like "ffmpeg-*-full_build" } |
            Select-Object -First 1 -ExpandProperty FullName
        if (-not $binDirectory) {
            throw "The pinned FFmpeg archive did not contain the expected full build directory."
        }
        Copy-Item -LiteralPath (Join-Path $binDirectory "bin\ffmpeg.exe") -Destination $ffmpegPath -Force
        Copy-Item -LiteralPath (Join-Path $binDirectory "bin\ffprobe.exe") -Destination $ffprobePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        }
    }
}

if (-not (Test-Path -LiteralPath $ffmpegPath) -or -not (Test-Path -LiteralPath $ffprobePath)) {
    throw "FFmpeg and FFprobe are required beneath assets\ffmpeg\windows."
}
$chromaprintHelp = & $ffmpegPath -hide_banner -h muxer=chromaprint 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $chromaprintHelp -notmatch "fp_format") {
    throw "The bundled FFmpeg must expose the Chromaprint muxer and raw fingerprint format."
}

Write-Host "Building the exported administrator dashboard..."
Push-Location (Join-Path $projectRoot "frontend")
try {
    & npm.cmd ci --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "Dashboard dependency installation failed." }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw "Dashboard build failed." }
}
finally { Pop-Location }

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating the isolated Windows packaging environment..."
    & $PythonExecutable -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python packaging environment." }
}
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Backend virtual-environment installation failed." }

Write-Host "Staging backend source and its dedicated Python environment..."
$resolvedDistRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "dist"))
if (-not $backendDist.StartsWith($resolvedDistRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace a backend staging path outside dist."
}
if (Test-Path -LiteralPath $backendDist) {
    Remove-Item -LiteralPath $backendDist -Recurse -Force
}
$sourceRoot = Join-Path $backendDist "source"
$pythonRuntime = Join-Path $backendDist "python-base"
$venvTemplate = Join-Path $backendDist "venv-template"
New-Item -ItemType Directory -Force -Path $sourceRoot, $pythonRuntime | Out-Null
Copy-ReleaseTree -Source (Join-Path $projectRoot "orchestrator") -Destination (Join-Path $sourceRoot "orchestrator")
Copy-ReleaseTree -Source (Join-Path $projectRoot "migrations") -Destination (Join-Path $sourceRoot "migrations")
Copy-ReleaseTree -Source (Join-Path $projectRoot "assets") -Destination (Join-Path $sourceRoot "assets")
Copy-Item -LiteralPath (Join-Path $projectRoot "alembic.ini") -Destination $sourceRoot
Copy-Item -LiteralPath (Join-Path $projectRoot ".main-version.json") -Destination $sourceRoot
Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $sourceRoot
Copy-ReleaseTree -Source (Join-Path $projectRoot "frontend\out") -Destination (Join-Path $sourceRoot "orchestrator\web")

$pythonBase = (& $PythonExecutable -c "import sys; print(sys.base_prefix)").Trim()
$pythonVersion = (& $PythonExecutable -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
if (-not (Test-Path -LiteralPath (Join-Path $pythonBase "python.exe"))) {
    throw "Could not locate the base Windows Python runtime."
}
Copy-Item -Path (Join-Path $pythonBase "*") -Destination $pythonRuntime -Recurse -Force
$runtimeSitePackages = Join-Path $pythonRuntime "Lib\site-packages"
if (Test-Path -LiteralPath $runtimeSitePackages) {
    Remove-Item -LiteralPath $runtimeSitePackages -Recurse -Force
}
Copy-Item -LiteralPath $venvRoot -Destination $venvTemplate -Recurse
Set-Content -LiteralPath (Join-Path $backendDist "runtime-version.txt") -Value $pythonVersion -Encoding ascii

Push-Location (Join-Path $projectRoot "launcher")
try {
    & npm.cmd ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "Launcher dependency installation failed." }
    & npm.cmd test
    if ($LASTEXITCODE -ne 0) { throw "Launcher tests failed." }
    if ($SkipInstaller) {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Launcher build failed." }
    }
    else {
        & npm.cmd run pack:windows
        if ($LASTEXITCODE -ne 0) { throw "Windows installer packaging failed." }
        $releaseRoot = Join-Path $projectRoot "launcher\release"
        $artifacts = Get-ChildItem -LiteralPath $releaseRoot -File -Filter "*.exe"
        $checksums = $artifacts | ForEach-Object {
            $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "$hash  $($_.Name)"
        }
        Set-Content -LiteralPath (Join-Path $releaseRoot "SHA256SUMS.txt") -Value $checksums -Encoding utf8
    }
}
finally { Pop-Location }
